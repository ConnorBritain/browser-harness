"""CDP WS holder + IPC relay (Unix socket on POSIX, TCP loopback on Windows). One daemon per BU_NAME."""
import asyncio, json, os, platform, re, socket, sys, time, urllib.error, urllib.request
from urllib.parse import urlparse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import _ipc as ipc
from . import auth
from . import paths
from cdp_use.client import CDPClient


def _load_env():
    repo_root = Path(__file__).resolve().parents[2]
    workspace = paths.workspace_dir()
    for p in (repo_root / ".env", workspace / ".env"):
        if not p.exists():
            continue
        _load_env_file(p)


def _load_env_file(p):
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

NAME = os.environ.get("BU_NAME", "default")
SOCK = ipc.sock_addr(NAME)
LOG = str(ipc.log_path(NAME))
PID = str(ipc.pid_path(NAME))
STATE = ipc.state_path(NAME)  # owner -> tabs, so tabs survive a daemon restart
BUF = 500
GUARDED = ("Target.attachToTarget", "Target.closeTarget", "Target.activateTarget")  # never on another owner's tab
_MAC_PROFILES = (
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Google/Chrome Canary",
    "Library/Application Support/Comet",
    "Library/Application Support/Arc/User Data",
    "Library/Application Support/Dia/User Data",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Microsoft Edge Beta",
    "Library/Application Support/Microsoft Edge Dev",
    "Library/Application Support/Microsoft Edge Canary",
    "Library/Application Support/BraveSoftware/Brave-Browser",
    "Library/Application Support/BraveSoftware/Brave-Origin",
)
_LINUX_PROFILES = (
    ".config/google-chrome",
    ".config/chromium",
    ".config/chromium-browser",
    ".config/microsoft-edge",
    ".config/microsoft-edge-beta",
    ".config/microsoft-edge-dev",
    ".var/app/org.chromium.Chromium/config/chromium",
    ".var/app/com.google.Chrome/config/google-chrome",
    ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
    ".var/app/com.microsoft.Edge/config/microsoft-edge",
)
_WINDOWS_PROFILES = (  # relative to %LOCALAPPDATA%; SxS = Canary channel
    "Google/Chrome/User Data",
    "Google/Chrome SxS/User Data",
    "Google/Chrome Beta/User Data",
    "Google/Chrome Dev/User Data",
    "Chromium/User Data",
    "Microsoft/Edge/User Data",
    "Microsoft/Edge Beta/User Data",
    "Microsoft/Edge Dev/User Data",
    "Microsoft/Edge SxS/User Data",
    "BraveSoftware/Brave-Browser/User Data",
)


def _publish_own_pid(path=PID, pid=None):
    """Atomically retain or publish this daemon's PID record.

    The parent may already have published a process-start fingerprint while
    holding the spawn lock. Never truncate that record: another cold caller
    must not observe an empty PID file and start a sibling daemon.
    """
    pid = pid or os.getpid()
    target = Path(path)
    try:
        published = target.read_text()
        try:
            published_pid = int(json.loads(published)["pid"])
        except (json.JSONDecodeError, TypeError, KeyError):
            published_pid = int(published.split()[0])
    except (FileNotFoundError, OSError, ValueError, IndexError):
        published = str(pid)
        published_pid = pid
    if published_pid != pid:
        published = str(pid)
    tmp = target.with_name(f"{target.name}.{pid}.tmp")
    tmp.write_text(published)
    os.replace(tmp, target)


def profile_dirs(system=None):
    system = system or platform.system()
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return [local / p for p in _WINDOWS_PROFILES]
    if system == "Darwin":
        return [Path.home() / p for p in _MAC_PROFILES]
    return [Path.home() / p for p in _LINUX_PROFILES]


PROFILES = profile_dirs()
INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://", "about:")
BU_API = "https://api.browser-use.com/api/v3"
REMOTE_ID = os.environ.get("BU_BROWSER_ID")
_REMOTE_STOPPED = False
BROWSER_KIND = "cloud" if REMOTE_ID else ("cdp" if (os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL")) else "local")
# Chrome 144+ shows a per-connection popup, and the connection that raised it is
# the only thing keeping it on screen. There is deliberately no approval
# deadline: expiry would drop the sheet and make a later attempt create another
# connection and another prompt. Chrome/process death and explicit cancellation
# still terminate the pending connection.
LOCAL_HANDSHAKE_TIMEOUT = None
# How long get_ws_url() keeps waiting for DevToolsActivePort before giving up
NO_TOGGLE_GRACE = 3
TOGGLE_BOOT_GRACE = 12
OWNER_IDLE = float(os.environ.get("BH_OWNER_IDLE", 1800))  # seconds before an idle owner's tabs are released
SWEEP_EVERY = min(60, OWNER_IDLE)
MARKS = {"deliverable": "\U0001F4CC", "handoff": "✋"}  # pin: stays for the person; raised hand: person acts now


def tab_marker_enabled():
    """Whether the cosmetic controlled-tab title marker should be added."""
    return os.environ.get("BH_TAB_MARKER", "").strip().lower() not in {"0", "false", "no", "off"}


def tab_marker_js(prefix):
    """Replace any earlier marker ("\U0001F434<label><mark> ") at the start of the title with `prefix`."""
    return f"(p=>{{const t=document.title.replace(/^\\u{{1F434}}\\S* /u,'');if(document.title!==p+t)document.title=p+t}})({json.dumps(prefix)})"


def _devtools_port_live(base):
    """True when something is listening on the profile's DevToolsActivePort port.

    A stale file left behind by a closed browser must not count as a running
    instance — it would route recovery to "click Allow" on a popup that can't
    exist."""
    try:
        port = int((base / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace").splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return False
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def remote_debugging_user_enabled():
    """chrome://inspect's "Allow remote debugging" toggle

    True only when a toggle-on profile also has a live DevTools port.
    False if a profile records it off, None when no profile records it."""
    seen = None
    for base in PROFILES:
        try:
            state = json.loads((base / "Local State").read_text(encoding="utf-8", errors="replace"))
            enabled = ((state.get("devtools") or {}).get("remote_debugging") or {}).get("user-enabled")
        except (OSError, ValueError, AttributeError):
            continue
        if enabled is True and _devtools_port_live(base):
            return True
        if enabled is False:
            seen = False
    return seen


def remote_debugging_toggle_profiles():
    """Profile dirs whose chrome://inspect toggle is recorded on in Local State"""
    out = []
    for base in PROFILES:
        try:
            state = json.loads((base / "Local State").read_text(encoding="utf-8", errors="replace"))
            if ((state.get("devtools") or {}).get("remote_debugging") or {}).get("user-enabled") is True:
                out.append(base)
        except (OSError, ValueError, AttributeError):
            continue
    return out


def browser_running_for_profile(base):
    """True when a running browser instance holds this user-data-dir (POSIX)"""
    try:
        target = os.readlink(str(base / "SingletonLock"))
    except OSError:
        return False
    try:
        pid = int(target.rsplit("-", 1)[-1])
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # pid exists but belongs to another user


def supported_browser_running():
    """Is any browser whose profile we scan actually running?"""
    if platform.system() == "Windows":
        # Chromium on Windows uses a named mutex instead of SingletonLock —
        import subprocess
        try:
            out = subprocess.check_output(["tasklist"], text=True, errors="replace", timeout=5).lower()
        except Exception:
            return True  # can't tell — assume running so recovery stays on the popup/toggle path
        return any(n in out for n in ("chrome.exe", "msedge.exe", "chromium.exe", "brave.exe", "helium.exe"))
    return any(browser_running_for_profile(base) for base in PROFILES)


def log(msg):
    open(LOG, "a", encoding="utf-8", errors="replace").write(f"{msg}\n")


def _safe_connection_label(url):
    """Log only endpoint topology, never CDP credentials or provider session paths."""
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            return "<redacted-cdp-endpoint>"
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except (TypeError, ValueError):
        return "<redacted-cdp-endpoint>"


async def _silent(coro):
    try:
        await coro
    except Exception:
        pass


def _ws_from_devtools_active_port(http_url: str) -> str | None:
    """When /json/version returns 404 (Chrome 147+ default profile), match DevToolsActivePort by port."""
    p = urlparse(http_url)
    want_port = str(p.port) if p.port else ""
    if not want_port:
        return None
    host = p.hostname or "127.0.0.1"
    if ":" in host:  # urlparse strips IPv6 brackets; restore them for the ws:// URL
        host = f"[{host}]"
    for base in PROFILES:
        try:
            active = (base / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace").splitlines()
        except (FileNotFoundError, NotADirectoryError):
            continue
        port = active[0].strip() if active else ""
        ws_path = active[1].strip() if len(active) > 1 else ""
        if port == want_port and ws_path:
            return f"ws://{host}:{port}{ws_path}"
    return None


def get_ws_url():
    if url := os.environ.get("BU_CDP_WS"):
        return url
    if url := os.environ.get("BU_CDP_URL"):
        # HTTP DevTools endpoint (e.g. http://127.0.0.1:9333) — resolve to ws via /json/version.
        # Use this for a dedicated automation Chrome on a non-default profile, which avoids the
        # M144 "Allow remote debugging" dialog and the M136 default-profile lockdown.
        deadline = time.time() + 30
        last_err = None
        base_url = url.rstrip("/")
        while time.time() < deadline:
            try:
                return json.loads(urllib.request.urlopen(f"{base_url}/json/version", timeout=5).read())["webSocketDebuggerUrl"]
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 403:
                    raise RuntimeError("permission-blocked: Chrome is reachable, but the per-session Allow remote debugging popup has not been accepted")
                if e.code == 404 and (ws := _ws_from_devtools_active_port(url)):
                    return ws
                time.sleep(1)
            except Exception as e:
                last_err = e
                time.sleep(1)
        hint = "is the dedicated automation Chrome running? Launch it with --remote-debugging-port=<port> --user-data-dir=<dedicated dir>"
        if platform.system() == "Windows":
            hint += "; on Windows also check that a firewall/antivirus isn't blocking localhost connections"
        raise RuntimeError(f"BU_CDP_URL={url} unreachable after 30s: {last_err} -- {hint}")
    deadline = time.time() + 30
    next_liveness_check = 0.0
    while time.time() < deadline:
        for base in PROFILES:
            try:
                active = (base / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace").splitlines()
            except (FileNotFoundError, NotADirectoryError):
                continue
            port = active[0].strip() if active else ""
            ws_path = active[1].strip() if len(active) > 1 else ""
            if not port:
                continue
            # Resolve the live WS URL via /json/version instead of trusting the path stored
            # alongside the port in DevToolsActivePort: if Chrome was previously launched
            # with a different --user-data-dir on the same port, that file is left behind
            # with a stale browser UUID and the WS upgrade returns 404.
            try:
                return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read())["webSocketDebuggerUrl"]
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    raise RuntimeError("permission-blocked: Chrome is reachable, but the per-session Allow remote debugging popup has not been accepted")
                # Chrome 147+ disables /json/* HTTP discovery on the default user-data-dir;
                # the ws path Chrome wrote to DevToolsActivePort still works.
                if e.code == 404 and ws_path:
                    return f"ws://127.0.0.1:{port}{ws_path}"
            except (OSError, KeyError, ValueError):
                pass
        # Closed browser leaves stale DevToolsActivePort files
        now = time.time()
        if now >= next_liveness_check:
            if not supported_browser_running():
                raise RuntimeError(
                    "chrome-not-running: no supported Chromium-family browser is running -- start Chrome, then retry"
                )
            next_liveness_check = now + 2
        # The browser is running but the port isn't up; waiting 30s
        grace = TOGGLE_BOOT_GRACE if remote_debugging_toggle_profiles() else NO_TOGGLE_GRACE
        if now > deadline - 30 + grace:
            break
        time.sleep(0.2)
    for probe_port in (9222, 9223):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{probe_port}/json/version", timeout=1) as r:
                return json.loads(r.read())["webSocketDebuggerUrl"]
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise RuntimeError("permission-blocked: Chrome is reachable, but the per-session Allow remote debugging popup has not been accepted")
        except (OSError, KeyError, ValueError):
            continue
    if remote_debugging_user_enabled() is False:
        raise RuntimeError('remote debugging is turned off for this browser instance — enable chrome://inspect/#remote-debugging (tick "Allow remote debugging for this browser instance")')
    raise RuntimeError(f"DevToolsActivePort not found in {[str(p) for p in PROFILES]} — enable chrome://inspect/#remote-debugging, or set BU_CDP_WS for a remote browser")


def stop_remote(strict=False):
    global _REMOTE_STOPPED
    if not REMOTE_ID:
        return True
    if _REMOTE_STOPPED:
        return True
    last_error = None
    for attempt in range(3):
        try:
            key = auth.get_browser_use_api_key()
            req = urllib.request.Request(
                f"{BU_API}/browsers/{REMOTE_ID}",
                data=json.dumps({"action": "stop"}).encode(),
                method="PATCH",
                headers={"X-Browser-Use-API-Key": key, "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15).read()
            _REMOTE_STOPPED = True
            log(f"stopped remote browser {REMOTE_ID}")
            return True
        except Exception as e:
            last_error = e
            log(f"stop_remote attempt {attempt + 1}/3 failed ({REMOTE_ID}): {e}")
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    if strict:
        raise RuntimeError(f"failed to stop remote browser {REMOTE_ID}: {last_error}")
    return False


def is_real_page(t):
    return t["type"] == "page" and not t.get("url", "").startswith(INTERNAL)


def is_blank(url):
    """An empty tab: safe to reuse instead of opening another one."""
    return url in ("", "about:blank", "data:text/html,") or url.startswith(
        ("about:blank#", "chrome://newtab", "chrome://new-tab-page", "edge://newtab", "about:newtab")
    )


def is_inspect_tab(t):
    """A chrome://inspect tab — normally the one the permission flow opened"""
    return t["type"] == "page" and t.get("url", "").startswith("chrome://inspect")


def harness_opened_inspect():
    """True when admin's recovery flow opened a chrome://inspect tab that is
    still awaiting cleanup (the marker survives until the next connect)."""
    try:
        return paths.inspect_marker().exists()
    except OSError:
        return False


class _PatientCDPClient(CDPClient):
    """CDPClient whose local Chrome approval handshake has no deadline."""

    async def start(self):
        import websockets
        if self.ws is not None:
            raise RuntimeError("Client is already started")
        connect_kwargs = {"max_size": self.max_ws_frame_size, "open_timeout": LOCAL_HANDSHAKE_TIMEOUT}
        if self.additional_headers:
            connect_kwargs["additional_headers"] = self.additional_headers
        self.ws = await websockets.connect(self.url, **connect_kwargs)
        self._message_handler_task = asyncio.create_task(self._handle_messages())


@dataclass
class Owner:
    """One agent: the tabs it may drive, its current tab, its events and dialog."""
    tabs: dict = field(default_factory=dict)  # target_id -> session_id, None until first use
    current: str | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=BUF))
    dialog: dict | None = None
    gone: str | None = None  # typed error returned once on the owner's next command
    seen: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    name: str | None = None  # from name_session(), shown in the owner's tab titles
    marks: dict = field(default_factory=dict)  # target_id -> "deliverable" | "handoff"
    created: set = field(default_factory=set)  # tabs this owner opened; unmarked ones close when it goes idle


class Daemon:
    """One browser connection shared by many owners. A tab has at most one owner."""

    def __init__(self):
        self.cdp = None
        self.owners = {}         # owner key -> Owner
        self.tab_owner = {}      # target_id -> owner key
        self.session_owner = {}  # session_id -> owner key
        self.info = {}           # target_id -> latest targetInfo
        self.stop = None         # asyncio.Event, set inside start()

    def owner(self, key):
        o = self.owners.setdefault(key, Owner())
        o.seen = time.time()
        return o

    def _yours(self, o):
        tabs = ", ".join(f"{t} {self.info.get(t, {}).get('url', '')}".strip() for t in o.tabs)
        return f"Your tabs: {tabs or 'none'}."

    def _taken(self, o, tid):
        return {"error": f"tab-taken: tab {tid} is driven by another agent. Nothing was sent. {self._yours(o)}"}

    def _closed(self, o, tid):
        url = self.info.get(tid, {}).get("url", "")
        return f"tab-closed: your tab {tid} {url} was closed. Nothing was sent. {self._yours(o)} Call new_tab(url) or switch_tab(tab)."

    def _mine(self, key, o, tid):
        """None when the owner holds tid, else the error to return."""
        if self.tab_owner.get(tid) == key:
            return None
        return self._taken(o, tid) if tid in self.tab_owner else {"error": f"tab {tid} is not yours; switch_tab() to it first"}

    def _own(self, key, tid, current=True, created=False):
        o = self.owners[key]
        self.tab_owner[tid] = key
        o.tabs.setdefault(tid, None)
        if created:
            o.created.add(tid)
        if current:
            o.current = tid
        self._save()

    def _drop(self, tid):
        """Forget a tab. If it was the owner's current tab, its next command learns it once."""
        key = self.tab_owner.pop(tid, None)
        if key is None:
            return
        o = self.owners[key]
        self.session_owner.pop(o.tabs.pop(tid, None), None)
        o.marks.pop(tid, None)
        o.created.discard(tid)
        if o.current == tid:
            o.current, o.gone = None, self._closed(o, tid)
        self._save()

    def _save(self):
        state = {k: {"tabs": list(o.tabs), "current": o.current, "name": o.name, "marks": o.marks,
                     "created": sorted(o.created)} for k, o in self.owners.items() if o.tabs}
        tmp = STATE.with_name(f"{STATE.name}.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, STATE)

    def _load(self, pages):
        """Owners keep their tabs, names and marks across daemon restarts."""
        try:
            state = json.loads(STATE.read_text())
        except (OSError, ValueError):
            return
        for key, saved in state.items():
            o = self.owner(key)
            o.name = saved.get("name")
            for tid in saved.get("tabs", []):
                if tid in pages:
                    self._own(key, tid, current=tid == saved.get("current"), created=tid in saved.get("created", []))
                    if mark := saved.get("marks", {}).get(tid):
                        o.marks[tid] = mark

    async def _current(self, key, o):
        """The owner's current tab. A fresh owner gets its own background tab, never the person's."""
        async with o.lock:
            if o.current is None:
                spare = [t for t, i in self.info.items() if REMOTE_ID and t not in self.tab_owner
                         and i.get("type") == "page" and is_blank(i.get("url", ""))]
                tid = spare[0] if spare else (await self.cdp.send_raw(
                    "Target.createTarget", {"url": "about:blank", "background": True}))["targetId"]
                self._own(key, tid, created=not spare)
            return o.current

    async def _session(self, key, o, tid):
        """The owner's session on its tab, attached once on first use. None once the tab is gone."""
        async with o.lock:
            if tid in o.tabs and o.tabs[tid] is None:
                sid = (await self.cdp.send_raw("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
                o.tabs[tid], self.session_owner[sid] = sid, key
                await self._enable_default_domains(sid)
                self._mark(key, tid)
            return o.tabs.get(tid)

    def _mark(self, key, tid, prefix=None):
        """Show in the tab title which agent holds it and its mark, off the synchronous IPC path."""
        o = self.owners[key]
        if not tab_marker_enabled() or not o.tabs.get(tid):
            return None
        if prefix is None:
            label = o.name or ("" if key == "default" else re.sub(r"\W", "", key)[:6])
            prefix = f"\U0001F434{label}{MARKS.get(o.marks.get(tid), '')} "
        return asyncio.create_task(_silent(asyncio.wait_for(
            self.cdp.send_raw("Runtime.evaluate", {"expression": tab_marker_js(prefix)}, session_id=o.tabs[tid]),
            timeout=2,
        )))

    async def _release(self, key, o):
        """The owner went idle. Marked tabs stay open and owned for the person; unmarked tabs it
        opened close (never the browser's last tab); unmarked tabs it took are given back."""
        for tid, sid in list(o.tabs.items()):
            if tid in o.marks:
                if sid:
                    o.tabs[tid] = None
                    self.session_owner.pop(sid, None)
                    await _silent(self.cdp.send_raw("Target.detachFromTarget", {"sessionId": sid}))
                continue
            last = sum(i.get("type") == "page" for i in self.info.values()) <= 1
            if tid in o.created and not last:
                await _silent(self.cdp.send_raw("Target.closeTarget", {"targetId": tid}))
                self.info.pop(tid, None)
            elif sid:
                if task := self._mark(key, tid, prefix=""):
                    await task
                await _silent(self.cdp.send_raw("Target.detachFromTarget", {"sessionId": sid}))
            self._drop(tid)
        o.current = o.gone = None
        self._save()

    async def sweep(self):
        """Release owners idle longer than OWNER_IDLE; forget idle owners with no tabs."""
        for key, o in list(self.owners.items()):
            if time.time() - o.seen <= OWNER_IDLE:
                continue
            if any(o.tabs.values()) or o.tabs.keys() - o.marks.keys():
                await self._release(key, o)
            elif not o.tabs:
                del self.owners[key]

    async def _close_inspect_tabs(self, targets):
        """Close chrome://inspect tabs left open by the permission recovery flow"""
        if not harness_opened_inspect():
            return
        for t in targets:
            if is_inspect_tab(t):
                try:
                    await self.cdp.send_raw("Target.closeTarget", {"targetId": t["targetId"]})
                    log(f"closed leftover chrome://inspect tab {t['targetId']}")
                except Exception as e:
                    log(f"close inspect tab {t['targetId']}: {e}")
        try:
            paths.inspect_marker().unlink()
        except OSError:
            pass

    async def _enable_default_domains(self, session_id):
        """Enable Page/DOM/Runtime/Network on a CDP session, in parallel, each bounded.

        Each fresh session starts with all domains disabled; without Network,
        wait_for_network_idle() silently sees no events."""
        async def enable_one(d):
            try:
                await asyncio.wait_for(self.cdp.send_raw(f"{d}.enable", session_id=session_id), timeout=4)
            except Exception as e:
                log(f"enable {d} on {session_id}: {e}")
        await asyncio.gather(*(enable_one(d) for d in ("Page", "DOM", "Runtime", "Network")))

    def _record_event(self, method, params, session_id=None):
        key = self.session_owner.get(session_id)
        if method == "Target.attachedToTarget" and key:
            self.session_owner[params["sessionId"]] = key  # auto-attached iframes and workers
        if method.startswith("Target.") and not session_id:
            return self._target_event(method, params)
        if session_id and key is None:
            return
        for o in [self.owners[key]] if key else self.owners.values():
            o.events.append({"method": method, "params": params, "session_id": session_id})
        if key is None:
            return
        o = self.owners[key]
        if method == "Page.javascriptDialogOpening":
            o.dialog = params
        elif method == "Page.javascriptDialogClosed":
            o.dialog = None
        elif method in ("Page.loadEventFired", "Page.domContentEventFired"):
            for tid, sid in o.tabs.items():
                if sid == session_id:
                    self._mark(key, tid)

    def _target_event(self, method, p):
        info = p.get("targetInfo") or {}
        tid = info.get("targetId") or p.get("targetId")
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            self.info[tid] = info
            opener = self.tab_owner.get(info.get("openerId"))
            if method == "Target.targetCreated" and opener and info.get("type") == "page":
                self._own(opener, tid, current=False, created=True)  # a popup belongs to its opener's owner
        elif method == "Target.targetDestroyed":
            self._drop(tid)
            self.info.pop(tid, None)
        elif method == "Target.targetCrashed" and (key := self.tab_owner.get(tid)):
            o = self.owners[key]
            if o.current == tid:
                o.gone = f"tab-crashed: your tab {tid} {self.info.get(tid, {}).get('url', '')} crashed. Nothing was sent. Call goto_url(url) to reload it or new_tab(url)."
        elif method == "Target.detachedFromTarget":
            o = self.owners.get(self.session_owner.pop(p.get("sessionId"), None))
            if o and o.tabs.get(tid) == p.get("sessionId"):
                o.tabs[tid] = None  # reattached on next use

    async def start(self):
        self.stop = asyncio.Event()
        url = get_ws_url()
        log(f"connecting to {_safe_connection_label(url)}")
        self.cdp = _PatientCDPClient(url) if BROWSER_KIND == "local" else CDPClient(url)
        if BROWSER_KIND == "local":
            # Allow while this handshake is still parked on the popup
            log("handshake-wait: if Chrome shows an 'Allow remote debugging?' popup, click Allow")
        try:
            await self.cdp.start()
        except Exception as e:
            if os.environ.get("BU_CDP_WS"):
                raise RuntimeError(
                    f"CDP WS handshake failed: {e} -- remote browser WebSocket connection failed. "
                    "This can happen when network policy blocks the connection, the WS URL is wrong or expired, or the remote endpoint is down. "
                    "If you use Browser Use cloud, verify auth and get a fresh URL via start_remote_daemon()."
                )
            if BROWSER_KIND == "local" and ("timed out" in str(e).lower() or "403" in str(e)) and remote_debugging_user_enabled():
                raise RuntimeError(
                    "permission-blocked: Chrome did not approve the remote debugging connection; "
                    "browser-harness did not retry or create another connection"
                )
            raise RuntimeError(f"CDP WS handshake failed: {e} -- click Allow in Chrome if prompted, then retry")
        orig = self.cdp._event_registry.handle_event
        async def tap(method, params, session_id=None):
            self._record_event(method, params, session_id)
            return await orig(method, params, session_id)
        self.cdp._event_registry.handle_event = tap
        targets = (await self.cdp.send_raw("Target.getTargets"))["targetInfos"]
        self.info = {t["targetId"]: t for t in targets}
        if BROWSER_KIND == "local":
            await self._close_inspect_tabs(targets)
        await self.cdp.send_raw("Target.setDiscoverTargets", {"discover": True})
        self._load({t["targetId"] for t in targets if t["type"] == "page"})
        log("connected")

    async def handle(self, req):
        # Token guard for Windows TCP loopback: any local process can otherwise
        # connect and issue CDP commands. expected_token() is None on POSIX so
        # this check is a no-op there (AF_UNIX + chmod 600 is the boundary).
        expected = ipc.expected_token()
        if expected is not None and req.get("token") != expected:
            return {"error": "unauthorized"}
        meta = req.get("meta")
        # Liveness probe — lets clients confirm the listener is actually this
        # daemon and not an unrelated process that reused our port post-crash.
        # `pid` lets restart_daemon() verify the live daemon's identity before
        # signaling — protects against SIGTERM-by-stale-pid-file after PID reuse.
        if meta == "ping":
            return {"pong": True, "pid": os.getpid(), "browser_kind": BROWSER_KIND, "endpoint": ipc.endpoint_label()}
        if meta == "shutdown":
            try:
                stop_remote(strict=True)  # a failed Cloud stop keeps the daemon so a retry can stop billing
            except Exception as e:
                return {"error": str(e)}
            self.stop.set()
            return {"ok": True}
        key = req.get("owner") or "default"
        o = self.owner(key)
        try:
            return await (self._meta(key, o, meta, req) if meta else self._send(key, o, req))
        except Exception as e:
            return {"error": str(e)}

    async def _meta(self, key, o, meta, req):
        if meta == "drain_events":
            out = list(o.events)
            o.events.clear()
            return {"events": out}
        if meta == "pending_dialog":
            return {"dialog": o.dialog}
        if meta == "tabs":
            pages = [t for t in (await self.cdp.send_raw("Target.getTargets"))["targetInfos"] if t["type"] == "page"]
            return {"tabs": [{**t, "owner": {key: "you", None: None}.get(self.tab_owner.get(t["targetId"]), "taken"),
                              "mark": o.marks.get(t["targetId"])} for t in pages]}
        if meta == "name_session":
            o.name = re.sub(r"\s+", "-", str(req.get("name") or "").strip())[:16] or None
            for tid in o.tabs:
                self._mark(key, tid)
            self._save()
            return {"name": o.name}
        if meta == "mark_tab":
            tid, mark = req.get("target_id") or o.current, req.get("mark")
            if mark not in (None, *MARKS):
                return {"error": f"mark_tab: mark must be one of {sorted(MARKS)} or None, not {mark!r}"}
            if err := self._mine(key, o, tid):
                return err
            if mark:
                o.marks[tid] = mark
            else:
                o.marks.pop(tid, None)
            self._mark(key, tid)
            self._save()
            return {"target_id": tid, "mark": mark}
        if meta == "connection_status":
            await self.cdp.send_raw("Browser.getVersion")  # raises when the browser connection is dead
            page = None
            if o.current:
                info = (await self.cdp.send_raw("Target.getTargetInfo", {"targetId": o.current}))["targetInfo"]
                if is_real_page(info):
                    page = {"targetId": o.current, "title": info.get("title") or "(untitled)", "url": info.get("url") or ""}
            return {"target_id": o.current, "session_id": o.tabs.get(o.current), "page": page}
        if meta == "close_tab":
            tid = req.get("target_id") or o.current
            if err := self._mine(key, o, tid):
                return err
            await self.cdp.send_raw("Target.closeTarget", {"targetId": tid})
            self._drop(tid)
            return {"closed": tid}
        if meta == "switch_tab":
            tid = req["target_id"]
            try:
                info = (await self.cdp.send_raw("Target.getTargetInfo", {"targetId": tid}))["targetInfo"]
            except Exception:
                return {"error": f"tab-closed: tab {tid} does not exist. Nothing was sent. {self._yours(o)} Call list_tabs()."}
            if self.tab_owner.get(tid, key) != key:
                return self._taken(o, tid)
            if req.get("url") and info.get("url") != req["url"]:
                return {"error": f"tab-changed: tab {tid} now shows {info.get('url')}, not {req['url']}. Nothing was sent. Call list_tabs() and pick again."}
            o.gone = None
            self._own(key, tid)
            if req.get("activate"):
                await self.cdp.send_raw("Target.activateTarget", {"targetId": tid})
            return {"target_id": tid, "session_id": await self._session(key, o, tid)}
        if meta == "new_tab":
            o.gone = None
            reuse = req.get("reuse") and o.current and is_blank(self.info.get(o.current, {}).get("url", "-"))
            tid = o.current if reuse else (await self.cdp.send_raw(
                "Target.createTarget", {"url": "about:blank", "background": True}))["targetId"]
            self._own(key, tid, created=not reuse)
            return {"target_id": tid, "session_id": await self._session(key, o, tid)}
        if o.gone:
            gone, o.gone = o.gone, None
            return {"error": gone}
        tid = await self._current(key, o)
        if meta == "session":
            return {"session_id": await self._session(key, o, tid)}
        if meta == "current_tab":
            info = (await self.cdp.send_raw("Target.getTargetInfo", {"targetId": tid}))["targetInfo"]
            return {"targetId": tid, "url": info.get("url", ""), "title": info.get("title", "")}
        return {"error": f"unknown meta {meta!r}"}

    async def _send(self, key, o, req):
        method, params, sid = req["method"], req.get("params") or {}, req.get("session_id")
        if method.startswith("Target."):  # browser-level; never routed through a tab session
            if method in GUARDED and self.tab_owner.get(params.get("targetId"), key) != key:
                return self._taken(o, params.get("targetId"))
            result = await self.cdp.send_raw(method, params)
            if method == "Target.attachToTarget":
                self.session_owner.setdefault(result["sessionId"], key)
            return {"result": result}
        if sid:  # an explicit session gets exactly that session, never a redirect
            if self.session_owner.get(sid, key) != key:
                return self._taken(o, sid)
            return {"result": await self.cdp.send_raw(method, params, session_id=sid)}
        if o.gone:
            gone, o.gone = o.gone, None
            return {"error": gone}
        tid = await self._current(key, o)
        sid = await self._session(key, o, tid)
        try:
            return {"result": await self.cdp.send_raw(method, params, session_id=sid)}
        except Exception as e:
            if "Session with given id not found" not in str(e):
                raise
        log(f"stale session {sid}, re-attaching {tid}")  # the same tab once, never another tab
        async with o.lock:
            if o.tabs.get(tid) == sid:
                o.tabs[tid] = None
        try:
            sid = await self._session(key, o, tid)
        except Exception:
            sid = None
        if sid is None:
            error = self._closed(o, tid)
            self._drop(tid)
            o.gone = None
            return {"error": error}
        return {"result": await self.cdp.send_raw(method, params, session_id=sid)}


async def serve(d):
    async def handler(reader, writer):
        try:
            line = await reader.readline()
            if not line: return
            resp = await d.handle(json.loads(line))
            writer.write((json.dumps(resp, default=str) + "\n").encode())
            await writer.drain()
        except Exception as e:
            log(f"conn: {e}")
            try:
                writer.write((json.dumps({"error": str(e)}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    async def sweeper():
        while True:
            await asyncio.sleep(SWEEP_EVERY)
            await _silent(d.sweep())

    tasks = {asyncio.create_task(ipc.serve(NAME, handler)), asyncio.create_task(d.stop.wait()), asyncio.create_task(sweeper())}
    if reader := getattr(d.cdp, "_message_handler_task", None):
        tasks.add(reader)  # the browser connection closed: exit instead of serving a dead browser
    await asyncio.sleep(0.05)  # let serve() bind so sock_addr() resolves to the live endpoint
    log(f"listening on {ipc.sock_addr(NAME)} (name={NAME}, remote={REMOTE_ID or 'local'})")
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.result()  # surfaces a serve crash
    finally:
        for t in tasks:
            t.cancel()
            try: await t
            except (asyncio.CancelledError, Exception): pass
        ipc.cleanup_endpoint(NAME)


async def main():
    d = Daemon()
    await d.start()
    await serve(d)


def already_running():
    # Ping handshake (not a bare connect) so a stale .port file + port reuse
    # after a daemon crash doesn't make us mistake an unrelated listener for ours.
    return ipc.ping(NAME, timeout=1.0)


if __name__ == "__main__":
    if already_running():
        print(f"daemon already running on {SOCK}", file=sys.stderr)
        sys.exit(0)
    open(LOG, "w").close()
    _publish_own_pid()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"fatal: {e}")
        sys.exit(1)
    finally:
        stop_remote()
        try: os.unlink(PID)
        except FileNotFoundError: pass

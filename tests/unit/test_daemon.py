import asyncio
import base64
import json
import os
import shutil
import tempfile
import time

import pytest

from browser_harness import daemon


def test_publish_own_pid_never_truncates_parent_record(tmp_path, monkeypatch):
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text('{"pid":4321,"started":"process start with spaces"}')
    real_replace = os.replace
    observed = []

    def replace(src, dst):
        observed.append(pid_file.read_text())
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    daemon._publish_own_pid(pid_file, pid=4321)

    expected = '{"pid":4321,"started":"process start with spaces"}'
    assert observed == [expected]
    assert pid_file.read_text() == expected


@pytest.mark.parametrize(
    ("url", "label"),
    [
        (
            "ws://openclaw-internal:secret@127.0.0.1:18792/devtools/browser/id?token=x",
            "ws://127.0.0.1:18792",
        ),
        ("wss://provider.example/session/private-id?token=secret", "wss://provider.example"),
        ("wss://[::1]:9222/devtools/browser/id", "wss://[::1]:9222"),
        ("not-a-url", "<redacted-cdp-endpoint>"),
    ],
)
def test_safe_connection_label_removes_credentials_paths_and_queries(url, label):
    assert daemon._safe_connection_label(url) == label


def test_remote_stop_retries_and_succeeds(monkeypatch):
    attempts = []
    monkeypatch.setattr(daemon, "REMOTE_ID", "browser-1")
    monkeypatch.setattr(daemon, "_REMOTE_STOPPED", False)
    monkeypatch.setattr(daemon.auth, "get_browser_use_api_key", lambda: "key")
    monkeypatch.setattr(daemon.time, "sleep", lambda _seconds: None)

    def urlopen(_request, timeout):
        attempts.append(timeout)
        if len(attempts) < 3:
            raise OSError("temporary")
        return type("Response", (), {"read": lambda self: b""})()

    monkeypatch.setattr(daemon.urllib.request, "urlopen", urlopen)

    assert daemon.stop_remote(strict=True) is True
    assert attempts == [15, 15, 15]
    assert daemon._REMOTE_STOPPED is True


def test_shutdown_keeps_daemon_alive_when_cloud_stop_fails(monkeypatch):
    d = daemon.Daemon()
    d.stop = asyncio.Event()
    monkeypatch.setattr(
        daemon,
        "stop_remote",
        lambda strict=False: (_ for _ in ()).throw(RuntimeError("billing stop failed")),
    )

    response = asyncio.run(d.handle({"meta": "shutdown"}))

    assert response == {"error": "billing stop failed"}
    assert d.stop.is_set() is False


class _Browser:
    """A fake browser: pages by target id, sessions, and every CDP call it received."""

    def __init__(self, *pages):
        self.calls = []  # (method, params, session_id)
        self.pages = dict(pages)  # target_id -> url
        self.stale = set()  # sessions Chrome no longer knows
        self.fail = {}  # method -> exception raised once
        self.n = 0

    def info(self, tid):
        return {"targetId": tid, "type": "page", "url": self.pages[tid], "title": tid}

    async def send_raw(self, method, params=None, session_id=None):
        params = params or {}
        self.calls.append((method, params, session_id))
        if session_id in self.stale:
            raise RuntimeError("{'code': -32001, 'message': 'Session with given id not found'}")
        if method in self.fail:
            raise self.fail.pop(method)
        tid = params.get("targetId")
        if method in ("Target.getTargetInfo", "Target.attachToTarget") and tid not in self.pages:
            raise RuntimeError("No target with given id found")
        if method == "Target.getTargets":
            return {"targetInfos": [self.info(t) for t in self.pages]}
        if method == "Target.getTargetInfo":
            return {"targetInfo": self.info(tid)}
        if method == "Target.createTarget":
            self.n += 1
            self.pages[f"tab-{self.n}"] = params["url"]
            return {"targetId": f"tab-{self.n}"}
        if method == "Target.attachToTarget":
            self.n += 1
            return {"sessionId": f"s{self.n}-{tid}"}
        if method == "Target.closeTarget":
            self.pages.pop(tid, None)
        return {}

    def sent(self, method):
        return [s for m, _p, s in self.calls if m == method]

    def tab_of(self, session):
        return session.split("-", 1)[1]


def _daemon(*pages):
    d = daemon.Daemon()
    d.cdp = _Browser(*pages)
    d.info = {t: d.cdp.info(t) for t in d.cdp.pages}
    return d


def _run(d, *reqs):
    async def go():
        return [await d.handle(r) for r in reqs]
    return asyncio.run(go())


def cmd(owner, method="Input.insertText", **params):
    return {"owner": owner, "method": method, "params": params or {"text": "x"}}


def meta(owner, kind, **fields):
    return {"owner": owner, "meta": kind, **fields}


PERSON = ("person-tab", "https://mail.example/")


# --- owners and their tabs ---

def test_fresh_owner_gets_its_own_background_tab_never_the_persons():
    """Flipped on purpose: the default daemon used to drive the person's first tab."""
    d = _daemon(PERSON)

    [r] = _run(d, cmd("a"))

    assert r == {"result": {}}
    assert [p for m, p, _s in d.cdp.calls if m == "Target.createTarget"] == [{"url": "about:blank", "background": True}]
    assert [d.cdp.tab_of(s) for s in d.cdp.sent("Input.insertText")] == ["tab-1"]
    assert d.tab_owner == {"tab-1": "a"}


def test_old_clients_without_owner_share_the_default_owner():
    d = _daemon()

    _run(d, {"method": "Input.insertText", "params": {"text": "x"}}, cmd("default"))

    assert len(d.cdp.sent("Target.createTarget")) == 1
    assert d.tab_owner == {"tab-1": "default"}


def test_each_owner_drives_only_its_own_tab():
    d = _daemon(PERSON)

    _run(d, cmd("a"), cmd("b"), cmd("a"), cmd("b"))

    assert [d.cdp.tab_of(s) for s in d.cdp.sent("Input.insertText")] == ["tab-1", "tab-3", "tab-1", "tab-3"]
    assert d.tab_owner == {"tab-1": "a", "tab-3": "b"}


def test_concurrent_first_commands_of_one_owner_open_one_tab():
    d = _daemon()

    async def go():
        return await asyncio.gather(d.handle(cmd("a")), d.handle(cmd("a")))
    asyncio.run(go())

    assert len(d.cdp.sent("Target.createTarget")) == 1
    assert len(d.cdp.sent("Target.attachToTarget")) == 1


def test_cloud_owner_adopts_the_spare_blank_tab(monkeypatch):
    """A cloud browser is exclusive and its live view shows the first tab."""
    monkeypatch.setattr(daemon, "REMOTE_ID", "cloud-browser")
    d = _daemon(("cloud-blank", "about:blank"))

    _run(d, cmd("a"))

    assert d.cdp.sent("Target.createTarget") == []
    assert d.tab_owner == {"cloud-blank": "a"}


def test_switch_tab_takes_a_free_tab_in_the_background():
    d = _daemon(PERSON)

    [r] = _run(d, meta("a", "switch_tab", target_id="person-tab"))

    assert r["target_id"] == "person-tab" and d.tab_owner == {"person-tab": "a"}
    assert d.cdp.sent("Target.activateTarget") == []
    enabled = {m for m, _p, s in d.cdp.calls if s == r["session_id"] and m.endswith(".enable")}
    assert enabled == {"Page.enable", "DOM.enable", "Runtime.enable", "Network.enable"}


def test_switch_tab_activates_only_when_asked():
    d = _daemon(PERSON)

    _run(d, meta("a", "switch_tab", target_id="person-tab", activate=True))

    assert [p for m, p, _s in d.cdp.calls if m == "Target.activateTarget"] == [{"targetId": "person-tab"}]


def test_switch_to_another_owners_tab_is_tab_taken_and_attaches_nothing():
    d = _daemon(PERSON)
    _run(d, meta("a", "switch_tab", target_id="person-tab"))
    attaches = len(d.cdp.sent("Target.attachToTarget"))

    [r] = _run(d, meta("b", "switch_tab", target_id="person-tab"))

    assert r["error"].startswith("tab-taken: tab person-tab is driven by another agent. Nothing was sent.")
    assert len(d.cdp.sent("Target.attachToTarget")) == attaches
    assert d.tab_owner == {"person-tab": "a"}


def test_tab_closed_during_a_switch_is_tab_closed_and_keeps_your_current_tab():
    """Found by the soak: c closes its tab while b's switch_tab awaits the tab info."""
    d = _daemon()
    _run(d, cmd("b"), cmd("c"))
    real = d.cdp.send_raw

    async def close_meanwhile(method, params=None, session_id=None):
        r = await real(method, params, session_id)
        if method == "Target.getTargetInfo" and params["targetId"] == "tab-3":
            d.cdp.pages.pop("tab-3")
            d._record_event("Target.targetDestroyed", {"targetId": "tab-3"})
        return r
    d.cdp.send_raw = close_meanwhile

    switched, after = _run(d, meta("b", "switch_tab", target_id="tab-3"), cmd("b"))

    assert switched["error"].startswith("tab-closed: your tab tab-3")
    assert d.owners["b"].current == "tab-1" and list(d.owners["b"].tabs) == ["tab-1"]
    assert after == {"result": {}} and d.tab_owner == {"tab-1": "b"}


def test_tab_closed_right_after_attach_is_tab_closed_not_a_zombie():
    """Attach reply and targetDestroyed arrive in one read (an OAuth popup closing itself)."""
    d = _daemon(("popup", "https://login.example/"))
    _run(d, cmd("a"))
    real = d.cdp.send_raw

    async def attach_then_destroy(method, params=None, session_id=None):
        r = await real(method, params, session_id)
        if method == "Target.attachToTarget" and params["targetId"] == "popup":
            d.cdp.pages.pop("popup")
            d._record_event("Target.targetDestroyed", {"targetId": "popup"})
        return r
    d.cdp.send_raw = attach_then_destroy

    switched, after = _run(d, meta("a", "switch_tab", target_id="popup"), cmd("a", "Storage.clearCookies"))

    assert switched["error"].startswith("tab-closed: your tab popup")
    assert d.owners["a"].tabs.keys() == {"tab-1"} and d.owners["a"].current == "tab-1"
    assert [d.cdp.tab_of(s) for s in d.cdp.sent("Storage.clearCookies")] == ["tab-1"]


def test_switch_tab_with_a_moved_url_is_tab_changed():
    d = _daemon(PERSON)

    [r] = _run(d, meta("a", "switch_tab", target_id="person-tab", url="https://mail.example/old"))

    assert r["error"].startswith("tab-changed: tab person-tab now shows https://mail.example/")
    assert d.tab_owner == {}


def test_switch_to_a_missing_tab_is_tab_closed():
    d = _daemon()

    [r] = _run(d, meta("a", "switch_tab", target_id="gone-tab"))

    assert r["error"].startswith("tab-closed: tab gone-tab does not exist.")


@pytest.mark.parametrize("method", ["Target.attachToTarget", "Target.closeTarget", "Target.activateTarget"])
def test_raw_target_calls_on_another_owners_tab_are_tab_taken(method):
    d = _daemon()
    _run(d, cmd("a"))
    sent = len(d.cdp.calls)

    [r] = _run(d, cmd("b", method, targetId="tab-1"))

    assert r["error"].startswith("tab-taken:")
    assert len(d.cdp.calls) == sent


def test_raw_attach_session_belongs_to_its_owner():
    d = _daemon(("frame", "https://ads.example/"))
    [r] = _run(d, cmd("a", "Target.attachToTarget", targetId="frame", flatten=True))
    sid = r["result"]["sessionId"]

    a, b = _run(d, {**cmd("a", "Runtime.evaluate", expression="1"), "session_id": sid},
                {**cmd("b", "Runtime.evaluate", expression="1"), "session_id": sid})

    assert a == {"result": {}}
    assert b["error"].startswith("tab-taken:")


def test_another_agents_tab_shows_no_url_or_title_even_to_raw_cdp():
    """Review S7: hiding was client-side only."""
    d = _daemon(PERSON)
    _run(d, cmd("a"), cmd("b"))
    d.cdp.pages["tab-3"] = "https://secret.example/token=abc"
    d.info["frame"] = {"targetId": "frame", "type": "iframe", "url": "https://secret.example/pay", "parentFrameId": "tab-3"}

    tabs, targets, info, own = _run(d, meta("a", "tabs"), cmd("a", "Target.getTargets"),
                                    cmd("a", "Target.getTargetInfo", targetId="tab-3"), cmd("b", "Target.getTargetInfo", targetId="tab-3"))

    assert "secret" not in json.dumps([tabs, targets, info]) and "secret" in json.dumps(own)
    assert {t["targetId"]: t.get("url") for t in tabs["tabs"]} == {"person-tab": "https://mail.example/", "tab-1": "about:blank", "tab-3": None}
    assert d._visible("a", d.info["frame"]) == {"targetId": "frame", "type": "iframe", "parentFrameId": "tab-3"}


def test_raw_attach_to_a_free_tab_takes_it():
    """Review S7: A raw-attached the person's tab, B took it with switch_tab, A kept driving it."""
    d = _daemon(PERSON, ("frame", "https://ads.example/"))
    d.info["frame"]["type"] = "iframe"

    attached, b_switch, b_attach, b_frame = _run(
        d, cmd("a", "Target.attachToTarget", targetId="person-tab", flatten=True), meta("b", "switch_tab", target_id="person-tab"),
        cmd("b", "Target.attachToTarget", targetId="person-tab", flatten=True), cmd("b", "Target.attachToTarget", targetId="frame", flatten=True))

    assert "sessionId" in attached["result"] and d.tab_owner == {"person-tab": "a"}
    assert d.owners["a"].current is None  # taken, but not made current
    assert b_switch["error"].startswith("tab-taken:") and b_attach["error"].startswith("tab-taken:")
    assert "sessionId" in b_frame["result"] and "frame" not in d.tab_owner  # a target that is no tab has no owner


def test_close_tab_closes_only_your_tabs():
    d = _daemon(PERSON)
    _run(d, cmd("a"))

    other, person, own = _run(d, meta("b", "close_tab", target_id="tab-1"),
                              meta("b", "close_tab", target_id="person-tab"),
                              meta("a", "close_tab"))

    assert other["error"].startswith("tab-taken:")
    assert person["error"] == "tab person-tab is not yours; switch_tab() to it first"
    assert own == {"closed": "tab-1"}
    assert list(d.cdp.pages) == ["person-tab"]


def test_after_closing_your_tab_the_next_command_is_tab_closed_once():
    """Nothing is sent to any other tab; the command after that opens a fresh tab."""
    d = _daemon(PERSON)
    _run(d, cmd("a"))

    _, gone, fresh = _run(d, meta("a", "close_tab"), cmd("a", "Page.navigate", url="https://x.example/"), cmd("a"))

    assert gone["error"].startswith("tab-closed: your tab tab-1")
    assert d.cdp.sent("Page.navigate") == []
    assert fresh == {"result": {}} and d.tab_owner == {"tab-3": "a"}


def test_person_closing_your_tab_is_tab_closed_once():
    d = _daemon(PERSON)
    _run(d, cmd("a"))
    d.cdp.pages.pop("tab-1")
    d._record_event("Target.targetDestroyed", {"targetId": "tab-1"})

    [r] = _run(d, cmd("a"))

    assert r["error"].startswith("tab-closed: your tab tab-1")
    assert [d.cdp.tab_of(s) for s in d.cdp.sent("Input.insertText")] == ["tab-1"]


def test_crash_is_tab_crashed_once():
    d = _daemon()
    _run(d, cmd("a"))
    d._record_event("Target.targetCrashed", {"targetId": "tab-1"})

    crashed, after = _run(d, cmd("a"), cmd("a"))

    assert crashed["error"].startswith("tab-crashed: your tab tab-1")
    assert after == {"result": {}}
    assert len(d.cdp.sent("Input.insertText")) == 2


def test_new_tab_reuses_an_empty_current_tab_only_for_a_url():
    d = _daemon(("blank", "data:text/html,"))
    _run(d, meta("a", "switch_tab", target_id="blank"))

    reused, fresh = _run(d, meta("a", "new_tab", reuse=True), meta("a", "new_tab", reuse=False))

    assert reused["target_id"] == "blank"
    assert fresh["target_id"] == "tab-2"
    assert [p for m, p, _s in d.cdp.calls if m == "Target.createTarget"] == [{"url": "about:blank", "background": True}]


def test_new_tab_clears_a_pending_tab_closed():
    d = _daemon()
    _run(d, cmd("a"), meta("a", "close_tab"))

    _, r = _run(d, meta("a", "new_tab"), cmd("a"))

    assert r == {"result": {}}


def test_popup_belongs_to_its_openers_owner():
    d = _daemon()
    _run(d, cmd("a"))

    d._record_event("Target.targetCreated", {"targetInfo": {"targetId": "popup", "type": "page", "url": "", "openerId": "tab-1"}})

    assert d.tab_owner["popup"] == "a"
    assert d.owners["a"].current == "tab-1"


def test_events_and_dialogs_belong_to_their_owner():
    d = _daemon()
    _run(d, cmd("a"), cmd("b"))
    sa, sb = d.owners["a"].tabs["tab-1"], d.owners["b"].tabs["tab-3"]

    d._record_event("Network.requestWillBeSent", {"requestId": "1"}, sa)
    d._record_event("Page.javascriptDialogOpening", {"message": "hi"}, sb)
    d._record_event("Browser.downloadWillBegin", {"frameId": "tab-1", "guid": "g", "url": "https://a.example/report.pdf"})
    d._record_event("Browser.downloadProgress", {"guid": "g", "state": "completed"})
    d._record_event("Browser.downloadProgress", {"guid": "someone-elses", "state": "inProgress"})
    d._record_event("Network.requestWillBeSent", {"requestId": "2"}, "unknown-session")

    ea, eb, da, db = _run(d, meta("a", "drain_events"), meta("b", "drain_events"),
                          meta("a", "pending_dialog"), meta("b", "pending_dialog"))
    assert [e["method"] for e in ea["events"]] == ["Network.requestWillBeSent", "Browser.downloadWillBegin", "Browser.downloadProgress"]
    assert [e["method"] for e in eb["events"]] == ["Page.javascriptDialogOpening"] and d.downloads == {}
    assert da == {"dialog": None} and db == {"dialog": {"message": "hi"}}


def test_auto_attached_child_sessions_belong_to_their_owner():
    d = _daemon()
    _run(d, cmd("a"))
    sa = d.owners["a"].tabs["tab-1"]

    d._record_event("Target.attachedToTarget", {"sessionId": "iframe-session", "targetInfo": {"targetId": "f"}}, sa)

    assert d.session_owner["iframe-session"] == "a"


def test_tabs_meta_shows_you_taken_or_free():
    d = _daemon(PERSON)
    _run(d, cmd("a"), cmd("b"))

    [r] = _run(d, meta("a", "tabs"))

    assert {t["targetId"]: t["owner"] for t in r["tabs"]} == {"person-tab": None, "tab-1": "you", "tab-3": "taken"}


def test_detached_session_reattaches_the_same_tab_on_next_use():
    d = _daemon()
    _run(d, cmd("a"))
    d._record_event("Target.detachedFromTarget", {"sessionId": d.owners["a"].tabs["tab-1"], "targetId": "tab-1"})

    _run(d, cmd("a"))

    assert [p["targetId"] for m, p, _s in d.cdp.calls if m == "Target.attachToTarget"] == ["tab-1", "tab-1"]


def test_current_tab_meta_passes_the_owners_target_id():
    """Regression for issue #304: Target.getTargetInfo needs the targetId."""
    d = _daemon(PERSON)
    _run(d, meta("a", "switch_tab", target_id="person-tab"))

    [r] = _run(d, meta("a", "current_tab"))

    assert r == {"targetId": "person-tab", "url": "https://mail.example/", "title": "person-tab"}
    assert [p for m, p, s in d.cdp.calls if m == "Target.getTargetInfo"][-1] == {"targetId": "person-tab"}


def test_an_old_clients_request_opens_no_tab():
    d = _daemon()

    [r] = _run(d, {"meta": "set_session", "session_id": "s", "target_id": "t"})

    assert r["error"].startswith("unknown request 'set_session': this daemon speaks protocol")
    assert d.cdp.calls == []


def test_connection_status_is_healthy_before_any_tab():
    d = _daemon(PERSON)

    [r] = _run(d, meta("a", "connection_status"))

    assert r == {"target_id": None, "session_id": None, "page": None}
    assert d.cdp.sent("Target.createTarget") == []


def test_event_errors_never_reach_the_websocket_reader(monkeypatch):
    """An exception in the event tap would end the reader and with it the daemon."""
    logged = []
    monkeypatch.setattr(daemon, "log", logged.append)
    d = _daemon(("frame", "https://ads.example/"))
    d.info["frame"]["type"] = "iframe"
    r, _ = _run(d, cmd("a", "Target.attachToTarget", targetId="frame", flatten=True), cmd("b"))
    d.owners["a"].seen = 0
    asyncio.run(d.sweep())  # a is idle and holds nothing: forgotten, but its raw session lives on

    d._record_event("Runtime.consoleAPICalled", {}, r["result"]["sessionId"])
    monkeypatch.setattr(daemon, "STATE", daemon.STATE.parent / "missing-dir" / "owners.json")
    d._record_event("Target.targetDestroyed", {"targetId": "tab-2"})
    monkeypatch.setattr(d, "_route_event", lambda *a: 1 / 0)
    d._record_event("Page.loadEventFired", {}, "s")

    assert "a" not in d.owners and d.tab_owner == {}
    assert [m.split(":")[0] for m in logged] == ["save owners", "event Page.loadEventFired"]


# --- stale sessions (S1) ---

def test_stale_session_never_replays_onto_another_tab():
    """S1: the agent's tab is gone; the person's tab must not receive the command."""
    d = _daemon(PERSON)
    _run(d, cmd("a"))
    d.cdp.stale.add(d.owners["a"].tabs["tab-1"])
    d.cdp.pages.pop("tab-1")

    [r] = _run(d, cmd("a"))

    assert r["error"].startswith("tab-closed: your tab tab-1")
    assert [p["targetId"] for m, p, _s in d.cdp.calls if m == "Target.attachToTarget"] == ["tab-1", "tab-1"]
    assert [d.cdp.tab_of(s) for s in d.cdp.sent("Input.insertText")] == ["tab-1", "tab-1"]
    assert d.tab_owner == {}


def test_stale_session_reattaches_the_same_tab_once():
    d = _daemon(PERSON)
    _run(d, cmd("a"))
    stale = d.owners["a"].tabs["tab-1"]
    d.cdp.stale.add(stale)

    [r] = _run(d, cmd("a"))

    assert r == {"result": {}}
    assert [d.cdp.tab_of(s) for s in d.cdp.sent("Input.insertText")] == ["tab-1", "tab-1", "tab-1"]
    assert d.owners["a"].tabs["tab-1"] != stale


def test_concurrent_stale_requests_reattach_once():
    d = _daemon()
    _run(d, cmd("a"))
    d.cdp.stale.add(d.owners["a"].tabs["tab-1"])

    async def go():
        return await asyncio.gather(d.handle(cmd("a")), d.handle(cmd("a")))
    results = asyncio.run(go())

    assert results == [{"result": {}}, {"result": {}}]
    assert len(d.cdp.sent("Target.attachToTarget")) == 2  # first use + one reattach


def test_stale_request_stays_on_its_tab_when_the_owner_switches_meanwhile():
    d = _daemon(PERSON)
    _run(d, cmd("a"))
    d.cdp.stale.add(d.owners["a"].tabs["tab-1"])
    gate = asyncio.Event()
    real = d.cdp.send_raw

    async def slow_reattach(method, params=None, session_id=None):
        if method == "Target.attachToTarget" and params["targetId"] == "tab-1":
            await gate.wait()
        return await real(method, params, session_id)
    d.cdp.send_raw = slow_reattach

    async def go():
        stale = asyncio.create_task(d.handle(cmd("a", text="old-tab-action")))
        await asyncio.sleep(0.01)
        switch = asyncio.create_task(d.handle(meta("a", "switch_tab", target_id="person-tab")))
        await asyncio.sleep(0.01)
        gate.set()
        return await stale, await switch
    result, switched = asyncio.run(go())

    assert result == {"result": {}} and switched["target_id"] == "person-tab"
    actions = [s for m, p, s in d.cdp.calls if m == "Input.insertText" and p == {"text": "old-tab-action"}]
    assert [d.cdp.tab_of(s) for s in actions] == ["tab-1", "tab-1"]


def test_failed_attach_keeps_the_tab_for_the_retry():
    d = _daemon()
    d.cdp.fail["Target.attachToTarget"] = RuntimeError("simulated attach failure")

    first, second = _run(d, cmd("a"), cmd("a"))

    assert first["error"] == "simulated attach failure"
    assert second == {"result": {}}
    assert len(d.cdp.sent("Target.createTarget")) == 1


def test_explicit_stale_session_is_not_redirected():
    """Explicit session requests keep their exact-session semantics."""
    d = _daemon()
    d.cdp.stale.add("explicit-stale-session")

    [r] = _run(d, {**cmd("a", "Runtime.evaluate", expression="1"), "session_id": "explicit-stale-session"})

    assert "Session with given id not found" in r["error"]
    assert d.cdp.calls == [("Runtime.evaluate", {"expression": "1"}, "explicit-stale-session")]


# --- tab marker and domains ---

def _titles(d):
    """Title prefixes the daemon wrote, in order, as (prefix, tab)."""
    return [(json.loads(p["expression"].rsplit("(", 1)[1].rstrip(")")), d.cdp.tab_of(s))
            for m, p, s in d.cdp.calls if m == "Runtime.evaluate"]


def _settle(d, *reqs, events=()):
    async def go():
        out = [await d.handle(r) for r in reqs]
        for e in events:
            d._record_event(*e)
        for _ in range(3):
            await asyncio.sleep(0)
        return out
    return asyncio.run(go())


@pytest.mark.parametrize("value", ["0", "false", "NO", "off"])
def test_tab_marker_can_be_disabled(monkeypatch, value):
    monkeypatch.setenv("BH_TAB_MARKER", value)
    d = _daemon()
    _run(d, cmd("a"))

    _settle(d, meta("a", "name_session", name="x"), meta("a", "mark_tab", mark="handoff"),
            events=[("Page.loadEventFired", {}, d.owners["a"].tabs["tab-1"])])

    assert d.cdp.sent("Runtime.evaluate") == []


def test_tab_title_shows_owner_and_mark_on_attach_load_name_and_mark(monkeypatch):
    monkeypatch.delenv("BH_TAB_MARKER", raising=False)
    d = _daemon()
    _settle(d, cmd("agent-7"))
    sid = d.owners["agent-7"].tabs["tab-1"]

    _settle(d, meta("agent-7", "name_session", name="checkout bot"), meta("agent-7", "mark_tab", mark="handoff"),
            events=[("Page.loadEventFired", {}, sid), ("Page.loadEventFired", {}, "someone-elses-session")])

    assert _titles(d) == [("\U0001F434agent7 ", "tab-1"), ("\U0001F434checkout-bot ", "tab-1"),
                          ("\U0001F434checkout-bot✋ ", "tab-1"), ("\U0001F434checkout-bot✋ ", "tab-1")]


def test_default_owner_keeps_the_plain_horse(monkeypatch):
    monkeypatch.delenv("BH_TAB_MARKER", raising=False)
    d = _daemon()

    _settle(d, cmd("default"))

    assert _titles(d) == [("\U0001F434 ", "tab-1")]


def test_tab_marker_js_replaces_any_earlier_marker():
    js = daemon.tab_marker_js("\U0001F434b\U0001F4CC ")
    assert js.startswith("(p=>{const t=document.title.replace(/^\\u{1F434}\\S* /u,'');")
    assert js.endswith('("\\ud83d\\udc34b\\ud83d\\udccc ")')


def test_mark_tab_only_marks_your_tabs_with_a_known_mark():
    d = _daemon(PERSON)
    _run(d, cmd("a"), cmd("b"))

    ok, taken, free, bad, tabs, cleared = _run(
        d, meta("a", "mark_tab", target_id="tab-1", mark="deliverable"), meta("a", "mark_tab", target_id="tab-3", mark="handoff"),
        meta("a", "mark_tab", target_id="person-tab", mark="handoff"), meta("a", "mark_tab", mark="keep"),
        meta("a", "tabs"), meta("b", "mark_tab", mark=None))

    assert ok == {"target_id": "tab-1", "mark": "deliverable"}
    assert taken["error"].startswith("tab-taken:")
    assert free["error"] == "tab person-tab is not yours; switch_tab() to it first"
    assert bad["error"].startswith("mark_tab: mark must be one of ['deliverable', 'handoff']")
    assert {t["targetId"]: t["mark"] for t in tabs["tabs"]} == {"person-tab": None, "tab-1": "deliverable", "tab-3": None}
    assert cleared == {"target_id": "tab-3", "mark": None} and d.owners["a"].marks == {"tab-1": "deliverable"}


def _idle_owner_with_five_tabs(monkeypatch):
    monkeypatch.delenv("BH_TAB_MARKER", raising=False)
    d = _daemon(PERSON, ("person-2", "https://docs.example/"))
    _run(d, cmd("a"), meta("a", "new_tab"), meta("a", "mark_tab", mark="deliverable"),
         meta("a", "switch_tab", target_id="person-2"), meta("a", "mark_tab", mark="handoff"),
         meta("a", "switch_tab", target_id="person-tab"), meta("a", "new_tab"))
    d.owners["a"].seen = 0
    d.cdp.calls.clear()
    return d  # tab-1 opened, tab-3 deliverable, person-2 handoff, person-tab taken, tab-7 current


def test_idle_release_gives_tabs_back_and_closes_nothing(monkeypatch):
    """The current tab and marked tabs stay as they are; the rest is detached with a clean title."""
    monkeypatch.delenv("BH_CLOSE_IDLE_TABS", raising=False)
    d = _idle_owner_with_five_tabs(monkeypatch)
    current_session = d.owners["a"].tabs["tab-7"]

    asyncio.run(d.sweep())

    assert sorted(d.cdp.pages) == ["person-2", "person-tab", "tab-1", "tab-3", "tab-7"]
    assert d.cdp.sent("Target.closeTarget") == []
    assert d.tab_owner == {"tab-7": "a"}
    assert d.owners["a"].current == "tab-7" and d.owners["a"].tabs["tab-7"] == current_session
    assert sorted(t for p, t in _titles(d) if p == "") == ["person-tab", "tab-1"]
    assert sorted(d.cdp.tab_of(p["sessionId"]) for m, p, _s in d.cdp.calls if m == "Target.detachFromTarget") == ["person-2", "person-tab", "tab-1", "tab-3"]
    d.cdp.calls.clear()
    asyncio.run(d.sweep())
    assert d.cdp.calls == []  # nothing left to release


def test_idle_release_closes_opened_tabs_only_when_asked(monkeypatch):
    monkeypatch.setenv("BH_CLOSE_IDLE_TABS", "1")
    d = _idle_owner_with_five_tabs(monkeypatch)

    asyncio.run(d.sweep())

    assert [p["targetId"] for m, p, _s in d.cdp.calls if m == "Target.closeTarget"] == ["tab-1"]
    assert sorted(d.cdp.pages) == ["person-2", "person-tab", "tab-3", "tab-7"]


def test_an_idle_owners_tab_can_be_taken_and_it_learns_tab_released():
    """Review S8: a finished agent's marked tab must not stay taken forever."""
    d = _daemon(PERSON)
    _run(d, cmd("a"), meta("a", "mark_tab", mark="deliverable"), cmd("b"))
    busy, _ = _run(d, meta("b", "switch_tab", target_id="tab-1"), meta("b", "tabs"))
    d.owners["a"].seen = 0
    asyncio.run(d.sweep())

    listed, taken, back, after = _run(d, meta("b", "tabs"), meta("b", "switch_tab", target_id="tab-1"), cmd("a"), cmd("a"))

    assert busy["error"].startswith("tab-taken:")
    assert {t["targetId"]: t["owner"] for t in listed["tabs"]}["tab-1"] is None
    assert taken["target_id"] == "tab-1" and d.tab_owner["tab-1"] == "b"
    assert back["error"].startswith("tab-released: your tab tab-1 was taken by another agent while you were idle.")
    assert after == {"result": {}} and d.owners["a"].current not in (None, "tab-1")


def test_release_tabs_closes_what_the_agent_opened_and_leaves_the_rest(monkeypatch):
    """Found on real Chrome: an agent that finished left its own about:blank tab behind."""
    monkeypatch.delenv("BH_CLOSE_IDLE_TABS", raising=False)
    d = _idle_owner_with_five_tabs(monkeypatch)
    d.owners["a"].seen = time.time()

    [r, after] = _run(d, meta("a", "release_tabs"), cmd("a"))

    assert r == {"released": True}
    assert sorted(d.cdp.pages) == ["person-2", "person-tab", "tab-3", "tab-9"]  # tab-1 and tab-7 closed, tab-9 is new
    assert sorted(t for p, t in _titles(d) if p == "") == ["person-tab"]
    assert after == {"result": {}} and d.tab_owner == {"tab-9": "a"}  # no stale tab-closed afterwards


def test_close_tab_closes_the_tab_the_agent_opened():
    d = _daemon(PERSON)
    _run(d, cmd("a"))

    [r] = _run(d, meta("a", "close_tab"))

    assert r == {"closed": "tab-1"} and list(d.cdp.pages) == ["person-tab"] and d.tab_owner == {}


def test_after_idle_the_next_command_still_goes_to_the_same_tab():
    """Review B1: the agent waits 31 min for an approval, then clicks in its form or handoff tab."""
    d = _daemon(PERSON)
    _run(d, cmd("a"), meta("a", "mark_tab", mark="handoff"))
    d.owners["a"].seen = 0

    asyncio.run(d.sweep())
    [r] = _run(d, cmd("a", text="continue"))

    assert r == {"result": {}} and d.cdp.sent("Target.createTarget") == [None]
    assert [d.cdp.tab_of(s) for s in d.cdp.sent("Input.insertText")] == ["tab-1", "tab-1"]


def test_a_popup_from_an_owned_tab_is_never_closed_on_idle(monkeypatch):
    """The person may have opened it from a handoff tab."""
    monkeypatch.setenv("BH_CLOSE_IDLE_TABS", "1")
    d = _daemon(PERSON)
    _run(d, cmd("a"), meta("a", "mark_tab", mark="handoff"))
    d.cdp.pages["popup"] = "https://bank.example/"
    d._record_event("Target.targetCreated", {"targetInfo": {"targetId": "popup", "type": "page", "url": "https://bank.example/", "openerId": "tab-1"}})
    d.owners["a"].seen = 0

    asyncio.run(d.sweep())

    assert "popup" in d.cdp.pages and "popup" not in d.tab_owner


def test_release_waits_for_the_owners_running_command_and_then_skips():
    """The owner comes back while the sweep waits for its lock: nothing is released."""
    d = _daemon(PERSON)
    _run(d, cmd("a"), meta("a", "new_tab"))

    async def go():
        o = d.owners["a"]
        await o.lock.acquire()  # a command of the owner is attaching a tab
        o.seen = 0
        sweep = asyncio.create_task(d.sweep())
        await asyncio.sleep(0.01)
        o.seen = time.time()  # ... and it was a new request
        o.lock.release()
        await sweep
    asyncio.run(go())

    assert d.tab_owner == {"tab-1": "a", "tab-3": "a"} and d.cdp.sent("Target.detachFromTarget") == []


def test_idle_release_never_closes_the_browsers_last_real_page(monkeypatch):
    monkeypatch.setenv("BH_CLOSE_IDLE_TABS", "1")
    d = _daemon(("settings", "chrome://settings/"))
    _run(d, cmd("a", "Page.navigate", url="https://shop.example/"), meta("a", "new_tab"))
    d.cdp.pages["tab-1"] = "https://shop.example/"
    d.info = {t: d.cdp.info(t) for t in d.cdp.pages}
    d.owners["a"].seen = 0

    asyncio.run(d.sweep())

    assert "tab-1" in d.cdp.pages and "tab-1" not in d.tab_owner


def test_sweep_keeps_active_owners_and_forgets_idle_empty_ones():
    d = _daemon()
    _run(d, cmd("a"), meta("b", "tabs"))
    d.owners["b"].seen = 0

    asyncio.run(d.sweep())

    assert list(d.owners) == ["a"] and d.tab_owner == {"tab-1": "a"}


def test_only_the_current_tab_streams_network_events():
    """Review S10: a chatty background tab must not push the current tab's events out of the ring."""
    d = _daemon(PERSON)
    _run(d, cmd("a"))
    first = d.owners["a"].tabs["tab-1"]
    d.cdp.calls.clear()

    _run(d, meta("a", "new_tab"))
    second = d.owners["a"].tabs["tab-3"]
    _run(d, meta("a", "switch_tab", target_id="tab-1"), meta("a", "switch_tab", target_id="tab-1"))

    assert [(m, s) for m, _p, s in d.cdp.calls if m == "Network.disable"] == [("Network.disable", first), ("Network.disable", second)]
    assert [s for m, _p, s in d.cdp.calls if m == "Network.enable"][-1] == first
    assert d.owners["a"].current == "tab-1"


def test_enable_default_domains_runs_four_enables_in_parallel():
    class _Probe(_Browser):
        def __init__(self):
            super().__init__()
            self.in_flight = self.peak = 0
            self.release = asyncio.Event()

        async def send_raw(self, method, params=None, session_id=None):
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await self.release.wait()
            self.in_flight -= 1
            return {}

    async def go():
        d = daemon.Daemon()
        d.cdp = _Probe()
        task = asyncio.create_task(d._enable_default_domains("s"))
        for _ in range(50):
            await asyncio.sleep(0)
        d.cdp.release.set()
        await task
        return d.cdp.peak
    assert asyncio.run(go()) == 4


def test_enable_default_domains_swallows_errors_per_domain():
    d = _daemon()
    d.cdp.fail["DOM.enable"] = RuntimeError("simulated DOM failure")

    asyncio.run(d._enable_default_domains("s"))

    assert [m for m, _p, _s in d.cdp.calls] == ["Page.enable", "DOM.enable", "Runtime.enable", "Network.enable"]


def test_close_inspect_tabs_only_when_the_harness_opened_one(monkeypatch, tmp_path):
    monkeypatch.setenv("BH_CONFIG_DIR", str(tmp_path))
    inspect = ("inspect-tab", "chrome://inspect/#remote-debugging")
    d = _daemon(inspect, PERSON)
    monkeypatch.setattr(daemon, "harness_opened_inspect", lambda: False)
    asyncio.run(d._close_inspect_tabs([d.cdp.info(t) for t in list(d.cdp.pages)]))
    assert d.cdp.sent("Target.closeTarget") == []

    monkeypatch.setattr(daemon, "harness_opened_inspect", lambda: True)
    asyncio.run(d._close_inspect_tabs([d.cdp.info(t) for t in list(d.cdp.pages)]))
    assert list(d.cdp.pages) == ["person-tab"]


# --- lifecycle ---

def test_ping_reports_protocol_and_other_active_owners():
    d = _daemon()
    _run(d, cmd("a"), cmd("b"), meta("c", "tabs"))
    d.owners["b"].seen = 0

    me, other = _run(d, {"meta": "ping", "owner": "a"}, {"meta": "ping", "owner": "z"})

    assert me["protocol"] == other["protocol"] == daemon.ipc.PROTOCOL
    assert (me["active"], other["active"]) == (0, 1)  # b idle, c holds no tab


def test_another_agents_probe_does_not_keep_the_default_owner_active():
    d = _daemon()
    _run(d, {"method": "Input.insertText", "params": {"text": "x"}})  # a client without owner opened a tab
    d.owners["default"].seen = 0

    _, ping = _run(d, {"method": "Target.getTargets", "params": {}, "owner": "agent-7"}, {"meta": "ping", "owner": "agent-7"})

    assert ping["active"] == 0 and d.owners["default"].seen == 0


def test_ping_is_not_activity():
    d = _daemon()
    d.last = 0
    _run(d, {"meta": "ping"})
    assert d.last == 0
    _run(d, meta("a", "tabs"))
    assert d.last > 0


@pytest.mark.parametrize(("name", "kind", "required", "exits"), [
    ("work", "local", False, True), ("default", "cdp", False, True), ("rig", "cdp", False, True),
    ("default", "local", False, False),  # Chrome would ask to Allow again
    ("remote", "cloud", False, False),  # exit stops the paid browser and its logins
    ("rig", "cdp", True, False), ("work", "local", True, False),  # an orchestrator requires it
])
def test_only_unused_named_local_and_cdp_daemons_idle_exit(monkeypatch, name, kind, required, exits):
    monkeypatch.setattr(daemon, "NAME", name)
    monkeypatch.setattr(daemon, "BROWSER_KIND", kind)
    monkeypatch.setattr(daemon, "log", lambda _m: None)
    d = _daemon()
    d.stop = asyncio.Event()
    _run(d, {"meta": "ping", "keep": required})
    d.last = time.time() - daemon.IDLE_EXIT - 1

    asyncio.run(d.sweep())

    assert d.stop.is_set() is exits


def test_a_daemon_started_under_require_existing_never_idle_exits(monkeypatch):
    monkeypatch.setenv("BH_REQUIRE_EXISTING_DAEMON", "1")
    assert daemon.Daemon().keep is True


def test_idle_exit_releases_tabs_before_it_stops(monkeypatch):
    """Review B2: stop was set first, so serve() cancelled the release at its first await."""
    monkeypatch.setattr(daemon, "NAME", "work")
    monkeypatch.setattr(daemon, "log", lambda _m: None)
    monkeypatch.setattr(daemon.ipc, "cleanup_endpoint", lambda _n: None)
    monkeypatch.setattr(daemon, "SWEEP_EVERY", 0.01)

    async def serve_forever(*_a):
        await asyncio.Event().wait()
    monkeypatch.setattr(daemon.ipc, "serve", serve_forever)

    async def go():
        d = _daemon(PERSON)
        d.stop = asyncio.Event()
        for r in (cmd("a"), meta("a", "new_tab"), cmd("b"), meta("b", "new_tab")):
            await d.handle(r)
        real = d.cdp.send_raw

        async def slow(method, params=None, session_id=None):
            await asyncio.sleep(0)  # a real websocket round trip yields
            return await real(method, params, session_id)
        d.cdp.send_raw = slow
        for o in d.owners.values():
            o.seen = 0
        d.last = 0
        await asyncio.wait_for(daemon.serve(d), timeout=2)
        return d
    d = asyncio.run(go())

    assert len(d.cdp.sent("Target.detachFromTarget")) == 2 and len(d.tab_owner) == 2


def test_recent_activity_keeps_a_named_daemon(monkeypatch):
    monkeypatch.setattr(daemon, "NAME", "work")
    d = _daemon()
    d.stop = asyncio.Event()

    asyncio.run(d.sweep())

    assert not d.stop.is_set()


def test_daemon_exits_when_the_browser_connection_closes(monkeypatch):
    monkeypatch.setattr(daemon, "log", lambda _m: None)
    monkeypatch.setattr(daemon.ipc, "cleanup_endpoint", lambda _name: None)

    async def serve_forever(*_a):
        await asyncio.Event().wait()
    monkeypatch.setattr(daemon.ipc, "serve", serve_forever)

    async def go():
        d = _daemon()
        d.stop = asyncio.Event()
        d.cdp._message_handler_task = asyncio.create_task(asyncio.sleep(0.01))  # the websocket reader ends
        await asyncio.wait_for(daemon.serve(d), timeout=2)
    asyncio.run(go())


def test_a_5_mb_upload_fits_in_one_request(monkeypatch):
    """Review B3: the fallback for cloud uploads sends the file inside one IPC line."""
    if daemon.ipc.IS_WINDOWS:
        pytest.skip("unix socket path")
    runtime = tempfile.mkdtemp(prefix="bh", dir="/tmp")  # AF_UNIX paths must stay short
    monkeypatch.setattr(daemon.ipc, "_RUNTIME", daemon.ipc.Path(runtime))
    monkeypatch.setattr(daemon, "NAME", "t")
    monkeypatch.setattr(daemon, "log", lambda _m: None)
    payload = base64.b64encode(os.urandom(5 << 20)).decode()

    def upload():
        c, token = daemon.ipc.connect("t", timeout=5)
        try:
            return daemon.ipc.request(c, token, {**cmd("a", "Runtime.callFunctionOn", arguments=[{"value": [["f.pdf", payload, "application/pdf"]]}])})
        finally:
            c.close()

    async def go():
        d = _daemon()
        d.stop = asyncio.Event()
        serving = asyncio.create_task(daemon.serve(d))
        await asyncio.sleep(0.2)
        reply = await asyncio.to_thread(upload)
        d.stop.set()
        await serving
        return d, reply
    try:
        d, reply = asyncio.run(go())
    finally:
        shutil.rmtree(runtime)

    assert reply == {"result": {}}
    assert [p["arguments"][0]["value"][0][1] == payload for m, p, _s in d.cdp.calls if m == "Runtime.callFunctionOn"] == [True]


# --- owner state survives a daemon restart ---

def test_owner_tabs_survive_a_daemon_restart():
    d = _daemon(PERSON)
    _run(d, cmd("a"), cmd("b"), meta("a", "switch_tab", target_id="person-tab"),
         meta("a", "mark_tab", mark="handoff"), meta("a", "name_session", name="shop"))
    d.cdp.pages.pop("tab-3")  # b's tab closed while the daemon was down

    again = daemon.Daemon()
    again.cdp = d.cdp
    again._load(set(d.cdp.pages))
    _run(again, cmd("a"), cmd("b"))

    assert again.owners["a"].current == "person-tab"
    assert set(again.owners["a"].tabs) == {"tab-1", "person-tab"}
    assert (again.owners["a"].name, again.owners["a"].marks, again.owners["a"].created) == ("shop", {"person-tab": "handoff"}, {"tab-1"})
    assert [d.cdp.tab_of(s) for s in d.cdp.sent("Input.insertText")][-2:] == ["person-tab", "tab-7"]


def test_shutdown_closes_no_tabs(monkeypatch):
    monkeypatch.setattr(daemon, "stop_remote", lambda strict=False: True)
    d = _daemon()
    d.stop = asyncio.Event()
    _run(d, cmd("a"))

    [r] = _run(d, {"meta": "shutdown"})

    assert r == {"ok": True} and d.stop.is_set()
    assert d.cdp.sent("Target.closeTarget") == []
    assert json.loads(daemon.STATE.read_text())["a"]["tabs"] == ["tab-1"]

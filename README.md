<img src="https://raw.githubusercontent.com/browser-use/media/main/browser-harness/banner-ink.svg" alt="Browser Harness" width="100%" />

# Browser Harness ♞

An agent harness for browser control is a loop: observe the page, act, repeat. Browser Harness gives the coding agent you already use that loop in your real Chrome, over CDP.

Browser Harness lets Claude Code, Codex or Cursor use your real Chrome browser: open pages, click, type, read and download, logged in as you. Paste one prompt into your coding agent to set it up. It talks to Chrome over the DevTools Protocol (CDP), and the agent writes any missing helper as it works, so the harness gets better with each task. Open source, MIT licensed.

Try browser-harness in [Browser Use Cloud](https://cloud.browser-use.com/v4?utm_campaign=browser-harness-use-in-cloud&utm_source=github) or paste the setup prompt into your coding agent.

```
  ● agent: wants to upload a file
  │
  ● agent-workspace/agent_helpers.py → helper missing
  │
  ● agent writes it                         agent_helpers.py
  │                                                       + custom helper
  ✓ file uploaded
```

**You will never use the browser again.**

## See it work

**Task:** "Open my X profile, find my latest 20 video posts, and download them."

[![Download my latest 20 X videos](docs/download-latest-20-x-videos.gif)](https://browser-use.com/showcase/videos/download-latest-20-x-videos.mp4)

## Setup prompt

Paste into Claude Code or Codex:

```text
Install or upgrade browser-harness to the latest stable version with uv using Python 3.12, register the skill from `browser-harness skill`, and connect it to my browser. Ask whether I want local browser recordings enabled; default to no and preserve my existing preference on upgrades. Follow https://github.com/browser-use/browser-harness/blob/main/install.md if setup or connection fails.
```

The agent will open `chrome://inspect/#remote-debugging`. On first setup, tick
the checkbox so the agent can connect to your browser:

<img src="docs/setup-remote-debugging.png" alt="Remote debugging setup" width="520" style="border-radius: 12px;" />

## How it works

- [`install.md`](install.md) connects the agent to your browser.
- [`SKILL.md`](SKILL.md) teaches it the browser workflow.
- [`src/browser_harness/`](src/browser_harness/) stays protected while the agent writes reusable helpers in its local workspace.

## Browser Harness vs Playwright MCP, Playwright CLI, Chrome DevTools MCP and Claude in Chrome

| | Works with | Browser it drives | What you approve in your own Chrome |
|---|---|---|---|
| **Browser Harness** | Claude Code, Codex, Cursor, any MCP client via `browser-harness-mcp` | Your running Chrome, logged in as you | Whole browser: enable remote debugging in `chrome://inspect`, then allow the connection |
| **Playwright MCP** | Any MCP client (Claude Code, Codex, Cursor, Copilot and more) | Its own browser profile by default. Your Chrome with `--extension` and the Playwright Extension | Chosen tab: the extension asks you to pick a tab and approve the connection |
| **Playwright CLI** | Claude Code, GitHub Copilot and other agents that run shell commands | Its own browser by default. Your Chrome with `attach --extension` or `attach --cdp` | Chosen tab via the Playwright Extension, or whole browser via `--cdp` |
| **Chrome DevTools MCP** | Any MCP client (Claude Code, Cursor, Copilot, Antigravity and more), plus a CLI | A new Chrome with a dedicated profile by default. Your Chrome 144+ with `--autoConnect` | Whole browser: Chrome asks you to allow the connection, then the server sees every open window of that profile |
| **Claude in Chrome** | Claude Code and the Claude extension, on a direct Anthropic plan (Pro, Max, Team, Enterprise) | Your Chrome, Edge or other Chromium browser, logged in as you | Per site: you allow sites in the extension, and Claude works in tabs it opens |

Matched eval on 100 public [BU Bench V2.1](https://github.com/browser-use/benchmark) tasks (bu2-001 to bu2-100). Same agent for every tool (Codex CLI 0.144.6, GPT-6 Luna, medium reasoning, web search off), the same fresh Browser Use cloud browser, the same frozen judge, one run per task, no retries. Measured 2026-10-14.

| Tool | Score 0-100 (95% CI) | Median input tokens per task | Median agent cost per task | Median agent time per task |
|---|---|---|---|---|
| Browser Harness 0.1.13 | 60.4 (55.2-65.4) | 2.1M | $0.042 | 9.0 min |
| Playwright MCP 0.0.83 | 60.0 (55.1-65.0) | 4.4M | $0.066 | 8.3 min |
| Chrome DevTools MCP 1.10.1 | 61.0 (55.8-65.9) | 6.7M | $0.097 | 8.7 min |
| agent-browser 0.39.1 | 56.3 (51.1-61.5) | 3.3M | $0.056 | 11.3 min |
| Playwright CLI | not yet measured | | | |
| Claude in Chrome | not measured (runs only with Claude) | | | |

No tool wins on success: every paired difference against Browser Harness has a 95% CI that includes zero. Browser Harness used the fewest input tokens and had the lowest agent cost per task, but it was not the fastest. Measured with Codex only; Claude Code and Cursor were not measured. Method and per-task scores: [coding-agent-browser-tools.json](https://browser-use.com/benchmarks/coding-agent-browser-tools.json).

### When to use something else

- **Playwright** (Test, MCP or CLI): isolated, repeatable CI tests of your own app, in a clean browser with no personal logins.
- **Chrome DevTools MCP**: performance traces and DevTools debugging (network, console, source-mapped stack traces).
- **Claude in Chrome**: you only use Claude, on a direct Anthropic plan. It is not available through Amazon Bedrock, Google Cloud's Agent Platform or Microsoft Foundry.

OpenAI Codex has its own feature called "Browser use" (the Codex in-app browser and Chrome). It is separate from Browser Use, the company behind this repo.

## Scale with Browser Use Cloud

Use your local browser for logged-in, personal work. When you want many browsers in parallel—with live previews, proxies, stealth, CAPTCHA solving, and more—scale with [Browser Use Cloud](https://cloud.browser-use.com/new-api-key).

## MCP server

`browser-harness-mcp` exposes the browser control helpers as MCP tools over
stdio, so any MCP client (Claude Code, Devin, Cursor, etc.) can drive the
browser without writing a second CDP layer. See [docs/MCP.md](docs/MCP.md) for
setup and client configuration.

### Cursor

Add this to `.cursor/mcp.json` in your project, or to `~/.cursor/mcp.json` for
every project. It needs [uv](https://docs.astral.sh/uv/).

```json
{
  "mcpServers": {
    "browser-harness": {
      "command": "uvx",
      "args": ["--from", "browser-harness[mcp]", "browser-harness-mcp"]
    }
  }
}
```

## Contributing

Bug fixes, documentation improvements, and agent-generated domain skills are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

[The Bitter Lesson of Agent Harnesses](https://browser-use.com/posts/bitter-lesson-agent-harnesses) · [Web Agents That Actually Learn](https://browser-use.com/posts/web-agents-that-actually-learn)

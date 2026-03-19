# CLAUDE.md — Instructions for Claude Code

This file tells Claude Code everything it needs to know about the ClaudeBud project.

---

## Project Overview

ClaudeBud is a self-hosted tool that lets a user monitor and control Claude Code CLI sessions from their Android phone.

It works as a **drop-in replacement for the `claude` command**. The user types `claudebud` instead of `claude` — everything else is identical. Under the hood, ClaudeBud wraps Claude Code in a pty process, monitors its output, and exposes a PWA web app on the local network so the user can watch and interact with sessions from their phone.

**Core value props:**
- Push notifications when Claude finishes or needs input
- Live terminal view on phone
- Minimal keyboard (Up, Down, Enter) for quick responses
- Multi-session tabs — one per `claudebud` invocation
- Works remotely via Tailscale

---

## First Task: Create a Comprehensive Build Plan

Before writing any code, create a file called `BUILD_PLAN.md` that covers:

1. Full project directory structure (all files and folders)
2. Detailed spec for each component
3. Data flow diagram (text-based)
4. All dependencies with versions
5. Build and packaging plan (how pip install will work)
6. Testing plan
7. Ordered implementation steps
8. Known risks or tricky parts

Only after `BUILD_PLAN.md` is approved should implementation begin.

---

## User Workflow

```bash
# First time only:
pip install claudebud
claudebud setup        # configures ntfy topic, sets up autostart

# Every day — just replace 'claude' with 'claudebud':
claudebud              # starts exactly like 'claude' would
                       # daemon starts automatically in background if not running
                       # new tab appears in phone app as "Terminal 1"

# In a second terminal:
claudebud              # appears as "Terminal 2"

# Rename a session from inside the terminal:
/name catheter-tracker # tab immediately renames in phone app

# Or tap the tab name in the phone app to rename inline
```

The user never manages the daemon directly. It starts automatically when `claudebud` is first invoked and keeps running in the background.

---

## Architecture

```
User types 'claudebud' in any terminal
        │
        ▼
ClaudeBud CLI (pty wrapper)
  - spawns 'claude' as a child pty process
  - proxies all input/output transparently
  - intercepts /name slash command
  - registers session with daemon
  - streams output to daemon via local socket
        │
        ▼
ClaudeBud Daemon (background process, one instance)
  - FastAPI + WebSockets
  - manages all active sessions
  - detects prompts and task completion in output
  - sends push notifications via ntfy.sh
  - serves PWA frontend
  - receives input from PWA and forwards to correct pty
        │
        ▼ (local network or Tailscale IP)
ClaudeBud PWA (Chrome on Android)
  - session tabs
  - live terminal display per session
  - minimal keyboard: Up / Down / Enter
  - full keyboard toggle
  - tap tab name to rename
```

---

## Tech Stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.8+ | pip installable, cross-platform |
| pty wrapper | Python `pty` module (Unix) / `winpty` (Windows) | Transparent terminal proxying |
| Web framework | FastAPI | Async, built-in WebSocket, lightweight |
| Push notifications | ntfy.sh HTTP API | Free, no account, great Android app |
| Frontend | Single-file Vanilla JS PWA | No build step, served directly by daemon |
| Config | JSON at `~/.claudebud/config.json` | Simple, user-editable |
| Remote access | Tailscale (user sets up independently) | No code changes needed |

---

## Repository Structure

```
claudebud/
├── README.md
├── CLAUDE.md
├── BUILD_PLAN.md              # created first, before any code
├── pyproject.toml
├── claudebud/
│   ├── __init__.py
│   ├── cli.py                 # entry point: 'claudebud' and 'claudebud setup'
│   ├── daemon.py              # FastAPI server, WebSocket hub, session registry
│   ├── session.py             # pty wrapper, output streaming, /name interception
│   ├── notifier.py            # ntfy.sh integration
│   ├── detector.py            # prompt and completion pattern detection
│   ├── config.py              # config file read/write
│   └── static/
│       └── index.html         # entire PWA frontend in one file
├── tests/
│   ├── test_detector.py
│   ├── test_notifier.py
│   └── test_session.py
└── docs/
    └── setup.md
```

---

## Component Specs

### 1. CLI (`cli.py`)

Entry point installed by pip as `claudebud`.

**Commands:**
- `claudebud` — main command, wraps `claude`. Accepts all the same arguments and flags that `claude` accepts and passes them through transparently.
- `claudebud setup` — interactive first-time setup (ntfy topic, autostart)

**Startup sequence when user runs `claudebud`:**
1. Check if daemon is running. If not, start it in the background silently.
2. Assign a session ID (UUID) and a default display name ("Terminal N" where N is the next available number).
3. Register the session with the daemon via local HTTP.
4. Spawn `claude` (with any passthrough args) as a pty child process.
5. Proxy stdin/stdout/stderr transparently — user sees exactly what they'd see with `claude`.
6. Intercept `/name <value>` — do not pass to claude, handle locally (update session name, notify daemon).
7. On exit, unregister session from daemon.

### 2. Daemon (`daemon.py`)

Single background process, one instance per machine.

FastAPI app that:
- Serves PWA at `GET /`
- Serves manifest at `GET /manifest.json`
- WebSocket at `ws://host:port/ws` — multiplexed, all sessions over one connection
- `POST /sessions/register` — called by CLI on startup
- `POST /sessions/unregister` — called by CLI on exit
- `POST /sessions/{id}/input` — receives input from PWA, forwards to correct pty
- `GET /sessions` — list active sessions with names and status
- `POST /sessions/{id}/rename` — rename a session

**WebSocket message format:**

All messages are JSON with a `type` and `session_id` field.

Server → client:
```json
{ "type": "output", "session_id": "uuid", "data": "terminal text chunk" }
{ "type": "session_added", "session_id": "uuid", "name": "Terminal 1" }
{ "type": "session_removed", "session_id": "uuid" }
{ "type": "session_renamed", "session_id": "uuid", "name": "catheter-tracker" }
{ "type": "session_status", "session_id": "uuid", "status": "prompt|running|idle" }
```

Client → server:
```json
{ "type": "input", "session_id": "uuid", "data": "\r" }
{ "type": "rename", "session_id": "uuid", "name": "new-name" }
```

### 3. Session Manager (`session.py`)

- Spawns `claude` as a pty child process
- Proxies all terminal I/O transparently
- Intercepts `/name <value>` before passing input to claude
- Streams output chunks to daemon via loopback HTTP POST
- Handles terminal resize events (SIGWINCH) and forwards to pty

### 4. Detector (`detector.py`)

Analyzes output chunks to classify events:
- `PROMPT` — Claude is waiting for user input → trigger notification
- `COMPLETE` — Claude finished a task → trigger notification
- `NORMAL` — regular output

Default patterns (all configurable):
```python
PROMPT_PATTERNS = [
    r"\(Y/n\)", r"\(y/N\)", r"\(yes/no\)",
    r"Allow", r"Approve", r"Do you want to",
    r"Press Enter", r"Continue\?",
]

COMPLETION_PATTERNS = [
    r"✓ Completed", r"Task complete", r"Done\.",
    r"Finished", r"All done",
]
```

Debounce notifications — don't fire twice for the same prompt event.

### 5. Notifier (`notifier.py`)

```python
async def notify(topic: str, title: str, message: str, server: str = "https://ntfy.sh"):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{server}/{topic}",
            content=message,
            headers={"Title": title, "Priority": "high"}
        )
```

Notification types:
- PROMPT: title = `"⚠️ Claude needs input"`, message = session name + last output line
- COMPLETE: title = `"✅ Claude finished"`, message = session name

### 6. Frontend PWA (`static/index.html`)

Single HTML file. No build step. No frameworks. Vanilla JS + CSS only.

**Layout:**
- **Tab bar** — one tab per active session. Default names: "Terminal 1", "Terminal 2" etc. Custom name shown if renamed. Tap tab name to edit inline.
- **Terminal area** — monospace output display, dark background, auto-scrolls to bottom
- **Bottom keyboard bar** — default minimal keyboard:

```
[ ↑ ]   [ ↓ ]   [ Enter ]   [ ⌨️ ]
```

Arrow keys send ANSI escape sequences (`\x1b[A` for up, `\x1b[B` for down).
Enter sends `\r`.
`⌨️` button toggles full keyboard (a textarea + Send button).

**Session tab behavior:**
- Tabs appear/disappear automatically as sessions start/stop
- Active tab highlighted
- Tap tab name → becomes an inline text input → confirm with Enter or blur
- Each tab maintains its own scrollback buffer in memory (up to `max_scrollback_lines`)
- Switching tabs shows that session's buffered output

**WebSocket behavior:**
- Single persistent connection to `ws://host:port/ws`
- Reconnects automatically on disconnect (exponential backoff, max 30s)
- All session multiplexing handled via `session_id` in messages
- Connection status shown as colored dot in top bar (green = connected, red = disconnected)

**PWA requirements:**
- `manifest.json` served at `/manifest.json` by daemon
- App name: "ClaudeBud"
- Theme: dark (`#1a1a1a`)
- Icons: emoji-based SVG is fine for MVP
- `viewport` meta tag for mobile
- Basic service worker for offline fallback page

**Visual style:**
- Background: `#1a1a1a`
- Terminal text: `#00ff88` (green on dark)
- Buttons: large, minimum 56px height, high contrast
- Tab bar: `#2a2a2a` with active tab highlight
- Font: monospace throughout

---

## Session Naming Logic

- Sessions are numbered sequentially: Terminal 1, Terminal 2, Terminal 3...
- Numbers increment globally and are never recycled (if Terminal 2 closes, next new one is Terminal 4)
- Once a session is manually renamed (via `/name` or tap-to-rename in PWA), it displays the custom name
- Custom name is stored in the daemon's session registry
- Custom name persists for the lifetime of that session only (not across restarts)

---

## Configuration

File: `~/.claudebud/config.json`

```json
{
  "port": 3131,
  "ntfy_topic": "",
  "ntfy_server": "https://ntfy.sh",
  "prompt_patterns": ["(Y/n)", "(y/N)", "Allow", "Approve"],
  "completion_patterns": ["✓ Completed", "Task complete"],
  "max_scrollback_lines": 2000
}
```

On first run, if config doesn't exist, create defaults and prompt user to run `claudebud setup`.

---

## Setup Command (`claudebud setup`)

Interactive CLI wizard:
1. Ask for ntfy topic name → save to config
2. Print ntfy app install instructions
3. Offer to set up autostart:
   - **Mac:** add to `launchd` plist
   - **Linux:** add to `systemd` user service
   - **Windows/WSL:** add to WSL autostart
4. Print the local URL to open on phone: `http://<local-ip>:3131`
5. Print Tailscale setup instructions (optional step)

---

## Packaging

`pyproject.toml`:
- Package name: `claudebud`
- Entry point: `claudebud = claudebud.cli:main`
- Dependencies: `fastapi`, `uvicorn[standard]`, `httpx`, `click`
- Python requires: `>=3.8`
- Include `claudebud/static/` in package data

---

## Platform Notes

- Primary platforms: **Mac, Linux, Windows (WSL)**
- `pty` module is Unix-only. On Windows, require WSL. Print a clear error message if run outside WSL on Windows.
- Daemon binds to `0.0.0.0` so it's reachable on all interfaces (local network + Tailscale)
- Auto-detect local IP for display in setup instructions

---

## Implementation Order

1. Project scaffold (pyproject.toml, folder structure, `__init__.py` files)
2. Config system (`config.py`)
3. Detector (`detector.py` — pure logic, easiest to test first)
4. Notifier (`notifier.py`)
5. Daemon skeleton (`daemon.py` — FastAPI, session registry, WebSocket hub)
6. Session manager (`session.py` — pty wrapper, output streaming to daemon)
7. CLI entry point (`cli.py` — daemon autostart, session registration, pty proxy)
8. Frontend PWA (`index.html` — tabs, terminal display, minimal keyboard)
9. Wire end-to-end and test full flow
10. Setup command (`claudebud setup`)
11. Autostart support (Mac / Linux / WSL)
12. Tests
13. README polish

---

## Out of Scope for MVP

- iOS support
- Native Android APK
- Authentication / password protection (Tailscale handles this)
- Session recording / playback
- Windows native without WSL
- Support for other CLI agents (future: Gemini CLI, Cursor, etc.)

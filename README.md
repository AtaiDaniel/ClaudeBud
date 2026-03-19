# ClaudeBud

[![PyPI version](https://img.shields.io/pypi/v/claudebud)](https://pypi.org/project/claudebud/)
[![Python](https://img.shields.io/pypi/pyversions/claudebud)](https://pypi.org/project/claudebud/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Monitor and control [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started) CLI sessions from your phone.

ClaudeBud is a **drop-in replacement for the `claude` command**. Type `claudebud` instead of `claude` — everything else is identical. Under the hood it wraps Claude Code in a pty, streams output to a background daemon, and serves a PWA web app on your local network so you can watch and interact with sessions from your phone.

## Features

- **Push notifications** when Claude finishes or needs your input (browser-native Web Push, no app needed)
- **Live terminal view** on your phone with full session history
- **Multi-session tabs** — one tab per `claudebud` invocation, with custom names via `-n`
- **Configurable keyboard** — customisable button grid + full virtual PC keyboard
- **Remote access** via [Tailscale](https://tailscale.com) with no extra config
- Works on **Windows**, **macOS**, and **Linux / WSL**

## Install

```bash
pip install claudebud
claudebud setup
```

Requires Python 3.8+ and [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started).

## Quick start

```bash
# Drop-in replacement for 'claude':
claudebud

# Name the session tab on your phone:
claudebud -n my-project

# All claude flags pass through unchanged:
claudebud --model claude-opus-4-5 -p "summarise this file" < notes.txt

# First-time setup (autostart, local URL):
claudebud setup
```

## How it works

```
claudebud (any terminal)
    │  spawns claude in a pty, proxies I/O transparently
    │  streams output to daemon over loopback HTTP
    ▼
ClaudeBud Daemon  (background, auto-started)
    │  FastAPI + WebSockets
    │  detects prompts and completions, sends Web Push notifications
    │  serves the PWA frontend
    ▼
ClaudeBud PWA  (phone browser / home screen icon)
    session tabs · live terminal · configurable keyboard
```

The daemon starts automatically the first time you run `claudebud` and keeps running in the background. You never manage it directly.

## Phone setup

1. Run `claudebud setup` — it prints a URL like `http://192.168.1.42:3131`
2. Open that URL in Chrome on your phone
3. Tap **🔔** in the top bar → **Enable notifications** and accept the browser prompt
4. Tap the browser menu → **Add to Home Screen** for a PWA icon

For detailed instructions see [docs/setup.md](docs/setup.md).

For access from outside your home network, use [Tailscale](https://tailscale.com) and open the URL with your machine's Tailscale IP instead.

## Notifications

Uses the browser's built-in **Web Push API** — no external app or account required. Works in Chrome on Android; Safari on iOS requires iOS 16.4+ with the app added to the home screen.

You'll receive:
- **⚠️ Claude needs input** — when Claude is waiting for your approval
- **✅ Claude finished** — when a task completes

## Configuration

`~/.claudebud/config.json` — created automatically on first run:

```json
{
  "port": 3131,
  "prompt_patterns": ["(Y/n)", "(y/N)", "Allow", "Approve"],
  "completion_patterns": ["Completed", "Task complete"],
  "max_scrollback_lines": 2000
}
```

## Autostart

`claudebud setup` configures the daemon to start on login:

| Platform | Method |
|---|---|
| macOS | launchd plist (`~/Library/LaunchAgents/`) |
| Linux | systemd user service |
| WSL | `.bashrc` / `.zshrc` guard |
| Windows | Startup folder batch script |

Without autostart, the daemon starts automatically on the first `claudebud` invocation.

## Remote access with Tailscale

1. Install Tailscale on both your machine and phone
2. Run `claudebud setup` and note the local URL
3. Replace the local IP with your machine's Tailscale IP
4. Open `http://<tailscale-ip>:3131` on your phone — done

## License

MIT

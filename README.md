# ClaudeBud

[![PyPI version](https://img.shields.io/pypi/v/claudebud)](https://pypi.org/project/claudebud/)
[![Python](https://img.shields.io/pypi/pyversions/claudebud)](https://pypi.org/project/claudebud/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Web control and push notifications for [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started) terminals.**

If you run Claude Code across multiple projects or terminal windows, you know the problem: you have to keep checking each window to see if Claude finished or needs your approval. ClaudeBud fixes that.

Replace `claude` with `claudebud` and every session gets a tab in a phone-accessible web app. The moment any session finishes a task or asks for your input, you get a push notification — no matter which terminal it came from, and no matter where you are. Tap the notification to open that session and respond in seconds.

ClaudeBud is a **drop-in replacement for the `claude` command** — all flags and arguments pass through unchanged. A background daemon starts automatically and keeps running between sessions.

## Features

- **Push notifications** — get alerted the instant Claude needs input or finishes, across every open session, no matter which terminal it ran in
- **Multi-session tabs** — one tab per `claudebud` invocation, named automatically or via `-n`; switch between sessions without hunting for the right window
- **Live terminal view** — full scrollback per session, readable on your phone
- **Quick-response keyboard** — Up / Down / Enter for common approvals; toggle to full keyboard for anything else
- **Remote access** via [Tailscale](https://tailscale.com) *(optional)* — monitor sessions from outside your network with automatic HTTPS

## Install

```bash
pip install claudebud
claudebud setup
```

Requires Python 3.8+ and [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started).

For a full step-by-step walkthrough — including autostart, phone setup, notifications, and optional Tailscale access — see [docs/setup.md](docs/setup.md).

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
3. Tap the browser menu → **Add to Home Screen** to install as a PWA (required for push notifications)
4. Open the installed PWA icon, tap **🔔** in the top bar → **Enable notifications** and accept the browser prompt

> **Push notifications require the PWA to be installed to the home screen.** Browser tabs cannot receive Web Push on Android — you must use the Add to Home Screen / install flow first.

For detailed instructions see [docs/setup.md — Phone setup](docs/setup.md#4-open-the-web-app-on-your-phone).

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
  "prompt_patterns": ["(Y/n)", "(y/N)", "(yes/no)", "Allow", "Approve",
                       "Do you want to", "Press Enter", "Continue?"],
  "completion_patterns": ["Completed", "Task complete", "Done.",
                           "Finished", "All done"],
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

## Remote access with Tailscale *(optional)*

ClaudeBud works on your local Wi-Fi without Tailscale. **Tailscale is only needed if you want to access your sessions from outside your home network** — e.g. from work or on mobile data.

[Tailscale](https://tailscale.com) creates a private encrypted tunnel between your devices with no port forwarding or firewall changes. It also provides automatic HTTPS certificates, which are required for push notifications over an external connection.

**Quick setup:**

1. Create a free account at [tailscale.com](https://tailscale.com) and install Tailscale on your PC:
   - **Windows:** [tailscale.com/download/windows](https://tailscale.com/download/windows)
   - **macOS:** Mac App Store or [tailscale.com/download/mac](https://tailscale.com/download/mac)
   - **Linux:** `curl -fsSL https://tailscale.com/install.sh | sh` then `sudo tailscale up`
2. Install the **Tailscale** app on your phone (Android/iOS — search in your app store) and sign in with the same account
3. In the [Tailscale admin console → DNS](https://login.tailscale.com/admin/dns), scroll to **HTTPS Certificates** and toggle it on
4. Restart the ClaudeBud daemon: close all `claudebud` sessions and open a new one
5. Open `https://<your-machine-name>.tail<xxxxx>.ts.net:3131` on your phone

Your machine name appears in the [Tailscale admin console → Machines](https://login.tailscale.com/admin/machines) tab, or run `tailscale status` in a terminal. ClaudeBud also prints the Tailscale URL in the startup banner once HTTPS is active.

> **Switching from HTTP to HTTPS:** After enabling Tailscale HTTPS, re-enable notifications in the app — push subscriptions are tied to the origin (`http://` vs `https://`). Open the new `https://` URL, tap 🔔, disable then re-enable.

For a full walkthrough see [docs/setup.md](docs/setup.md#8-remote-access-with-tailscale-optional).

## License

MIT

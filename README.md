# ClaudeBud

Monitor and control [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started) CLI sessions from your phone.

ClaudeBud is a **drop-in replacement for the `claude` command**. Type `claudebud` instead of `claude` — everything else is identical. Under the hood it wraps Claude Code in a pty, streams output to a background daemon, and serves a PWA web app on your local network so you can watch and interact with sessions from your phone.

## Features

- **Push notifications** when Claude finishes or needs your input (via [ntfy](https://ntfy.sh))
- **Live terminal view** on your phone
- **Multi-session tabs** — one tab per `claudebud` invocation, with custom names via `-n`
- **Minimal phone keyboard** — Up / Down / Enter plus a full-keyboard toggle
- **Remote access** via [Tailscale](https://tailscale.com) with no extra config
- Works on **Windows**, **macOS**, and **Linux / WSL**

## Install

```bash
pip install claudebud
claudebud setup
```

Requires Python 3.8+ and [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started).

## Usage

```bash
# Drop-in replacement for 'claude':
claudebud

# Name the session tab on your phone:
claudebud -n my-project

# All claude flags pass through unchanged:
claudebud --model claude-opus-4-5 -p "summarise this file" < notes.txt

# First-time setup (ntfy topic, autostart):
claudebud setup

# Update claudebud (and optionally claude):
claudebud update
```

## How it works

```
claudebud (any terminal)
    │  spawns claude in a pty, proxies I/O transparently
    │  streams output to daemon over loopback HTTP
    ▼
ClaudeBud Daemon  (background, auto-started)
    │  FastAPI + WebSockets
    │  detects prompts and completions, sends ntfy notifications
    │  serves the PWA frontend
    ▼
ClaudeBud PWA  (phone browser / home screen icon)
    session tabs · live terminal · Up/Down/Enter keyboard
```

The daemon starts automatically the first time you run `claudebud` and keeps running in the background. You never manage it directly.

## Phone setup

1. Run `claudebud setup` — it prints a URL like `http://192.168.1.42:3131`
2. Open that URL in Chrome on your phone
3. Tap the browser menu → **Add to Home Screen** for a PWA icon
4. Subscribe to your ntfy topic in the [ntfy app](https://ntfy.sh)

For access from outside your home network, use [Tailscale](https://tailscale.com) and open the URL with your machine's Tailscale IP instead.

## Notifications

Uses [ntfy.sh](https://ntfy.sh) — free and open-source, no account required.

```bash
claudebud setup   # enter a unique topic name, e.g. "alice-claude-7x3k"
```

Install ntfy on your phone and subscribe to the same topic. You'll receive:
- **⚠️ Claude needs input** — when Claude is waiting for approval
- **✅ Claude finished** — when a task completes

Use a long random topic name to keep it private.

## Configuration

`~/.claudebud/config.json` — created automatically on first run:

```json
{
  "port": 3131,
  "ntfy_topic": "",
  "ntfy_server": "https://ntfy.sh",
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

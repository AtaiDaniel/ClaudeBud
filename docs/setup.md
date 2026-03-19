# ClaudeBud Setup Guide

Step-by-step walkthrough for getting ClaudeBud running on your machine and phone.

---

## 1. Install Claude Code

If you haven't already, install Claude Code using the native installer:

```
https://docs.anthropic.com/en/docs/claude-code/getting-started
```

Verify it works: `claude --version`

---

## 2. Install ClaudeBud

ClaudeBud is available on [PyPI](https://pypi.org/project/claudebud/):

```bash
pip install claudebud
```

Verify: `claudebud --version`

To upgrade to the latest version at any time:

```bash
pip install --upgrade claudebud
```

---

## 3. Run the setup wizard

```bash
claudebud setup
```

The wizard will:
- Offer to configure autostart
- Print the URL to open on your phone

---

## 4. Enable push notifications

ClaudeBud uses the browser's built-in **Web Push API** — no external app or account needed.

1. Open the PWA on your phone (the URL printed by `claudebud setup`)
2. Tap the **Enable Notifications** button in the top bar
3. Accept the browser permission prompt

That's it. You'll receive a notification whenever Claude needs input or finishes a task.

---

## 5. Open the web app on your phone

After the wizard runs, it prints a URL like:

```
http://192.168.1.42:3131
```

Open that URL in **Chrome** on your phone. You should see the ClaudeBud tab UI.

To install as a PWA (home screen icon):
- **Android/Chrome**: tap the ⋮ menu → **Add to Home Screen**
- **iOS/Safari**: tap the Share icon → **Add to Home Screen**

---

## 6. Start a session

Back on your PC, instead of `claude`, type:

```bash
claudebud
```

A new tab appears in the phone app labelled **Terminal 1**. Everything else works exactly like `claude`.

To give the tab a custom name:

```bash
claudebud -n my-project
```

---

## 7. Autostart (optional)

The daemon starts automatically the first time you run `claudebud` in any terminal. If you want it to start on login (so the phone app works even before you open a terminal), run `claudebud setup` and answer **y** when asked about autostart.

### macOS
A `launchd` plist is written to `~/Library/LaunchAgents/sh.claudebud.plist`.
Load it immediately with: `launchctl load ~/Library/LaunchAgents/sh.claudebud.plist`

### Linux (systemd)
A user service is written to `~/.config/systemd/user/claudebud.service`.
Enable it with: `systemctl --user enable --now claudebud`

### WSL
A startup guard is appended to `~/.bashrc` (or `~/.zshrc`). The daemon starts when you open a new WSL terminal.

### Windows
A batch file is added to `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`.
The daemon starts on your next Windows login.

---

## 8. Remote access with Tailscale (optional)

To monitor sessions from outside your home network:

1. Install [Tailscale](https://tailscale.com) on your PC
2. Install the Tailscale app on your phone
3. Sign in with the same account on both
4. Find your PC's Tailscale IP: `tailscale ip -4`
5. Open `http://<tailscale-ip>:3131` on your phone

No port forwarding or firewall changes needed.

---

## Troubleshooting

**The phone app shows "No active sessions"**
Make sure you've run `claudebud` (not `claude`) in a terminal after opening the web app.

**Notifications aren't arriving**
- Open the PWA on your phone and make sure you tapped **Enable Notifications** and granted browser permission

**The daemon didn't start**
Run `claudebud` in a terminal — it starts the daemon automatically.
Check the log at `~/.claudebud/daemon.log` (when using autostart).

**Port 3131 is already in use**
Edit `~/.claudebud/config.json` and change `"port"` to a free port (e.g. 3132).
Restart the daemon and update the URL on your phone.

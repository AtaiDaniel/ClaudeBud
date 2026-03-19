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

## 4. Open the web app on your phone

After the wizard runs, it prints a URL like:

```
http://192.168.1.42:3131
```

Open that URL in **Chrome** on your phone. You should see the ClaudeBud tab UI.

To install as a PWA (home screen icon):
- **Android/Chrome**: tap the ⋮ menu → **Add to Home Screen**
- **iOS/Safari**: tap the Share icon → **Add to Home Screen**

> **Installing to the home screen is required for push notifications.** A browser tab alone cannot receive Web Push on Android — you must go through the install flow.

---

## 5. Enable push notifications

ClaudeBud uses the browser's built-in **Web Push API** — no external app or account needed.

1. Open the installed PWA from your home screen (not a regular browser tab)
2. Tap the **🔔** button in the top bar to open notification settings
3. Tap **Enable notifications**
4. Accept the browser permission prompt when it appears

You'll receive:
- **⚠️ Claude needs input** — when Claude is waiting for your approval
- **✅ Claude finished** — when a task completes

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

## 8. Remote access with Tailscale *(optional)*

> **Skip this section if you only use ClaudeBud at home on Wi-Fi.** Everything works without Tailscale on a local network.
>
> **You need Tailscale only if you want to access your sessions from outside your home network** — e.g. from work, on mobile data, or while travelling. Tailscale also provides automatic HTTPS certificates, which are required for push notifications over an external connection.

---

### What is Tailscale?

[Tailscale](https://tailscale.com) creates a private encrypted network (a "tailnet") between your devices using WireGuard. Once both your PC and phone are on the same tailnet, your phone can reach your PC at a stable DNS name from anywhere — no port forwarding, no firewall changes, no dynamic DNS. Tailscale also provides free Let's Encrypt certificates for your machine, which ClaudeBud uses automatically.

It is free for personal use (up to 3 users, 100 devices).

---

### Step-by-step Tailscale setup

#### 1. Create a Tailscale account

Go to [tailscale.com](https://tailscale.com) and sign up. You can use a Google, Microsoft, GitHub, or Apple account — no new password needed.

#### 2. Install Tailscale on your PC

| Platform | Instructions |
|---|---|
| **Windows** | Download the installer from [tailscale.com/download/windows](https://tailscale.com/download/windows) and run it. A Tailscale icon appears in the system tray. Click it → **Log in**. |
| **macOS** | Install from the [Mac App Store](https://apps.apple.com/app/tailscale/id1475387142) or download from [tailscale.com/download/mac](https://tailscale.com/download/mac). Click the menu-bar icon → **Log in**. |
| **Linux** | Run the install script from [tailscale.com/download/linux](https://tailscale.com/download/linux), then `sudo tailscale up`. Follow the login link it prints. |
| **WSL** | Install Tailscale in Windows (not WSL). The Windows Tailscale client handles connectivity for the whole machine including WSL. |

After logging in, your machine appears in the [Tailscale admin console → Machines](https://login.tailscale.com/admin/machines).

#### 3. Install Tailscale on your phone

- **Android:** Search for **Tailscale** in the Play Store and install it
- **iOS:** Search for **Tailscale** in the App Store and install it

Open the app and sign in with the **same account** you used on your PC. You should see your PC listed as a connected device.

#### 4. Enable HTTPS certificates

This is what gives ClaudeBud a trusted certificate (so push notifications work and your browser doesn't show a security warning).

1. Open [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
2. Scroll down to the **HTTPS Certificates** section
3. Click **Enable HTTPS** (you may need to enable MagicDNS first if prompted — accept the suggestion)

#### 5. Find your machine's Tailscale address

Run this in a terminal on your PC:

```bash
tailscale status
```

Look for your machine's row. The address looks like:
```
mypc.tail12345.ts.net   100.x.x.x   ...
```

Or check the [Machines tab](https://login.tailscale.com/admin/machines) in the admin console — the name is shown there.

Your ClaudeBud URL will be:
```
https://mypc.tail12345.ts.net:3131
```

ClaudeBud also prints this URL in the startup banner once HTTPS is active.

#### 6. Restart the ClaudeBud daemon

ClaudeBud obtains its certificate when the daemon starts. After enabling HTTPS certificates in step 4, restart the daemon so it picks up the cert:

```bash
# Close any running claudebud sessions, then open a new one:
claudebud
```

Check the daemon log to confirm:

```bash
# macOS / Linux / WSL:
tail ~/.claudebud/daemon.log
```

You should see:
```
INFO HTTPS enabled via Tailscale cert for mypc.tail12345.ts.net
INFO ClaudeBud daemon starting on https://0.0.0.0:3131
```

#### 7. Open the app on your phone

Open `https://mypc.tail12345.ts.net:3131` in Chrome on your phone. The connection goes over Tailscale — no open ports, no exposure to the internet.

---

### Switching from HTTP to HTTPS

If you were already using ClaudeBud over HTTP and you now enable Tailscale HTTPS, you need to re-register for push notifications:

1. Open the **new `https://` URL** on your phone (not the old `http://` one)
2. Re-install the PWA if needed (Add to Home Screen)
3. Tap **🔔** → **Disable notifications**, then **Enable notifications** again

This is necessary because the browser's push subscription is tied to the origin (`http://` vs `https://`) — the old subscription is no longer valid.

---

### Tailscale troubleshooting

**"HTTPS not working / ERR_SSL_PROTOCOL_ERROR"**
- Confirm HTTPS Certificates is enabled at [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
- Check `~/.claudebud/daemon.log` for `tailscale cert` errors
- Restart the daemon after enabling HTTPS in the admin console

**"My machine doesn't appear in Tailscale"**
- Make sure you're signed in to the same Tailscale account on both devices
- On Windows, check the system tray — Tailscale should show as Connected
- On Linux, run `sudo tailscale status` to verify

**"Connection times out from phone"**
- Verify both devices are shown as connected in the [Machines tab](https://login.tailscale.com/admin/machines)
- Try pinging your PC from the phone app (Tailscale app → select machine → ping)
- Make sure the daemon is running: open a terminal and run `claudebud`

---

## Troubleshooting

**The phone app shows "No active sessions"**
Make sure you've run `claudebud` (not `claude`) in a terminal after opening the web app.

**Notifications aren't arriving**
- Make sure the PWA is **installed to your home screen** (Add to Home Screen) — push notifications don't work from a regular browser tab on Android
- Open the PWA from the home screen icon, tap 🔔 → Enable notifications, and accept the permission prompt
- If you recently switched from HTTP to HTTPS: disable and re-enable notifications so a fresh subscription is registered for the new origin
- Check `~/.claudebud/daemon.log` — push service errors are logged there

**"Enable notifications" shows permission denied**
On Android, this means the system notification permission was denied for the browser. Go to Android Settings → Apps → Chrome (or Edge) → Notifications → enable it, then retry.

**HTTPS not working / ERR_SSL_PROTOCOL_ERROR**
- Check `~/.claudebud/daemon.log` — look for `tailscale cert` errors
- Make sure HTTPS Certificates is enabled in the [Tailscale admin console](https://login.tailscale.com/admin/dns)
- Restart the daemon after enabling HTTPS certificates in the admin console

**The daemon didn't start**
Run `claudebud` in a terminal — it starts the daemon automatically.
Check the log at `~/.claudebud/daemon.log` for errors.

**Port 3131 is already in use**
Edit `~/.claudebud/config.json` and change `"port"` to a free port (e.g. 3132).
Restart the daemon and update the URL on your phone.

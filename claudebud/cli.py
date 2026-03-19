"""
cli.py — entry point: 'claudebud' and 'claudebud setup'.
"""
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

from . import __version__
from .config import get_config_path, load_config, save_config


# On Windows, claude may be an npm .cmd file (old) or a native .exe (new installer).
# shell=True handles both: .cmd files need the shell, .exe files work with it too.
_SHELL = sys.platform == "win32"


def _run_claude(args: list, **kwargs) -> subprocess.CompletedProcess:
    """Run claude as a subprocess, handling Windows .cmd resolution."""
    return subprocess.run(["claude"] + args, shell=_SHELL, **kwargs)


def _claude_available() -> bool:
    return shutil.which("claude") is not None


# ── Daemon management ──────────────────────────────────────────────────────────

def is_daemon_running(port: int) -> bool:
    for scheme in ("https", "http"):
        try:
            r = httpx.get(
                f"{scheme}://127.0.0.1:{port}/sessions",
                timeout=1.0,
                verify=False,
            )
            if r.status_code == 200:
                return True
        except Exception:
            continue
    return False


def _daemon_base_url(port: int) -> str:
    """Return the loopback base URL the daemon is actually serving on."""
    for scheme in ("https", "http"):
        try:
            r = httpx.get(
                f"{scheme}://127.0.0.1:{port}/sessions",
                timeout=1.0,
                verify=False,
            )
            if r.status_code == 200:
                return f"{scheme}://127.0.0.1:{port}"
        except Exception:
            continue
    return f"http://127.0.0.1:{port}"


def start_daemon(port: int) -> None:
    """Launch the daemon as a fully detached background process."""
    kwargs = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP ensures the daemon is
        # not killed when the launching terminal window closes.
        DETACHED_PROCESS      = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
        kwargs["close_fds"] = True

    subprocess.Popen(
        [sys.executable, "-m", "claudebud.daemon"],
        **kwargs,
    )
    # Wait up to 4 seconds for it to be ready
    for _ in range(13):
        time.sleep(0.3)
        if is_daemon_running(port):
            return
    print("[claudebud] Warning: daemon did not start in time.", file=sys.stderr)


def ensure_daemon(port: int) -> None:
    if not is_daemon_running(port):
        start_daemon(port)


# ── Main command ───────────────────────────────────────────────────────────────

def run_claude(passthrough_args: list, session_name: str = None) -> None:
    """Wrap 'claude' with pty proxying and daemon integration."""
    # Import here so Windows users see the error only when running, not on import
    from .session import Session

    cfg = load_config()
    port = cfg["port"]

    ensure_daemon(port)

    base_url = _daemon_base_url(port)
    session_id = str(uuid.uuid4())

    try:
        payload = {"session_id": session_id}
        if session_name:
            payload["name"] = session_name
        httpx.post(
            f"{base_url}/sessions/register",
            json=payload,
            timeout=5.0,
            verify=False,
        )
    except Exception as e:
        print(f"[claudebud] Warning: could not register session: {e}", file=sys.stderr)

    # With -n: lock the tab title to the given name.
    # Without -n: set an initial title of "claudebud" but let claude overwrite it.
    display_title = session_name or "claudebud"
    sys.stdout.write(f"\x1b]0;{display_title}\x07")
    sys.stdout.flush()

    # Pass terminal_title only when -n was given so session.py intercepts
    # and replaces any title escapes claude emits.  Without -n we pass None
    # so claude's own title sequences flow through unchanged.
    local_ip = _get_local_ip()
    tailscale_fqdn = _get_tailscale_fqdn()
    # Use the scheme the daemon is actually serving (http or https).
    # _daemon_base_url probes loopback to detect it.
    scheme = "https" if base_url.startswith("https") else "http"
    local_url = f"{scheme}://{local_ip}:{port}"
    if tailscale_fqdn:
        # HTTPS certs are issued for the hostname; use raw IP only for HTTP.
        ts_host = tailscale_fqdn if scheme == "https" else _get_tailscale_ip()
        tailscale_url = f"{scheme}://{ts_host}:{port}" if ts_host else ""
    else:
        tailscale_url = ""
    session = Session(
        session_id, port, passthrough_args,
        terminal_title=session_name,
    )

    # Print a clean banner before Claude's pty takes over.
    # Stays visible in the terminal scrollback after Claude's TUI clears the screen.
    _print_launch_banner(local_url, tailscale_url)

    exit_code = 0
    try:
        exit_code = session.run()
    finally:
        try:
            httpx.post(
                f"{base_url}/sessions/unregister",
                json={"session_id": session_id},
                timeout=2.0,
                verify=False,
            )
        except Exception:
            pass

    sys.exit(exit_code)


# ── Setup wizard ───────────────────────────────────────────────────────────────

def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "<your-machine-ip>"


def _get_tailscale_ip() -> str:
    """Return the Tailscale IPv4 address, or empty string if not available."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=2,
        )
        ip = result.stdout.strip()
        if ip and result.returncode == 0:
            return ip
    except Exception:
        pass
    return ""


def _get_tailscale_fqdn() -> str:
    """Return the machine's Tailscale DNS name (e.g. 'box.tail-xxxx.ts.net') or ''."""
    import shutil as _shutil
    bin_ = _shutil.which("tailscale") or ""
    if not bin_ and sys.platform == "win32":
        candidate = Path(r"C:\Program Files\Tailscale\tailscale.exe")
        if candidate.exists():
            bin_ = str(candidate)
    if not bin_:
        return ""
    try:
        result = subprocess.run(
            [bin_, "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            import json as _json
            data = _json.loads(result.stdout)
            dns = data.get("Self", {}).get("DNSName", "")
            return dns.rstrip(".")
    except Exception:
        pass
    return ""


def _print_launch_banner(local_url: str, tailscale_url: str) -> None:
    """Print a clean one-time banner before Claude's pty starts.

    Uses no cursor magic — just plain text that scrolls into the terminal
    scrollback once Claude's TUI takes over the screen.
    """
    from . import __version__ as _cbv
    DIM = "\x1b[2m"
    CB  = "\x1b[38;2;0;210;160m"  # teal/mint — ClaudeBud accent
    RST = "\x1b[0m"
    rule = DIM + "─" * 48 + RST
    print(rule)
    print(f"  {CB}ClaudeBud v{_cbv}{RST}  {DIM}—  watch & control from your phone{RST}")
    print(f"  {DIM}Local:   {RST} {CB}{local_url}{RST}")
    if tailscale_url:
        print(f"  {DIM}External:{RST} {CB}{tailscale_url}{RST}  {DIM}(Tailscale){RST}")
    print(rule)
    print(flush=True)


def _print_access_urls(port: int) -> None:
    local_ip = _get_local_ip()
    tailscale_fqdn = _get_tailscale_fqdn()
    print(f"  Local:     http://{local_ip}:{port}")
    if tailscale_fqdn:
        print(f"  Tailscale: https://{tailscale_fqdn}:{port}")


def _setup_autostart_macos(port: int) -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "sh.claudebud.plist"
    python = sys.executable
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>sh.claudebud</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>claudebud.daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>StandardOutPath</key>
  <string>{Path.home()}/.claudebud/daemon.log</string>
  <key>StandardErrorPath</key>
  <string>{Path.home()}/.claudebud/daemon.log</string>
</dict>
</plist>
"""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    print(f"  Written: {plist_path}")
    print(f"  To load now:  launchctl load {plist_path}")
    print(f"  To unload:    launchctl unload {plist_path}")


def _setup_autostart_linux(port: int) -> None:
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_path = service_dir / "claudebud.service"
    python = sys.executable
    service = f"""[Unit]
Description=ClaudeBud daemon
After=network.target

[Service]
ExecStart={python} -m claudebud.daemon
Restart=on-failure
StandardOutput=append:{Path.home()}/.claudebud/daemon.log
StandardError=append:{Path.home()}/.claudebud/daemon.log

[Install]
WantedBy=default.target
"""
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service)
    print(f"  Written: {service_path}")
    print("  To enable:  systemctl --user enable --now claudebud")
    print("  To disable: systemctl --user disable --now claudebud")


def _setup_autostart_windows() -> None:
    """Add a .bat launcher to the Windows Startup folder."""
    startup = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / \
              "Start Menu" / "Programs" / "Startup"
    bat_path = startup / "claudebud-daemon.bat"
    python = sys.executable
    bat = (
        "@echo off\n"
        f'"{python}" -m claudebud.daemon\n'
    )
    startup.mkdir(parents=True, exist_ok=True)
    bat_path.write_text(bat)
    print(f"  Written: {bat_path}")
    print("  The daemon will start automatically the next time you log in.")
    print("  To disable: delete that file.")


def _setup_autostart_wsl() -> None:
    guard = (
        "\n# ClaudeBud daemon autostart\n"
        "if ! curl -sf http://127.0.0.1:3131/sessions > /dev/null 2>&1; then\n"
        f"    {sys.executable} -m claudebud.daemon &\n"
        "fi\n"
    )
    rc_file = Path.home() / (
        ".zshrc" if (Path.home() / ".zshrc").exists() else ".bashrc"
    )
    existing = rc_file.read_text() if rc_file.exists() else ""
    if "claudebud.daemon" in existing:
        print(f"  Autostart already present in {rc_file}")
    else:
        with rc_file.open("a") as f:
            f.write(guard)
        print(f"  Added autostart guard to {rc_file}")


def run_setup() -> None:
    print("=" * 50)
    print("  ClaudeBud Setup")
    print("=" * 50)
    print()

    cfg = load_config()

    print("Notifications are configured via the PWA — tap the 🔔 button in the app.")
    print("(Requires HTTPS — Tailscale provides this automatically, or use the local cert.)")
    print()

    # Autostart
    print()
    try:
        ans = input("Set up autostart? (y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        ans = ""

    if ans == "y":
        platform = sys.platform
        is_wsl = "microsoft" in Path("/proc/version").read_text().lower() if Path("/proc/version").exists() else False

        if is_wsl:
            print("  Detected: WSL")
            _setup_autostart_wsl()
        elif platform == "darwin":
            print("  Detected: macOS")
            _setup_autostart_macos(cfg["port"])
        elif platform.startswith("linux"):
            print("  Detected: Linux (systemd)")
            _setup_autostart_linux(cfg["port"])
        elif platform == "win32":
            print("  Detected: Windows")
            _setup_autostart_windows()
        else:
            print(f"  Unsupported platform for autostart: {platform}")

    # 3. Detect Tailscale + show URLs
    print()
    ip = _get_local_ip()
    port = cfg["port"]
    tailscale_fqdn = _get_tailscale_fqdn()

    if tailscale_fqdn:
        print("Tailscale detected")
        print(f"  Machine: {tailscale_fqdn}")
        print()
        print("Open this URL on your phone (Tailscale — works anywhere):")
        print(f"  https://{tailscale_fqdn}:{port}")
        print()
        print("  Note: requires HTTPS Certificates enabled on your tailnet.")
        print("  If push notifications don't work, enable it at:")
        print("    https://login.tailscale.com/admin/dns  →  Enable HTTPS")
        print()
        print("Local network (same Wi-Fi only):")
        print(f"  http://{ip}:{port}")
    else:
        print("Tailscale not detected.")
        print("  Install Tailscale for remote access + automatic HTTPS:")
        print("  https://tailscale.com")
        print()
        print("Open this URL on your phone (local Wi-Fi only):")
        print(f"  http://{ip}:{port}")

    print()
    print("Setup complete.")


# ── Update command ─────────────────────────────────────────────────────────────

def run_update() -> None:
    """Update claudebud via pip, then offer to update claude."""
    print("Updating claudebud...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "claudebud"]
    )
    if result.returncode != 0:
        print("[claudebud] Update failed.", file=sys.stderr)
        return

    print()
    try:
        ans = input("Also update claude? (y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        ans = ""

    if ans == "y":
        _run_claude(["update"])


# ── Entry point ────────────────────────────────────────────────────────────────

_HELP_HEADER = f"""\
ClaudeBud {__version__} - wrapper for Claude Code
  claudebud [claude-options] [prompt]

ClaudeBud extras:
  -n, --name <name>    Set PWA tab name for this session (also passed to claude)
  setup                First-time setup (autostart, URL)
  update / upgrade     Update claudebud; optionally update claude too

-- Claude options ------------------------------------------------------------
"""


def main() -> None:
    try:
        _main()
    except KeyboardInterrupt:
        sys.exit(0)


def _main() -> None:
    args = sys.argv[1:]

    if not args:
        run_claude([], session_name=None)
        return

    # -h / --help: show claudebud header then claude's full help
    if "-h" in args or "--help" in args:
        print(_HELP_HEADER, end="")
        if _claude_available():
            _run_claude(["--help"])
        else:
            print("(claude not found in PATH - see https://docs.anthropic.com/en/docs/claude-code/getting-started)")
        return

    # -v / --version: show claudebud version then claude's version
    if "-v" in args or "--version" in args:
        print(f"claudebud {__version__}", flush=True)
        if _claude_available():
            _run_claude(["-v"])
        else:
            print("(claude not found in PATH - see https://docs.anthropic.com/en/docs/claude-code/getting-started)")
        return

    if args[0] == "setup":
        run_setup()
        return

    if args[0] in ("update", "upgrade"):
        run_update()
        return

    # -p / --print: non-interactive pipe mode — skip daemon/pty, run claude directly
    if "-p" in args or "--print" in args:
        print("[claudebud] Non-interactive mode - running claude directly.", file=sys.stderr)
        sys.exit(_run_claude(args).returncode)

    # Peek at -n/--name to use as the PWA tab name.
    # The flag is kept in passthrough so claude also receives it
    # (claude uses -n/--name to set its own session title).
    session_name = None
    for i, arg in enumerate(args):
        if arg in ("-n", "--name") and i + 1 < len(args):
            session_name = args[i + 1]
            break

    run_claude(args, session_name=session_name)

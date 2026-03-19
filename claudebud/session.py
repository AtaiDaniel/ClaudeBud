"""
session.py — pty wrapper, output streaming to daemon.
Supports Unix/macOS (via os.openpty) and Windows (via pywinpty / ConPTY).
Session naming is handled at launch time via -n/--name in cli.py,
and at any time via the web app (POST /sessions/{id}/rename).
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import List, Optional

import httpx

# Unix-only stdlib modules
try:
    import fcntl
    import select
    import signal
    import struct
    import termios
    import tty
    _UNIX = True
except ImportError:
    _UNIX = False

# Windows-only stdlib modules
if sys.platform == "win32":
    import msvcrt

# Arrow key map for Windows special key sequences (prefix \x00 or \xe0)
# Values are str because pywinpty.write() expects str
_WIN_ARROW_MAP = {
    "H": "\x1b[A",  # Up
    "P": "\x1b[B",  # Down
    "K": "\x1b[D",  # Left
    "M": "\x1b[C",  # Right
}


# Matches OSC title-setting sequences: ESC ] 0;title BEL  or  ESC ] 2;title BEL
# Also handles ST terminator (ESC \) which some terminals use instead of BEL.
_TITLE_RE = re.compile(rb"\x1b\][0-2];[^\x07\x1b]*(?:\x07|\x1b\\)")

# ── Banner rewriter ────────────────────────────────────────────────────────────

# Strips ANSI/VT escape sequences from bytes so we can read plain text.
# Handles both BEL-terminated and ST-terminated (ESC \) OSC sequences, plus
# standalone ST — the latter matters on Windows where Claude Code may emit
# ESC \ as a string terminator, which otherwise passes through as a stray '\'.
_ANSI_STRIP = re.compile(
    rb"\x1b(?:"
    rb"\[[0-9;?]*[a-zA-Z]"              # CSI:  ESC [ … letter
    rb"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC:  ESC ] … BEL  or  ESC \
    rb"|[=><!]"                          # two-char: ESC = > < !
    rb"|\(B"                             # charset:  ESC ( B
    rb"|[0-9]"                           # single digit sequences
    rb"|\\"                              # standalone ST: ESC \
    rb")"
)

# Matches cursor-right sequences \x1b[NC — we convert these to spaces
_CURSOR_RIGHT = re.compile(rb"\x1b\[(\d*)C")

# Version number in raw bytes — appears contiguously even amid escape codes
_VERSION_RE = re.compile(rb"v\d+\.\d+")

# Standalone ESC+\ (String Terminator) emitted by Claude Code on Windows as a
# sequence terminator.  xterm.js on the phone may not consume it silently and
# can display it as a stray '\' character.
_STANDALONE_ST_RE = re.compile(rb"\x1b\\")

# Horizontal-rule shortening: replacement and char set used in _post_output.
_SHORT_RULE = ("\x1b[2m" + "─" * 28 + "\x1b[0m").encode()
_RULE_CHARS  = frozenset("─━═╌╍-")

# Block characters used by Claude's logo — stripped when extracting info text
_LOGO_CHARS = frozenset("▐▛█▜▌▝▘▙▚▟▞▗▖▄▀▒░▓")


def _to_plain(raw: bytes) -> str:
    """Convert raw terminal bytes to approximate plain text.
    Cursor-right sequences are replaced with equivalent spaces so that
    words spaced via \x1b[1C render correctly (e.g. 'Claude Code v2.1.78')."""
    def _cr_to_spaces(m: re.Match) -> bytes:
        n = int(m.group(1)) if m.group(1) else 1
        return b" " * n
    text = _CURSOR_RIGHT.sub(_cr_to_spaces, raw)
    text = _ANSI_STRIP.sub(b"", text)
    return text.decode("utf-8", errors="replace")

# Claude logo reconstructed with original orange colour
_OR   = "\x1b[38;2;215;119;87m"   # orange — matches Claude's logo
_CB   = "\x1b[38;2;0;210;160m"    # teal/mint — ClaudeBud accent
_RST  = "\x1b[0m"
_DIM  = "\x1b[2m"

_LOGO = [
    f" {_OR}▐▛███▜▌{_RST}",
    f"{_OR}▝▜█████▛▘{_RST}",
    f"  {_OR}▘▘ ▝▝{_RST}  ",
]

# Buddy — appears on logo lines 1 and 2, right of the logo.
# Both rows are padded to the same visible width (8 chars) so the
# info column stays aligned across all three banner lines.
_BUDDY = [
    "       ",              # line 0: 7-space placeholder
    f"{_OR}▐▛█▛█{_RST}  ",  # line 1: 5 chars + 2 trail = 7 visible
    f"{_OR}▜████▘{_RST} ",  # line 2: 6 chars + 1 trail = 7 visible
]


class _BannerOverwriter:
    """Passes all pty output through immediately (zero buffering), but once
    the Claude startup banner is detected in the accumulated stream it appends
    ANSI cursor-repositioning sequences that jump back to row 1 and overwrite
    those 3 lines with a version that includes the ClaudeBud buddy character."""

    _MARKER   = "Claude Code v"
    _MAX_SEEN = 8_000   # stop tracking after this many bytes

    def __init__(self, local_url: str = "", tailscale_url: str = "") -> None:
        self._seen: bytes = b""
        self._done: bool  = False
        self._local_url = local_url
        self._tailscale_url = tailscale_url

    def feed(self, data: bytes) -> bytes:
        """Always returns data immediately; may append an overwrite payload."""
        if self._done:
            return data

        self._seen += data
        if len(self._seen) > self._MAX_SEEN:
            self._done = True
            return data

        # Quick check: version number must be present in raw bytes
        if not _VERSION_RE.search(self._seen):
            return data

        # Render cursor-right sequences as spaces so word gaps are preserved
        plain = _to_plain(self._seen)

        # Extract 3 non-empty info lines (strip logo block chars from each)
        info: list = []
        for line in plain.splitlines():
            text = "".join(c for c in line if c not in _LOGO_CHARS).strip()
            if text:
                info.append(text)
            if len(info) == 3:
                break

        if len(info) < 3:
            return data  # keep watching

        self._done = True
        overwrite = self._build_overwrite(info[0], info[1], info[2])
        return data + overwrite.encode("utf-8")

    def _build_overwrite(self, version: str, model: str, path: str) -> str:
        from . import __version__ as _cbv
        # Normalise Windows path separators so backslashes don't appear in the
        # terminal output stream (avoids stray '\' characters on Windows).
        path = path.replace("\\", "/")
        cb_line = f"\x1b[2K         {_CB}+ ClaudeBud v{_cbv}{_RST}"
        rows = [
            f"\x1b[2K{_LOGO[0]}{_BUDDY[0]}{version}",
            f"\x1b[2K{_LOGO[1]}{_BUDDY[1]}{model}",
            f"\x1b[2K{_LOGO[2]}{_BUDDY[2]}{_DIM}{path}{_RST}",
            cb_line,
            f"\x1b[2K  {_DIM}Local:    {_RST}{_CB}{self._local_url}{_RST}",
        ]
        if self._tailscale_url:
            rows.append(
                f"\x1b[2K  {_DIM}External: {_RST}{_CB}{self._tailscale_url}{_RST}"
                f"  {_DIM}(Tailscale){_RST}"
            )

        # Claude's original banner is 3 lines. For every extra line we add,
        # insert a blank line at row 4 first to push Claude's content down,
        # then compensate the restored cursor position with \x1b[NB.
        extra = len(rows) - 3 + 1  # +1: Claude's cursor lands one line below banner
        return (
            "\x1b7"                  # save cursor (after Claude's banner, ~row 4)
            + "\x1b[4;1H"           # go to first line after Claude's 3-line banner
            + f"\x1b[{extra}L"      # insert extra blank lines, pushing content down
            + "\x1b[1;1H"           # jump to row 1
            + "\r\n".join(rows)
            + "\x1b8"               # restore cursor (to saved row 4, inside our banner)
            + f"\x1b[{extra}B"      # move down to compensate → lands after our banner
        )


class Session:
    def __init__(
        self,
        session_id: str,
        daemon_port: int,
        args: List[str],
        terminal_title: Optional[str] = None,
        local_url: str = "",
        tailscale_url: str = "",
    ):
        self.session_id = session_id
        self.daemon_port = daemon_port
        self.args = args
        # The escape sequence we inject whenever claude tries to set a title
        self._title_escape: Optional[bytes] = (
            f"\x1b]0;{terminal_title}\x07".encode() if terminal_title else None
        )
        self._master_fd: int = -1      # Unix only
        self._winpty_proc = None       # Windows only
        self._proc = None              # Unix subprocess
        self._running = False
        self._base_url = f"http://127.0.0.1:{daemon_port}"
        self._http = httpx.Client(timeout=5.0)
        self._banner = _BannerOverwriter(local_url=local_url, tailscale_url=tailscale_url)

    def run(self) -> int:
        """Spawn claude in a pty, proxy I/O, return its exit code."""
        if sys.platform == "win32":
            return self._run_windows()
        return self._run_unix()

    # ── Unix path ──────────────────────────────────────────────────────────────

    def _run_unix(self) -> int:
        master_fd, slave_fd = os.openpty()
        self._master_fd = master_fd
        self._sync_terminal_size(master_fd)

        claude = shutil.which("claude") or "claude"
        cmd = [claude] + self.args
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        self._proc = proc
        self._running = True

        stdin_fd = sys.stdin.fileno()
        old_attrs = None
        try:
            old_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        except termios.error:
            pass

        prev_sigwinch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, lambda s, f: self._sync_terminal_size(master_fd))

        out_thread = threading.Thread(
            target=self._output_loop_unix, args=(master_fd,), daemon=True
        )
        din_thread = threading.Thread(
            target=self._daemon_input_loop, daemon=True
        )
        hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        out_thread.start()
        din_thread.start()
        hb_thread.start()

        try:
            self._stdin_loop_unix(master_fd)
        finally:
            self._running = False
            signal.signal(signal.SIGWINCH, prev_sigwinch)
            if old_attrs is not None:
                try:
                    termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_attrs)
                except termios.error:
                    pass
            self._http.close()

        proc.wait()
        return proc.returncode

    def _output_loop_unix(self, master_fd: int) -> None:
        stdout_fd = sys.stdout.fileno()
        while self._running:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.1)
            except (ValueError, OSError):
                break
            if not r:
                if self._proc and self._proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break
            if not data:
                break

            data = self._banner.feed(data)
            data = self._rewrite_title(data)
            os.write(stdout_fd, data)
            self._post_output(data)

        self._running = False

    def _stdin_loop_unix(self, master_fd: int) -> None:
        """Pure passthrough: stdin → pty master fd."""
        stdin_fd = sys.stdin.fileno()
        while self._running:
            try:
                r, _, _ = select.select([stdin_fd], [], [], 0.1)
            except (ValueError, OSError):
                break
            if not r:
                if self._proc and self._proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(stdin_fd, 256)
            except OSError:
                break
            if not data:
                break
            os.write(master_fd, data)

    def _sync_terminal_size(self, master_fd: int) -> None:
        try:
            buf = struct.pack("HHHH", 0, 0, 0, 0)
            buf = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, buf)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, buf)
            rows, cols = struct.unpack("HHHH", buf)[:2]
            self._post_terminal_size(int(cols), int(rows))
        except Exception:
            pass

    # ── Windows path ───────────────────────────────────────────────────────────

    def _run_windows(self) -> int:
        try:
            from winpty import PtyProcess
        except ImportError:
            print(
                "ClaudeBud requires pywinpty on Windows.\n"
                "Install it with: pip install pywinpty"
            )
            return 1

        try:
            cols, rows = os.get_terminal_size()
        except OSError:
            cols, rows = 80, 24

        self._post_terminal_size(cols, rows)

        claude = shutil.which("claude") or "claude"
        cmd = [claude] + self.args
        # CreateProcess cannot spawn .cmd batch files (npm-installed claude) directly;
        # wrap via cmd.exe so both npm and native-installer versions work.
        # /q suppresses cmd.exe echoing the command line into the pty.
        if claude.lower().endswith(".cmd"):
            # /q  — suppress echo; /d  — disable AutoRun registry commands
            # (AutoRun can emit path-containing output before our command runs)
            cmd = ["cmd.exe", "/q", "/d", "/c"] + cmd
        proc = PtyProcess.spawn(cmd, dimensions=(rows, cols))
        self._winpty_proc = proc
        self._running = True

        out_thread = threading.Thread(
            target=self._output_loop_windows, args=(proc,), daemon=True
        )
        din_thread = threading.Thread(
            target=self._daemon_input_loop, daemon=True
        )
        hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        out_thread.start()
        din_thread.start()
        hb_thread.start()

        try:
            self._stdin_loop_windows(proc)
        finally:
            self._running = False
            try:
                proc.terminate()
            except Exception:
                pass
            self._http.close()

        return 0

    def _output_loop_windows(self, proc) -> None:
        while self._running:
            try:
                data = proc.read(4096)
            except Exception:
                if not proc.isalive():
                    break
                time.sleep(0.01)
                continue
            if not data:
                if not proc.isalive():
                    break
                time.sleep(0.01)
                continue

            # pywinpty returns str; encode for banner/title rewriting and daemon POST
            if isinstance(data, str):
                raw = data.encode("utf-8", errors="replace")
            else:
                raw = data

            raw = self._banner.feed(raw)
            raw = self._rewrite_title(raw)

            sys.stdout.write(raw.decode("utf-8", errors="replace"))
            sys.stdout.flush()
            self._post_output(raw)

        self._running = False

    def _stdin_loop_windows(self, proc) -> None:
        """Pure passthrough: keyboard → pty. Arrow keys mapped to ANSI escapes."""
        # Flush any keys that landed in the console buffer before pty was ready
        # (e.g. the Enter that launched claudebud, or cmd.exe startup artifacts)
        time.sleep(0.15)
        while msvcrt.kbhit():
            msvcrt.getwch()

        while self._running and proc.isalive():
            try:
                if not msvcrt.kbhit():
                    time.sleep(0.01)
                    continue
            except KeyboardInterrupt:
                break

            ch = msvcrt.getwch()

            # Special key prefix (arrow keys, function keys, etc.)
            if ch in ("\x00", "\xe0"):
                ch2 = msvcrt.getwch()
                escape = _WIN_ARROW_MAP.get(ch2)
                if escape:
                    proc.write(escape)
                # Other special keys silently dropped for MVP
                continue

            # pywinpty.write() expects str
            proc.write(ch)

    # ── Shared helpers ─────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        """Ping the daemon every 10s so it knows this session is still alive."""
        while self._running:
            try:
                self._http.post(
                    f"{self._base_url}/sessions/{self.session_id}/heartbeat",
                    timeout=2.0,
                )
            except Exception:
                pass
            # Sleep in small increments so we respond to _running=False quickly
            for _ in range(10):
                if not self._running:
                    break
                time.sleep(1.0)

    def _daemon_input_loop(self) -> None:
        """Long-poll daemon for input forwarded from the PWA."""
        while self._running:
            try:
                r = self._http.get(
                    f"{self._base_url}/sessions/{self.session_id}/next_input",
                    params={"timeout": 25.0},
                    timeout=30.0,
                )
                if r.status_code == 200:
                    data = r.json().get("data", "")
                    if data:
                        if sys.platform == "win32" and self._winpty_proc:
                            self._winpty_proc.write(data)  # winpty expects str
                        elif self._master_fd >= 0:
                            os.write(self._master_fd, data.encode("utf-8"))
            except Exception:
                if self._running:
                    time.sleep(1.0)

    def _rewrite_title(self, data: bytes) -> bytes:
        """Replace any OSC title escape in data with our preferred title."""
        if self._title_escape is None:
            return data
        return _TITLE_RE.sub(self._title_escape, data)

    def _post_terminal_size(self, cols: int, rows: int) -> None:
        """Report the PC terminal dimensions to the daemon so the PWA can match."""
        try:
            self._http.post(
                f"{self._base_url}/sessions/{self.session_id}/terminal_size",
                json={"cols": cols, "rows": rows},
                timeout=2.0,
            )
        except Exception:
            pass

    def _post_output(self, data: bytes) -> None:
        try:
            # Strip standalone ESC+\ (String Terminator) — emitted by Claude Code on
            # Windows as a sequence terminator.  xterm.js on the phone may render it
            # as a stray '\' in the terminal / entry box.
            phone_data = _STANDALONE_ST_RE.sub(b"", data)

            # Shorten long horizontal-rule lines for the phone display.
            # We process line-by-line, strip ANSI codes first (so interspersed colour
            # codes don't break detection), then check whether the visible content is
            # ≥70% rule characters.  Matching lines are replaced with a short dimmed
            # rule (─ × 28) that fits a phone screen without wrapping.
            filtered: list = []
            for line in phone_data.split(b"\n"):
                plain = _ANSI_STRIP.sub(b"", line).decode("utf-8", errors="replace").strip()
                if len(plain) >= 20:
                    rule_count = sum(1 for c in plain if c in _RULE_CHARS)
                    if rule_count / len(plain) > 0.70:
                        line = _SHORT_RULE
                filtered.append(line)
            phone_data = b"\n".join(filtered)

            self._http.post(
                f"{self._base_url}/sessions/{self.session_id}/output",
                json={"data": phone_data.decode("utf-8", errors="replace")},
            )
        except Exception:
            pass

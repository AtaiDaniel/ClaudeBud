"""
daemon.py — FastAPI server, WebSocket hub, session registry.
"""
import asyncio
import datetime
import ipaddress
import json
import logging
import os
import socket as _socket
import subprocess
import time as _time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from .config import load_config, save_config
from .detector import DebouncedDetector, Detector, EventType
from .notifier import notify, StaleSubscriptionError

logger = logging.getLogger(__name__)

# Set by run() after cert resolution; read by GET /info so the CLI can
# display the correct URL (scheme + hostname) in the startup banner.
_serving_scheme: str = "http"
_serving_fqdn:   str = ""


# ── VAPID key generation ──────────────────────────────────────────────────────

def _generate_vapid_keys() -> tuple:
    """Generate a P-256 VAPID key pair. Returns (priv_b64url, pub_b64url)."""
    from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
    import base64
    pk = generate_private_key(SECP256R1())
    priv = base64.urlsafe_b64encode(
        pk.private_numbers().private_value.to_bytes(32, "big")
    ).rstrip(b"=").decode()
    n = pk.public_key().public_numbers()
    pub = base64.urlsafe_b64encode(
        b"\x04" + n.x.to_bytes(32, "big") + n.y.to_bytes(32, "big")
    ).rstrip(b"=").decode()
    return priv, pub


# ── Tailscale HTTPS helpers ───────────────────────────────────────────────────

def _find_tailscale_bin() -> str:
    """Return the path to the tailscale CLI binary, or '' if not found."""
    import shutil
    found = shutil.which("tailscale")
    if found:
        return found
    # On Windows, Tailscale CLI is often not in PATH for detached background processes.
    candidate = Path(r"C:\Program Files\Tailscale\tailscale.exe")
    if candidate.exists():
        return str(candidate)
    return ""


def _get_tailscale_fqdn() -> tuple:
    """Return (fqdn, tailscale_bin).  fqdn is '' if Tailscale is unavailable."""
    tailscale_bin = _find_tailscale_bin()
    if not tailscale_bin:
        logger.info("Tailscale CLI not found — HTTPS disabled")
        return "", ""
    try:
        result = subprocess.run(
            [tailscale_bin, "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            dns = data.get("Self", {}).get("DNSName", "")
            fqdn = dns.rstrip(".")
            if fqdn:
                logger.info("Tailscale FQDN: %s", fqdn)
            else:
                logger.info("Tailscale running but DNSName empty — HTTPS disabled")
            return fqdn, tailscale_bin
        else:
            logger.warning("tailscale status failed (rc=%d): %s", result.returncode, result.stderr.strip())
    except Exception as e:
        logger.warning("tailscale status error: %s", e)
    return "", tailscale_bin


def _get_local_ips() -> list:
    """Return all detected local IPv4 addresses as ipaddress.IPv4Address objects."""
    ips: set = set()
    ips.add(ipaddress.IPv4Address("127.0.0.1"))
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(ipaddress.IPv4Address(s.getsockname()[0]))
        s.close()
    except Exception:
        pass
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None):
            if info[0] == _socket.AF_INET:
                ips.add(ipaddress.IPv4Address(info[4][0]))
    except Exception:
        pass
    return list(ips)


def _ensure_self_signed_cert() -> tuple:
    """Generate (or reuse) a self-signed cert stored at ~/.claudebud/cert.pem.

    The cert lists all current local IPs as SANs so Android Chrome accepts it
    on the local network.  Regenerates automatically when the machine's IP
    changes.  Returns (cert_path, key_path).
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    base = Path.home() / ".claudebud"
    cert_path = base / "cert.pem"
    key_path  = base / "key.pem"
    current_ips = set(_get_local_ips())

    if cert_path.exists() and key_path.exists():
        try:
            existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            cert_ips = set(san.value.get_values_for_type(x509.IPAddress))
            if current_ips.issubset(cert_ips):
                return cert_path, key_path
        except Exception:
            pass

    logger.info("Generating self-signed TLS certificate...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san_list = [x509.DNSName("localhost")] + [x509.IPAddress(ip) for ip in current_ips]
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ClaudeBud")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ClaudeBud")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .sign(key, hashes.SHA256())
    )
    base.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except Exception:
        pass
    logger.info("Self-signed TLS certificate saved to %s", cert_path)
    return cert_path, key_path


def _ensure_tailscale_cert(fqdn: str, tailscale_bin: str = "tailscale") -> tuple:
    """Run 'tailscale cert' to obtain/renew a Let's Encrypt cert for *fqdn*.

    Returns (cert_path, key_path).  Raises on failure (HTTPS not enabled on
    the tailnet, tailscale not running, etc.).
    """
    base = Path.home() / ".claudebud"
    base.mkdir(parents=True, exist_ok=True)
    cert_path = base / "ts-cert.pem"
    key_path  = base / "ts-key.pem"
    logger.info("Running: %s cert %s", tailscale_bin, fqdn)
    result = subprocess.run(
        [
            tailscale_bin, "cert",
            "--cert-file", str(cert_path),
            "--key-file",  str(key_path),
            fqdn,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"tailscale cert failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return cert_path, key_path


# ── Paths ─────────────────────────────────────────────────────────────────────

def get_pid_file() -> Path:
    return Path.home() / ".claudebud" / "daemon.pid"


def get_static_dir() -> Path:
    return Path(__file__).parent / "static"


# ── Version/model detection ───────────────────────────────────────────────────
#
# Claude Code's startup banner looks like (amid ANSI escapes):
#   Claude Code v2.1.79
#   Opus 4.6 (1M context) · Claude Max
#   /path/to/working/dir
#
# We strip ANSI codes and logo block chars to extract plain text lines.

import re as _re

_ANSI_STRIP = _re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*[a-zA-Z]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[=><!]"
    r"|\(B"
    r"|[0-9]"
    r"|\\"
    r")"
)
_CURSOR_RIGHT = _re.compile(r"\x1b\[(\d*)C")
_LOGO_CHARS = frozenset("▐▛█▜▌▝▘▙▚▟▞▗▖▄▀▒░▓")
_MAX_DETECT_CHARS = 8_000


def _to_plain(raw: str) -> str:
    """Strip ANSI escapes and convert cursor-right to spaces."""
    def _cr(m: _re.Match) -> str:
        return " " * (int(m.group(1)) if m.group(1) else 1)
    return _ANSI_STRIP.sub("", _CURSOR_RIGHT.sub(_cr, raw))


_VERSION_LINE_RE = _re.compile(r"Claude Code v[\d.]+")
_MODEL_LINE_RE = _re.compile(
    r"(?:Opus|Sonnet|Haiku|claude)[\s\d.]+"
    r"(?:\([^)]*\))?"           # optional "(1M context)"
    r"(?:\s*[·]\s*\S.*)?"       # optional " · Claude Max"
)


def _extract_version_model(plain: str):
    """Return (version, model) from Claude's banner text, or (None, None)."""
    version = None
    model = None
    for line in plain.splitlines():
        text = "".join(c for c in line if c not in _LOGO_CHARS).strip()
        if not text:
            continue
        if not version:
            m = _VERSION_LINE_RE.search(text)
            if m:
                version = m.group(0)
                continue
        if version and not model:
            m = _MODEL_LINE_RE.search(text)
            if m:
                model = m.group(0).strip()
                return version, model
    return None, None


# ── Data structures ───────────────────────────────────────────────────────────

HEARTBEAT_INTERVAL = 10   # session.py pings every 10s
HEARTBEAT_TIMEOUT  = 35   # daemon drops session after 35s of silence
MAX_BUFFER_CHUNKS  = 600  # max output chunks stored per session (~few MB)


@dataclass
class SessionInfo:
    session_id: str
    name: str
    number: int
    status: str = "running"
    created_at: float = field(default_factory=_time.time)
    last_heartbeat: float = field(default_factory=_time.time)
    output_buffer: list = field(default_factory=list)
    terminal_cols: int = 80
    terminal_rows: int = 24
    claude_version: str = ""
    claude_model: str = ""


class SessionRegistry:
    def __init__(self):
        self._sessions: Dict[str, SessionInfo] = {}
        self._input_queues: Dict[str, asyncio.Queue] = {}
        self._detectors: Dict[str, DebouncedDetector] = {}
        self._detect_bufs: Dict[str, str] = {}   # raw text buffer for version detection
        self._counter: int = 0
        self._lock = asyncio.Lock()

    async def register(self, session_id: str, name: Optional[str] = None) -> SessionInfo:
        async with self._lock:
            self._counter += 1
            n = self._counter
            info = SessionInfo(
                session_id=session_id,
                name=name or f"Terminal {n}",
                number=n,
            )
            self._sessions[session_id] = info
            self._input_queues[session_id] = asyncio.Queue()
            self._detect_bufs[session_id] = ""
            return info

    async def unregister(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
            self._input_queues.pop(session_id, None)
            self._detectors.pop(session_id, None)
            self._detect_bufs.pop(session_id, None)

    def try_detect_version(self, session_id: str, data: str) -> bool:
        """Try to detect Claude version/model from output. Returns True if newly detected."""
        info = self._sessions.get(session_id)
        if not info or info.claude_version:
            return False  # already detected or no session
        buf = self._detect_bufs.get(session_id, "")
        if len(buf) >= _MAX_DETECT_CHARS:
            return False  # give up after threshold
        buf += data
        self._detect_bufs[session_id] = buf
        plain = _to_plain(buf)
        version, model = _extract_version_model(plain)
        if version and model:
            info.claude_version = version
            info.claude_model = model
            self._detect_bufs.pop(session_id, None)  # free buffer
            return True
        return False

    async def rename(self, session_id: str, name: str) -> Optional[SessionInfo]:
        async with self._lock:
            info = self._sessions.get(session_id)
            if info:
                info.name = name
            return info

    async def set_status(self, session_id: str, status: str) -> None:
        async with self._lock:
            info = self._sessions.get(session_id)
            if info:
                info.status = status

    def get(self, session_id: str) -> Optional[SessionInfo]:
        return self._sessions.get(session_id)

    def list_all(self) -> list:
        return list(self._sessions.values())

    def get_queue(self, session_id: str) -> Optional[asyncio.Queue]:
        return self._input_queues.get(session_id)

    def touch_heartbeat(self, session_id: str) -> None:
        info = self._sessions.get(session_id)
        if info:
            info.last_heartbeat = _time.time()

    def get_detector(self, session_id: str, cfg: dict) -> DebouncedDetector:
        if session_id not in self._detectors:
            d = Detector(cfg["prompt_patterns"], cfg["completion_patterns"])
            self._detectors[session_id] = DebouncedDetector(d)
        return self._detectors[session_id]


class WebSocketHub:
    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message, ensure_ascii=False)
        dead: Set[WebSocket] = set()
        async with self._lock:
            clients = set(self._clients)
        for ws in clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._clients -= dead


# ── Module-level state (created fresh in lifespan) ────────────────────────────

registry: Optional[SessionRegistry] = None
hub: Optional[WebSocketHub] = None
_cfg: dict = {}


async def _stale_session_cleanup():
    """Background task: remove sessions whose heartbeat has timed out."""
    while True:
        await asyncio.sleep(15)
        now = _time.time()
        stale = [
            sid for sid, info in registry._sessions.items()
            if now - info.last_heartbeat > HEARTBEAT_TIMEOUT
        ]
        for sid in stale:
            logger.info("Removing stale session %s (heartbeat timeout)", sid)
            await registry.unregister(sid)
            await hub.broadcast({"type": "session_removed", "session_id": sid})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global registry, hub, _cfg
    _cfg = load_config()
    if not _cfg.get("vapid_private_key") or not _cfg.get("vapid_public_key"):
        logger.info("Generating VAPID key pair...")
        priv, pub = _generate_vapid_keys()
        _cfg["vapid_private_key"] = priv
        _cfg["vapid_public_key"]  = pub
        save_config(_cfg)
        logger.info("VAPID keys saved.")
    registry = SessionRegistry()
    hub = WebSocketHub()

    pid_file = get_pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    cleanup_task = asyncio.create_task(_stale_session_cleanup())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass


app = FastAPI(lifespan=lifespan)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    index = get_static_dir() / "index.html"
    return FileResponse(
        str(index), media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@app.get("/manifest.json")
async def serve_manifest():
    return JSONResponse({
        "name": "ClaudeBud",
        "short_name": "ClaudeBud",
        "display": "standalone",
        "background_color": "#1a1a1a",
        "theme_color": "#1a1a1a",
        "start_url": "/",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}
        ],
    })


@app.get("/icon.svg")
async def serve_icon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<text y=".9em" font-size="90">🤖</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/info")
async def get_info():
    """Return the scheme and FQDN the daemon is actually serving on.
    Used by the CLI to build the correct URL for the startup banner."""
    cfg = load_config()
    return {"scheme": _serving_scheme, "fqdn": _serving_fqdn, "port": cfg.get("port", 3131)}


@app.get("/sessions")
async def list_sessions():
    return [
        {
            "session_id": s.session_id,
            "name": s.name,
            "number": s.number,
            "status": s.status,
        }
        for s in registry.list_all()
    ]


@app.post("/sessions/register")
async def register_session(body: dict):
    session_id = body.get("session_id", "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    name = body.get("name") or None
    info = await registry.register(session_id, name)
    await hub.broadcast({
        "type": "session_added",
        "session_id": info.session_id,
        "name": info.name,
        "number": info.number,
        "terminal_cols": info.terminal_cols,
        "terminal_rows": info.terminal_rows,
    })
    return {"session_id": info.session_id, "name": info.name, "number": info.number}


@app.post("/sessions/unregister")
async def unregister_session(body: dict):
    session_id = body.get("session_id", "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    await registry.unregister(session_id)
    await hub.broadcast({"type": "session_removed", "session_id": session_id})
    return {"ok": True}


@app.post("/sessions/{session_id}/output")
async def receive_output(session_id: str, body: dict):
    data = body.get("data", "")
    info = registry.get(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="session not found")

    registry.touch_heartbeat(session_id)
    info.output_buffer.append(data)
    if len(info.output_buffer) > MAX_BUFFER_CHUNKS:
        info.output_buffer = info.output_buffer[-MAX_BUFFER_CHUNKS:]
    await hub.broadcast({"type": "output", "session_id": session_id, "data": data})

    detector = registry.get_detector(session_id, _cfg)
    event = detector.detect(data)

    async def _push(title: str, message: str) -> None:
        try:
            await notify(
                title, message,
                _cfg.get("push_subscription", {}),
                _cfg.get("vapid_private_key", ""),
                _cfg.get("vapid_public_key", ""),
            )
        except StaleSubscriptionError:
            logger.warning("Stale push subscription cleared.")
            _cfg["push_subscription"] = {}
            save_config(_cfg)

    if event == EventType.PROMPT:
        await registry.set_status(session_id, "prompt")
        await hub.broadcast({
            "type": "session_status",
            "session_id": session_id,
            "status": "prompt",
        })
        last_line = data.strip().splitlines()[-1] if data.strip() else ""
        await _push("⚠️ Claude needs input", f"{info.name}: {last_line}")
    elif event == EventType.COMPLETE:
        await registry.set_status(session_id, "idle")
        await hub.broadcast({
            "type": "session_status",
            "session_id": session_id,
            "status": "idle",
        })
        await _push("✅ Claude finished", info.name)

    return {"ok": True}


@app.post("/sessions/{session_id}/info")
async def session_info(session_id: str, body: dict):
    """Called by session.py when Claude version/model is detected."""
    info = registry.get(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="session not found")
    info.claude_version = body.get("version", "")
    info.claude_model = body.get("model", "")
    logger.info("Detected Claude: %s / %s", info.claude_version, info.claude_model)
    from . import __version__ as _cbv
    await hub.broadcast({
        "type": "session_info",
        "session_id": session_id,
        "version": info.claude_version,
        "model": info.claude_model,
        "cb_version": _cbv,
    })
    return {"ok": True}


@app.post("/sessions/{session_id}/heartbeat")
async def session_heartbeat(session_id: str):
    """Called periodically by session.py to confirm the process is still alive."""
    info = registry.get(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="session not found")
    registry.touch_heartbeat(session_id)
    return {"ok": True}


@app.post("/sessions/{session_id}/rename")
async def rename_session(session_id: str, body: dict):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    info = await registry.rename(session_id, name)
    if not info:
        raise HTTPException(status_code=404, detail="session not found")
    await hub.broadcast({
        "type": "session_renamed",
        "session_id": session_id,
        "name": name,
    })
    return {"ok": True, "name": name}


@app.get("/sessions/{session_id}/next_input")
async def next_input(session_id: str, timeout: float = 30.0):
    """Long-poll endpoint — session.py calls this to receive input forwarded from the PWA."""
    q = registry.get_queue(session_id)
    if q is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        data = await asyncio.wait_for(q.get(), timeout=timeout)
        return {"data": data}
    except asyncio.TimeoutError:
        return {"data": ""}


_SW_JS = """\
self.addEventListener('push', function(ev) {
  if (!ev.data) return;
  let p; try { p = ev.data.json(); } catch(e) { p = {title:'ClaudeBud', body:ev.data.text()}; }
  ev.waitUntil(self.registration.showNotification(p.title||'ClaudeBud', {
    body: p.body||'', icon:'/icon.svg', badge:'/icon.svg',
    tag:'claudebud', renotify:true,
  }));
});
self.addEventListener('notificationclick', function(ev) {
  ev.notification.close();
  ev.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(function(cl){
    for (const c of cl) { if ('focus' in c) return c.focus(); }
    if (clients.openWindow) return clients.openWindow('/');
  }));
});
self.addEventListener('install', function() { self.skipWaiting(); });
self.addEventListener('activate', function(ev) { ev.waitUntil(clients.claim()); });
"""


@app.get("/sw.js")
async def serve_service_worker():
    return Response(
        content=_SW_JS,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache, no-store"},
    )


@app.get("/push/vapid-public-key")
async def get_vapid_public_key():
    key = _cfg.get("vapid_public_key", "")
    if not key:
        raise HTTPException(status_code=503, detail="VAPID keys not ready")
    return {"vapid_public_key": key}


@app.post("/push/subscribe")
async def push_subscribe(body: dict):
    global _cfg
    if not body.get("endpoint"):
        raise HTTPException(status_code=400, detail="endpoint required")
    _cfg["push_subscription"] = body
    save_config(_cfg)
    return {"ok": True}


@app.delete("/push/subscribe")
async def push_unsubscribe():
    global _cfg
    _cfg["push_subscription"] = {}
    save_config(_cfg)
    return {"ok": True}


@app.get("/config/notifications")
async def get_notifications_config():
    sub = _cfg.get("push_subscription", {})
    return {"enabled": bool(sub and sub.get("endpoint"))}


@app.post("/config/notifications/test")
async def test_notification():
    sub  = _cfg.get("push_subscription", {})
    priv = _cfg.get("vapid_private_key", "")
    pub  = _cfg.get("vapid_public_key", "")
    if not sub or not sub.get("endpoint"):
        raise HTTPException(
            status_code=400,
            detail="No push subscription. Enable notifications in the app first.",
        )
    try:
        await notify("🧪 ClaudeBud test", "Notifications are working!", sub, priv, pub)
    except StaleSubscriptionError:
        _cfg["push_subscription"] = {}
        save_config(_cfg)
        raise HTTPException(
            status_code=410,
            detail="Subscription expired. Re-enable notifications in the app.",
        )
    except Exception as exc:
        logger.warning("Test notification failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.post("/sessions/{session_id}/terminal_size")
async def set_terminal_size(session_id: str, body: dict):
    """Called by session.py when the PC terminal is resized. Broadcasts to all PWA clients."""
    cols = body.get("cols")
    rows = body.get("rows")
    if not cols or not rows:
        raise HTTPException(status_code=400, detail="cols and rows required")
    info = registry.get(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="session not found")
    info.terminal_cols = int(cols)
    info.terminal_rows = int(rows)
    await hub.broadcast({
        "type": "terminal_resize",
        "session_id": session_id,
        "cols": int(cols),
        "rows": int(rows),
    })
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await hub.connect(ws)
    sessions_list = registry.list_all()
    snapshot = [
        {
            "session_id": s.session_id,
            "name": s.name,
            "number": s.number,
            "status": s.status,
            "terminal_cols": s.terminal_cols,
            "terminal_rows": s.terminal_rows,
            "claude_version": s.claude_version,
            "claude_model": s.claude_model,
        }
        for s in sessions_list
    ]
    await ws.send_text(json.dumps({"type": "sessions_snapshot", "sessions": snapshot}))
    # Replay buffered output for each session so the client sees existing history.
    # Prefix with a full screen clear so xterm.js starts blank — without this,
    # replaying multiple TUI redraw cycles causes duplicate / overlapping content.
    # Only replay the last 80 chunks to avoid sending stale cursor-positioning state
    # from old response cycles that would confuse xterm.js layout.
    HISTORY_REPLAY_CHUNKS = 150
    CLEAR_HOME = "\x1b[2J\x1b[H"
    for s in sessions_list:
        if s.output_buffer:
            recent = s.output_buffer[-HISTORY_REPLAY_CHUNKS:]
            history = CLEAR_HOME + "".join(recent)
            await ws.send_text(json.dumps({
                "type": "session_history",
                "session_id": s.session_id,
                "data": history,
            }))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            session_id = msg.get("session_id", "")

            if msg_type == "input":
                q = registry.get_queue(session_id)
                if q is not None:
                    await q.put(msg.get("data", ""))
            elif msg_type == "rename":
                name = msg.get("name", "").strip()
                if name:
                    info = await registry.rename(session_id, name)
                    if info:
                        await hub.broadcast({
                            "type": "session_renamed",
                            "session_id": session_id,
                            "name": name,
                        })
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(ws)


# ── Entry point ───────────────────────────────────────────────────────────────

def run(port: int = 3131):
    global _serving_scheme, _serving_fqdn

    # Log to file so cert errors are visible even though the daemon is detached.
    log_path = Path.home() / ".claudebud" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.INFO)

    ssl_certfile = None
    ssl_keyfile  = None

    # Try Tailscale — provides a browser-trusted Let's Encrypt cert.
    fqdn, tailscale_bin = _get_tailscale_fqdn()
    if fqdn:
        try:
            cert_path, key_path = _ensure_tailscale_cert(fqdn, tailscale_bin)
            ssl_certfile    = str(cert_path)
            ssl_keyfile     = str(key_path)
            _serving_scheme = "https"
            _serving_fqdn   = fqdn
            logger.info("HTTPS enabled via Tailscale cert for %s", fqdn)
        except Exception as e:
            logger.warning(
                "Tailscale cert unavailable (%s) — serving HTTP. "
                "To enable HTTPS: login.tailscale.com/admin/dns → Enable HTTPS Certificates.", e
            )

    # No self-signed fallback — requires manual trust on phones.
    # HTTP is fine for local network; Tailscale WireGuard encrypts remote traffic.

    scheme = "https" if ssl_certfile else "http"
    logger.info("ClaudeBud daemon starting on %s://0.0.0.0:%d", scheme, port)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


if __name__ == "__main__":
    cfg = load_config()
    run(port=cfg.get("port", 3131))

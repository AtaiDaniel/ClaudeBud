"""
daemon.py — FastAPI server, WebSocket hub, session registry.
"""
import asyncio
import json
import logging
import os
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


# ── Paths ─────────────────────────────────────────────────────────────────────

def get_pid_file() -> Path:
    return Path.home() / ".claudebud" / "daemon.pid"


def get_static_dir() -> Path:
    return Path(__file__).parent / "static"


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


class SessionRegistry:
    def __init__(self):
        self._sessions: Dict[str, SessionInfo] = {}
        self._input_queues: Dict[str, asyncio.Queue] = {}
        self._detectors: Dict[str, DebouncedDetector] = {}
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
            return info

    async def unregister(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)
            self._input_queues.pop(session_id, None)
            self._detectors.pop(session_id, None)

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
    return FileResponse(str(index), media_type="text/html")


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
        }
        for s in sessions_list
    ]
    await ws.send_text(json.dumps({"type": "sessions_snapshot", "sessions": snapshot}))
    # Replay buffered output for each session so the client sees existing history
    for s in sessions_list:
        if s.output_buffer:
            history = "".join(s.output_buffer)
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
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    cfg = load_config()
    run(port=cfg.get("port", 3131))

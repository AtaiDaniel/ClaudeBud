"""tests/test_daemon.py — tests for daemon.py HTTP endpoints and WebSocket."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import claudebud.daemon as daemon_module
from claudebud.daemon import app
from claudebud.config import DEFAULTS


MOCK_CFG = {
    **DEFAULTS,
    "ntfy_topic": "",   # empty → notifications skipped
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with isolated PID file and mocked config."""
    pid_file = tmp_path / "daemon.pid"
    monkeypatch.setattr(daemon_module, "get_pid_file", lambda: pid_file)
    monkeypatch.setattr(daemon_module, "load_config", lambda: dict(MOCK_CFG))

    with TestClient(app) as c:
        yield c


# ── Session lifecycle ─────────────────────────────────────────────────────────

def test_list_sessions_empty(client):
    r = client.get("/sessions")
    assert r.status_code == 200
    assert r.json() == []


def test_register_session(client):
    r = client.post("/sessions/register", json={"session_id": "abc-123"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "abc-123"
    assert body["name"] == "Terminal 1"
    assert body["number"] == 1


def test_register_session_sequential_naming(client):
    client.post("/sessions/register", json={"session_id": "s1"})
    r = client.post("/sessions/register", json={"session_id": "s2"})
    assert r.json()["name"] == "Terminal 2"
    assert r.json()["number"] == 2


def test_register_session_custom_name(client):
    r = client.post("/sessions/register", json={"session_id": "s1", "name": "my-session"})
    assert r.json()["name"] == "my-session"


def test_register_session_missing_id(client):
    r = client.post("/sessions/register", json={})
    assert r.status_code == 400


def test_list_sessions_after_register(client):
    client.post("/sessions/register", json={"session_id": "abc"})
    r = client.get("/sessions")
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "abc"
    assert sessions[0]["name"] == "Terminal 1"


def test_unregister_session(client):
    client.post("/sessions/register", json={"session_id": "abc"})
    r = client.post("/sessions/unregister", json={"session_id": "abc"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    sessions = client.get("/sessions").json()
    assert sessions == []


def test_unregister_missing_id(client):
    r = client.post("/sessions/unregister", json={})
    assert r.status_code == 400


def test_rename_session(client):
    client.post("/sessions/register", json={"session_id": "abc"})
    r = client.post("/sessions/abc/rename", json={"name": "catheter-tracker"})
    assert r.status_code == 200
    assert r.json()["name"] == "catheter-tracker"
    sessions = client.get("/sessions").json()
    assert sessions[0]["name"] == "catheter-tracker"


def test_rename_session_not_found(client):
    r = client.post("/sessions/nonexistent/rename", json={"name": "foo"})
    assert r.status_code == 404


def test_rename_session_empty_name(client):
    client.post("/sessions/register", json={"session_id": "abc"})
    r = client.post("/sessions/abc/rename", json={"name": "   "})
    assert r.status_code == 400


# ── Output endpoint ───────────────────────────────────────────────────────────

def test_output_ok(client):
    client.post("/sessions/register", json={"session_id": "abc"})
    r = client.post("/sessions/abc/output", json={"data": "hello world"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_output_session_not_found(client):
    r = client.post("/sessions/missing/output", json={"data": "hi"})
    assert r.status_code == 404


def test_output_prompt_pattern_updates_status(client):
    client.post("/sessions/register", json={"session_id": "abc"})
    client.post("/sessions/abc/output", json={"data": "Allow this tool? (Y/n)"})
    sessions = client.get("/sessions").json()
    assert sessions[0]["status"] == "prompt"


def test_output_complete_pattern_updates_status(client):
    client.post("/sessions/register", json={"session_id": "abc"})
    client.post("/sessions/abc/output", json={"data": "✓ Completed successfully"})
    sessions = client.get("/sessions").json()
    assert sessions[0]["status"] == "idle"


def test_output_normal_keeps_running_status(client):
    client.post("/sessions/register", json={"session_id": "abc"})
    client.post("/sessions/abc/output", json={"data": "Writing file src/main.py..."})
    sessions = client.get("/sessions").json()
    assert sessions[0]["status"] == "running"


# ── Static / manifest ─────────────────────────────────────────────────────────

def test_manifest_json(client):
    r = client.get("/manifest.json")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "ClaudeBud"
    assert body["theme_color"] == "#1a1a1a"


def test_serve_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_serve_icon(client):
    r = client.get("/icon.svg")
    assert r.status_code == 200
    assert "svg" in r.headers["content-type"]


# ── WebSocket ─────────────────────────────────────────────────────────────────

def test_ws_receives_snapshot_on_connect(client):
    client.post("/sessions/register", json={"session_id": "s1"})
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "sessions_snapshot"
    assert len(msg["sessions"]) == 1
    assert msg["sessions"][0]["session_id"] == "s1"


def test_ws_snapshot_empty_when_no_sessions(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "sessions_snapshot"
    assert msg["sessions"] == []


def test_ws_rename_via_websocket(client):
    client.post("/sessions/register", json={"session_id": "s1"})
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # consume snapshot
        ws.send_json({"type": "rename", "session_id": "s1", "name": "my-tab"})
        msg = ws.receive_json()
    assert msg["type"] == "session_renamed"
    assert msg["name"] == "my-tab"
    # Verify it persisted
    sessions = client.get("/sessions").json()
    assert sessions[0]["name"] == "my-tab"


def test_ws_input_queued_for_long_poll(client):
    client.post("/sessions/register", json={"session_id": "s1"})
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # consume snapshot
        ws.send_json({"type": "input", "session_id": "s1", "data": "\r"})
    # The input was queued; retrieve it via long-poll (timeout=0 → immediate or empty)
    r = client.get("/sessions/s1/next_input?timeout=0.1")
    assert r.status_code == 200
    assert r.json()["data"] == "\r"


def test_next_input_timeout_returns_empty(client):
    client.post("/sessions/register", json={"session_id": "s1"})
    r = client.get("/sessions/s1/next_input?timeout=0.05")
    assert r.status_code == 200
    assert r.json()["data"] == ""


def test_next_input_session_not_found(client):
    r = client.get("/sessions/missing/next_input?timeout=0.05")
    assert r.status_code == 404


# ── PID file ──────────────────────────────────────────────────────────────────

def test_pid_file_created_and_removed(tmp_path, monkeypatch):
    pid_file = tmp_path / "daemon.pid"
    monkeypatch.setattr(daemon_module, "get_pid_file", lambda: pid_file)
    monkeypatch.setattr(daemon_module, "load_config", lambda: dict(MOCK_CFG))

    with TestClient(app):
        assert pid_file.exists()
        assert pid_file.read_text().isdigit()

    assert not pid_file.exists()

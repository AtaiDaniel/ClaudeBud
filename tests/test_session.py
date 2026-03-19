"""tests/test_session.py — tests for session.py"""
import sys
import pytest
from unittest.mock import MagicMock, patch
import httpx
import respx

from claudebud.session import Session, _UNIX


# ── Session fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def session():
    """Session instance with HTTP client mocked (no real daemon needed)."""
    s = Session(session_id="test-id", daemon_port=3131, args=[])
    s._http = MagicMock()
    s._master_fd = 10
    return s


# ── Platform routing ───────────────────────────────────────────────────────────

def test_run_dispatches_to_windows_on_win32(session, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with patch.object(session, "_run_windows", return_value=0) as mock_win:
        session.run()
    mock_win.assert_called_once()


def test_run_dispatches_to_unix_on_linux(session, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with patch.object(session, "_run_unix", return_value=0) as mock_unix:
        session.run()
    mock_unix.assert_called_once()


def test_run_dispatches_to_unix_on_darwin(session, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with patch.object(session, "_run_unix", return_value=0) as mock_unix:
        session.run()
    mock_unix.assert_called_once()


def test_run_windows_returns_1_if_winpty_missing(session, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with patch.dict("sys.modules", {"winpty": None}):
        result = session._run_windows()
    assert result == 1


# ── _post_output ───────────────────────────────────────────────────────────────

def test_post_output_sends_decoded_text(session):
    with respx.mock:
        route = respx.post("http://127.0.0.1:3131/sessions/test-id/output").mock(
            return_value=httpx.Response(200)
        )
        session._http = httpx.Client(timeout=5.0)
        session._post_output(b"hello world")
    assert route.called
    import json
    body = json.loads(route.calls[0].request.content)
    assert body["data"] == "hello world"


def test_post_output_swallows_errors(session):
    with respx.mock:
        respx.post("http://127.0.0.1:3131/sessions/test-id/output").mock(
            side_effect=httpx.ConnectError("down")
        )
        session._http = httpx.Client(timeout=5.0)
        session._post_output(b"data")  # should not raise


# ── Terminal size sync (Unix only) ─────────────────────────────────────────────

@pytest.mark.skipif(not _UNIX, reason="Unix-only")
def test_sync_terminal_size_does_not_raise_on_error(session):
    with patch("claudebud.session.fcntl.ioctl", side_effect=OSError("no tty")):
        session._sync_terminal_size(5)  # should not raise

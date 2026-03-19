"""tests/test_notifier.py — tests for notifier.py"""
import httpx
import pytest
import respx

from claudebud.notifier import notify


@pytest.mark.asyncio
async def test_notify_sends_post_to_ntfy():
    with respx.mock:
        route = respx.post("https://ntfy.sh/my-topic").mock(
            return_value=httpx.Response(200)
        )
        await notify("my-topic", "Test Title", "Test message")
    assert route.called
    req = route.calls[0].request
    assert req.headers["Title"] == "Test Title"
    assert req.content == b"Test message"


@pytest.mark.asyncio
async def test_notify_skips_when_topic_empty():
    with respx.mock:
        # No routes registered — any real HTTP call would raise
        await notify("", "Title", "msg")  # should complete silently


@pytest.mark.asyncio
async def test_notify_uses_custom_server():
    with respx.mock:
        route = respx.post("https://my.ntfy.server/cool-topic").mock(
            return_value=httpx.Response(200)
        )
        await notify("cool-topic", "T", "m", server="https://my.ntfy.server")
    assert route.called


@pytest.mark.asyncio
async def test_notify_swallows_http_error():
    with respx.mock:
        respx.post("https://ntfy.sh/my-topic").mock(
            return_value=httpx.Response(500)
        )
        await notify("my-topic", "T", "m")  # should not raise


@pytest.mark.asyncio
async def test_notify_swallows_network_error():
    with respx.mock:
        respx.post("https://ntfy.sh/my-topic").mock(
            side_effect=httpx.ConnectError("unreachable")
        )
        await notify("my-topic", "T", "m")  # should not raise


@pytest.mark.asyncio
async def test_notify_sets_priority_high():
    with respx.mock:
        route = respx.post("https://ntfy.sh/prio-topic").mock(
            return_value=httpx.Response(200)
        )
        await notify("prio-topic", "T", "m")
    assert route.calls[0].request.headers["Priority"] == "high"

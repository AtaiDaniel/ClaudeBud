"""
notifier.py — Web Push notifications via VAPID (pywebpush).
"""
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

VAPID_CLAIMS = {"sub": "mailto:claudebud@localhost"}


class StaleSubscriptionError(Exception):
    """Push service returned 410 — subscription is no longer valid."""


def _send_push_sync(
    subscription: dict,
    title: str,
    message: str,
    vapid_private_key: str,
    vapid_public_key: str,
) -> None:
    """Synchronous helper; called via run_in_executor so the event loop is never blocked."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        raise RuntimeError(
            "pywebpush is not installed. Run: pip install pywebpush"
        )

    data = json.dumps({"title": title, "body": message})
    try:
        resp = webpush(
            subscription_info=subscription,
            data=data,
            vapid_private_key=vapid_private_key,
            vapid_claims=dict(VAPID_CLAIMS),
        )
        if resp is not None:
            logger.info("Push service response: HTTP %s", resp.status_code)
        if resp is not None and resp.status_code == 410:
            raise StaleSubscriptionError()
        if resp is not None and resp.status_code not in (200, 201):
            body = ""
            try:
                body = resp.text[:300]
            except Exception:
                pass
            raise RuntimeError(f"Push service returned HTTP {resp.status_code}: {body}")
    except StaleSubscriptionError:
        raise
    except WebPushException as exc:
        raise RuntimeError(f"WebPushException: {exc}") from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to send Web Push: {exc}") from exc


async def notify(
    title: str,
    message: str,
    subscription: dict,
    vapid_private_key: str,
    vapid_public_key: str,
) -> bool:
    """
    Send a Web Push notification.
    Silently returns False if subscription or VAPID key is not configured.
    Raises StaleSubscriptionError if the push service returns 410.
    Raises RuntimeError on other push failures.
    """
    if not subscription or not subscription.get("endpoint") or not vapid_private_key:
        return False

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        _send_push_sync,
        subscription,
        title,
        message,
        vapid_private_key,
        vapid_public_key,
    )
    return True

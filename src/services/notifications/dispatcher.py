"""Notification dispatch — dedupe, retry once, and NEVER raise into the caller.

A Slack outage must not fail an investigation. Every path returns DeliveryResult.

Bodies are deliberately NOT masked: Slack/Telegram go to the customer's own
workspace, inside their trust boundary, and "AURA_POD_03 is crashlooping" is useless
to an on-call engineer. Masking is for external LLM egress only.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from src.config_settings import get_settings
from src.services.notifications.base import DeliveryResult, Notification, NotificationChannel
from src.services.notifications.slack import SlackChannel
from src.services.notifications.telegram import TelegramChannel

log = logging.getLogger(__name__)

_CHANNEL_TYPES: dict[str, type[NotificationChannel]] = {
    "slack": SlackChannel,
    "telegram": TelegramChannel,
}

_LOG_TABLE = "notification-log"


def known_channel_types() -> list[str]:
    return sorted(_CHANNEL_TYPES)


def resolve_channels(user_id: str = "", project_id: str = "") -> list[NotificationChannel]:
    """Configured channels: user-connectors rows first, then .env defaults."""
    out: list[NotificationChannel] = []
    seen: set[str] = set()

    if user_id:
        try:
            from src.database.dynamo_client import query_items
            for row in query_items("user-connectors", "userId", user_id, limit=200):
                if row.get("type") != "notification":
                    continue
                ctype = row.get("provider", "")
                cls = _CHANNEL_TYPES.get(ctype)
                if not cls:
                    continue
                out.append(cls(dict(row.get("config") or {})))
                seen.add(ctype)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not read notification connectors: %s", exc)

    s = get_settings()
    if "slack" not in seen and (s.slack_bot_token or s.slack_webhook_url):
        out.append(SlackChannel({"bot_token": s.slack_bot_token,
                                 "webhook_url": s.slack_webhook_url,
                                 "channel_id": s.slack_default_channel}))
    if "telegram" not in seen and s.telegram_bot_token:
        out.append(TelegramChannel({"bot_token": s.telegram_bot_token,
                                    "chat_id": s.telegram_default_chat_id}))
    return out


def _already_sent(dedupe_key: str) -> bool:
    if not dedupe_key:
        return False
    try:
        from src.database.dynamo_client import query_items
        rows = query_items(_LOG_TABLE, "dedupeKey", dedupe_key, limit=1)
        if not rows:
            return False
        sent = rows[0].get("sentAt", "")
        ttl = get_settings().notification_dedupe_ttl_s
        then = datetime.fromisoformat(sent.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - then).total_seconds() < ttl
    except Exception:  # noqa: BLE001
        return False


def _record(n: Notification, results: list[DeliveryResult]) -> None:
    """The dedupe row doubles as the delivery audit trail."""
    if not n.dedupe_key:
        return
    try:
        from src.database.dynamo_client import put_item
        put_item(_LOG_TABLE, {
            "dedupeKey": n.dedupe_key,
            "sentAt": datetime.now(timezone.utc).isoformat(),
            "kind": n.kind, "severity": n.severity, "title": n.title[:200],
            "results": [r.to_dict() for r in results],
        })
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not record notification: %s", exc)


async def send(n: Notification, channels: list[NotificationChannel] | None = None,
               user_id: str = "", project_id: str = "") -> list[DeliveryResult]:
    s = get_settings()
    if not s.notifications_enabled:
        return [DeliveryResult(False, "all", skipped_reason="notifications disabled")]
    if _already_sent(n.dedupe_key):
        log.info("Suppressed duplicate notification %s", n.dedupe_key)
        return [DeliveryResult(False, "all", skipped_reason="deduped")]

    targets = channels if channels is not None else resolve_channels(user_id, project_id)
    if not targets:
        return [DeliveryResult(False, "all", skipped_reason="no channels configured")]

    results: list[DeliveryResult] = []
    for ch in targets:
        res = await _send_one(ch, n)
        results.append(res)
        if not res.ok and not res.skipped_reason:
            log.warning("Notification to %s failed: %s", res.channel, res.error)
    _record(n, results)
    return results


async def _send_one(ch: NotificationChannel, n: Notification) -> DeliveryResult:
    try:
        res = await ch.send(n)
    except Exception as exc:  # noqa: BLE001
        res = DeliveryResult(False, ch.channel_type, error=str(exc)[:200])
    if res.ok or res.skipped_reason:
        return res
    await asyncio.sleep(0.5 + random.random() * 0.5)      # one retry with jitter
    try:
        return await ch.send(n)
    except Exception as exc:  # noqa: BLE001
        return DeliveryResult(False, ch.channel_type, error=str(exc)[:200])


def send_sync(n: Notification, user_id: str = "", project_id: str = "") -> None:
    """Fire-and-forget from sync code or an already-running loop."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(send(n, user_id=user_id, project_id=project_id))
    except RuntimeError:
        try:
            asyncio.run(send(n, user_id=user_id, project_id=project_id))
        except Exception as exc:  # noqa: BLE001
            log.debug("send_sync failed: %s", exc)


async def test_channels(user_id: str = "") -> list[dict]:
    out = []
    for ch in resolve_channels(user_id):
        try:
            ok, msg = await ch.test()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)[:200]
        out.append({"channel": ch.channel_type, "ok": ok, "message": msg})
    return out

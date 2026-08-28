"""Slack — chat.postMessage when a bot token is set, otherwise an incoming webhook."""
from __future__ import annotations

import logging
import time

import httpx

from src.services.notifications.base import DeliveryResult, Notification, NotificationChannel
from src.services.notifications.templates import slack_blocks

log = logging.getLogger(__name__)


class SlackChannel(NotificationChannel):
    channel_type = "slack"

    async def send(self, n: Notification) -> DeliveryResult:
        t0 = time.monotonic()
        bot_token = self._cfg("bot_token", "botToken")
        webhook = self._cfg("webhook_url", "webhookUrl")
        channel = self._cfg("channel_id", "channelId", "channel")
        blocks = slack_blocks(n)

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if bot_token and channel:
                    r = await client.post(
                        "https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {bot_token}",
                                 "Content-Type": "application/json"},
                        json={"channel": channel, "text": n.title, "blocks": blocks})
                    payload = r.json()
                    if not payload.get("ok"):
                        return DeliveryResult(False, self.channel_type,
                                              error=payload.get("error", "slack error"),
                                              latency_ms=_ms(t0))
                elif webhook:
                    r = await client.post(webhook, json={"text": n.title, "blocks": blocks})
                    if r.status_code >= 400:
                        return DeliveryResult(False, self.channel_type,
                                              error=f"HTTP {r.status_code}",
                                              latency_ms=_ms(t0))
                else:
                    return DeliveryResult(False, self.channel_type,
                                          skipped_reason="no bot_token+channel or webhook_url",
                                          latency_ms=_ms(t0))
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, self.channel_type, error=str(exc)[:200],
                                  latency_ms=_ms(t0))
        return DeliveryResult(True, self.channel_type, latency_ms=_ms(t0))

    async def test(self) -> tuple[bool, str]:
        bot_token = self._cfg("bot_token", "botToken")
        if bot_token:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.post("https://slack.com/api/auth.test",
                                          headers={"Authorization": f"Bearer {bot_token}"})
                p = r.json()
                return bool(p.get("ok")), p.get("team", p.get("error", ""))
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)[:200]
        if self._cfg("webhook_url", "webhookUrl"):
            return True, "webhook configured (not verified without sending)"
        return False, "not configured"


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)

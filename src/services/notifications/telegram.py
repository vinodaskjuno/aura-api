"""Telegram — sendMessage via the bot API."""
from __future__ import annotations

import logging
import time

import httpx

from src.services.notifications.base import DeliveryResult, Notification, NotificationChannel
from src.services.notifications.templates import telegram_text

log = logging.getLogger(__name__)


class TelegramChannel(NotificationChannel):
    channel_type = "telegram"

    async def send(self, n: Notification) -> DeliveryResult:
        t0 = time.monotonic()
        token = self._cfg("bot_token", "botToken", "token")
        chat_id = self._cfg("chat_id", "chatId")
        if not (token and chat_id):
            return DeliveryResult(False, self.channel_type,
                                  skipped_reason="no bot_token or chat_id",
                                  latency_ms=_ms(t0))
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": telegram_text(n),
                          "parse_mode": "HTML", "disable_web_page_preview": True})
            payload = r.json()
            if not payload.get("ok"):
                return DeliveryResult(False, self.channel_type,
                                      error=str(payload.get("description", ""))[:200],
                                      latency_ms=_ms(t0))
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(False, self.channel_type, error=str(exc)[:200],
                                  latency_ms=_ms(t0))
        return DeliveryResult(True, self.channel_type, latency_ms=_ms(t0))

    async def test(self) -> tuple[bool, str]:
        token = self._cfg("bot_token", "botToken", "token")
        if not token:
            return False, "not configured"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            p = r.json()
            return bool(p.get("ok")), (p.get("result", {}).get("username")
                                       or str(p.get("description", "")))
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)

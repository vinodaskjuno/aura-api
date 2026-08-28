"""Notification channel contract.

Lives under services/ rather than observability/ deliberately: routers/budget.py
importing `from src.observability...` would be a wrong-direction dependency (billing
depending on SRE tooling). There are already three consumers — observability, budget,
and provider health.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Notification:
    kind: str          # incident | investigation_done | budget_threshold | provider_down
    severity: str      # critical | high | medium | low | info
    title: str
    body: str = ""
    links: list[dict] = field(default_factory=list)    # [{"label","url"}]
    fields: list[dict] = field(default_factory=list)   # [{"label","value"}]
    dedupe_key: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class DeliveryResult:
    ok: bool
    channel: str
    error: str | None = None
    latency_ms: int = 0
    skipped_reason: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "channel": self.channel, "error": self.error,
                "latencyMs": self.latency_ms, "skippedReason": self.skipped_reason}


SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡",
                  "low": "🔵", "info": "⚪"}


class NotificationChannel(ABC):
    channel_type: str = "base"

    def __init__(self, config: dict) -> None:
        self.config = config or {}

    @abstractmethod
    async def send(self, n: Notification) -> DeliveryResult: ...

    @abstractmethod
    async def test(self) -> tuple[bool, str]: ...

    def _cfg(self, *names: str, default: str = "") -> str:
        for name in names:
            v = self.config.get(name)
            if v not in (None, ""):
                return str(v)
        return default

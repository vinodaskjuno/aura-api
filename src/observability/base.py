"""ObservabilityProvider — the query-shaped sibling of AbstractConnector.

Deliberately NOT an AbstractConnector subclass:
  * `sync() -> SyncResult` returns counters, not rows; an SRE agent needs the data.
  * It is synchronous; every agent is `async` and signal collection fans out.
  * It has no (service, window, filter) parameterization — observability queries are
    always windowed.
  * Its lifecycle is a daily cron writing to the graph, not an operator at 3am.

What IS reused is the credential *storage and UX*: providers are configured through
the existing /connectors router and the `user-connectors` table.
"""
from __future__ import annotations

import logging
from abc import ABC
from typing import Any

from src.observability.types import (
    EventQuery, EventRecord, LogPage, LogQuery, MetricQuery, MetricSeries,
    ProviderHealth, Signal, TraceQuery, TraceSummary,
)

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """A provider call failed. Carries enough context to render in the UI."""

    def __init__(self, message: str, *, provider_id: str = "", status: int = 0):
        super().__init__(message)
        self.provider_id = provider_id
        self.status = status


class ProviderNotConfigured(ProviderError):
    """No credentials found for this provider type."""


class ObservabilityProvider(ABC):
    provider_type: str = "base"
    display_name: str = "Base Provider"
    capabilities: frozenset[Signal] = frozenset()

    def __init__(self, config: dict, *, provider_id: str = "", label: str = "") -> None:
        self.config = config or {}
        self.provider_id = provider_id or self.provider_type
        self.label = label or self.display_name

    # ── Capability-aware defaults ────────────────────────────────────────────
    # Unsupported signals return an empty result rather than raising, so callers
    # can fan out over every provider and merge without hasattr checks or
    # NotImplementedError handling.

    def supports(self, signal: Signal) -> bool:
        return signal in self.capabilities

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id,
                              provider_type=self.provider_type,
                              status="not_configured",
                              message="health check not implemented")

    async def list_services(self) -> list[str]:
        return []

    async def query_logs(self, q: LogQuery) -> LogPage:
        return LogPage(unsupported=True)

    async def query_metrics(self, q: MetricQuery) -> list[MetricSeries]:
        return []

    async def query_traces(self, q: TraceQuery) -> list[TraceSummary]:
        return []

    async def recent_deploys(self, q: EventQuery) -> list[EventRecord]:
        return []

    # ── Helpers ──────────────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "providerType": self.provider_type,
            "label": self.label,
            "displayName": self.display_name,
            "capabilities": sorted(self.capabilities),
        }

    def _cfg(self, *names: str, default: str = "") -> str:
        """Read the first present config key. Providers accept snake_case and
        camelCase because the connectors UI and .env disagree on convention."""
        for n in names:
            v = self.config.get(n)
            if v not in (None, ""):
                return str(v)
        return default

"""FixtureProvider — replays recorded provider responses with zero network.

Registered under the SAME provider ids as real providers, so agents need no changes
(seam S4). This is what makes the eval harness possible, and it doubles as the
integration-test double.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.observability.base import ObservabilityProvider
from src.observability.types import (
    EventQuery, EventRecord, LogPage, LogQuery, LogRecord, MetricPoint, MetricQuery,
    MetricSeries, ProviderHealth, Signal, TraceQuery, TraceSummary,
)


class FixtureProvider(ObservabilityProvider):
    provider_type = "fixture"
    display_name = "Fixture (replay)"
    capabilities: frozenset[Signal] = frozenset({"logs", "metrics", "traces", "events"})

    def __init__(self, data: dict, *, provider_id: str = "fixture",
                 label: str = "Fixture", provider_type: str = "fixture") -> None:
        super().__init__({}, provider_id=provider_id, label=label)
        self.data = data or {}
        self.provider_type = provider_type

    @classmethod
    def from_file(cls, path: str | Path, **kw) -> "FixtureProvider":
        return cls(json.loads(Path(path).read_text()), **kw)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, self.provider_type, "connected",
                              latency_ms=1, message="fixture")

    async def list_services(self) -> list[str]:
        return list(self.data.get("services") or [])

    async def query_logs(self, q: LogQuery) -> LogPage:
        records = [
            LogRecord.make(self.provider_id, self.provider_type, r["timestamp"],
                           r.get("level", "INFO"), r.get("service", q.service),
                           r.get("body", ""), labels=r.get("labels", {}),
                           source_url=r.get("source_url", ""))
            for r in self.data.get("logs", [])
        ]
        if q.filter:
            records = [r for r in records if q.filter.lower() in r.body.lower()]
        return LogPage(records=records[: q.limit], total_estimate=len(records), query_ms=1)

    async def query_metrics(self, q: MetricQuery) -> list[MetricSeries]:
        out = []
        for s in self.data.get("metrics", []):
            series = MetricSeries(
                series_id=s.get("series_id", s.get("metric", "m")),
                provider_id=self.provider_id, provider_type=self.provider_type,
                metric=s.get("metric", ""), service=s.get("service", q.service),
                unit=s.get("unit", ""), labels=s.get("labels", {}),
                points=[MetricPoint(p["timestamp"], float(p["value"]))
                        for p in s.get("points", [])],
                source_url=s.get("source_url", ""))
            out.append(series.compute_stats())
        return out

    async def query_traces(self, q: TraceQuery) -> list[TraceSummary]:
        return [TraceSummary(
            trace_id=t.get("trace_id", ""), provider_id=self.provider_id,
            provider_type=self.provider_type, root_service=t.get("root_service", q.service),
            root_operation=t.get("root_operation", ""), start_time=t.get("start_time", ""),
            duration_ms=float(t.get("duration_ms", 0)), span_count=t.get("span_count", 0),
            error_count=t.get("error_count", 0), status=t.get("status", "ok"),
            services_touched=t.get("services_touched", []),
            source_url=t.get("source_url", ""))
            for t in self.data.get("traces", [])]

    async def recent_deploys(self, q: EventQuery) -> list[EventRecord]:
        return [EventRecord.make(
            self.provider_id, self.provider_type, e.get("kind", "deploy"),
            e["timestamp"], e.get("service", q.service), e.get("title", ""),
            description=e.get("description", ""), version=e.get("version", ""),
            actor=e.get("actor", ""), labels=e.get("labels", {}),
            source_url=e.get("source_url", ""))
            for e in self.data.get("events", [])]

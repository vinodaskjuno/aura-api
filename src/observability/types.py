"""Normalized observability signal types.

Every provider adapter converts its native wire format into these shapes, so agents
never branch on which vendor produced a record.

The cardinal rule enforced throughout: an LLM sees `evidence_id + summary + timestamp
+ service` and nothing else. `payload` and `raw` never enter a prompt.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

Signal = Literal["logs", "metrics", "traces", "events"]
SIGNALS: tuple[Signal, ...] = ("logs", "metrics", "traces", "events")

EventKind = Literal[
    "deploy", "config_change", "scale", "incident", "alert", "k8s_event", "release",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(*parts: Any, length: int = 12) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


# ── Time ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TimeWindow:
    start: str        # ISO8601 UTC
    end: str          # ISO8601 UTC

    @staticmethod
    def parse(value: str) -> datetime:
        v = (value or "").strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @classmethod
    def last(cls, minutes: int) -> "TimeWindow":
        end = datetime.now(timezone.utc)
        return cls(start=(end - timedelta(minutes=minutes)).isoformat(), end=end.isoformat())

    @property
    def start_dt(self) -> datetime:
        return self.parse(self.start)

    @property
    def end_dt(self) -> datetime:
        return self.parse(self.end)

    def duration_s(self) -> int:
        return max(0, int((self.end_dt - self.start_dt).total_seconds()))

    def widen(self, before_s: int = 0, after_s: int = 0) -> "TimeWindow":
        return TimeWindow(
            start=(self.start_dt - timedelta(seconds=before_s)).isoformat(),
            end=(self.end_dt + timedelta(seconds=after_s)).isoformat(),
        )

    def contains(self, ts: str) -> bool:
        try:
            t = self.parse(ts)
        except Exception:  # noqa: BLE001
            return False
        return self.start_dt <= t <= self.end_dt

    def epoch_ns(self) -> tuple[int, int]:
        return (int(self.start_dt.timestamp() * 1e9), int(self.end_dt.timestamp() * 1e9))

    def epoch_s(self) -> tuple[int, int]:
        return (int(self.start_dt.timestamp()), int(self.end_dt.timestamp()))

    def epoch_ms(self) -> tuple[int, int]:
        return (int(self.start_dt.timestamp() * 1000), int(self.end_dt.timestamp() * 1000))

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end}


# ── Queries ──────────────────────────────────────────────────────────────────
# No provider-neutral DSL. `filter` is a plain substring and `labels` is exact-match
# k/v; `raw_query` is a documented, explicitly non-portable escape hatch. LogQL,
# PromQL, DQL, Lucene and KQL do not unify, and pretending otherwise produces a
# language that can't express the query you actually need at 3am.

@dataclass(frozen=True)
class LogQuery:
    service: str
    window: TimeWindow
    filter: str = ""
    levels: tuple[str, ...] = ("ERROR", "FATAL", "WARN")
    labels: dict[str, str] = field(default_factory=dict)
    limit: int = 500
    cursor: str | None = None
    raw_query: str | None = None


@dataclass(frozen=True)
class MetricQuery:
    service: str
    window: TimeWindow
    metric: str = ""
    step_s: int = 30
    aggregation: str = "avg"
    labels: dict[str, str] = field(default_factory=dict)
    raw_query: str | None = None


@dataclass(frozen=True)
class TraceQuery:
    service: str
    window: TimeWindow
    min_duration_ms: int = 0
    errors_only: bool = False
    trace_id: str = ""
    limit: int = 20
    labels: dict[str, str] = field(default_factory=dict)
    raw_query: str | None = None


@dataclass(frozen=True)
class EventQuery:
    service: str
    window: TimeWindow
    kinds: tuple[str, ...] = ("deploy", "config_change", "scale", "k8s_event")
    limit: int = 100
    labels: dict[str, str] = field(default_factory=dict)
    raw_query: str | None = None


# ── Normalized records ───────────────────────────────────────────────────────

@dataclass
class LogRecord:
    record_id: str
    provider_id: str
    provider_type: str
    timestamp: str
    level: str
    service: str
    body: str
    truncated: bool = False
    labels: dict[str, str] = field(default_factory=dict)
    trace_id: str = ""
    source_url: str = ""
    raw: dict = field(default_factory=dict)   # NEVER enters a prompt

    @classmethod
    def make(cls, provider_id: str, provider_type: str, timestamp: str, level: str,
             service: str, body: str, labels: dict | None = None,
             trace_id: str = "", source_url: str = "", raw: dict | None = None,
             max_body: int = 4000) -> "LogRecord":
        text = body or ""
        truncated = len(text) > max_body
        stream = json.dumps(labels or {}, sort_keys=True)
        return cls(
            record_id=_digest(provider_id, timestamp, stream, text[:512], length=16),
            provider_id=provider_id, provider_type=provider_type,
            timestamp=timestamp, level=(level or "UNKNOWN").upper(), service=service,
            body=text[:max_body], truncated=truncated, labels=labels or {},
            trace_id=trace_id, source_url=source_url, raw=raw or {},
        )


@dataclass
class LogPage:
    records: list[LogRecord] = field(default_factory=list)
    cursor: str | None = None
    total_estimate: int | None = None
    unsupported: bool = False
    error: str | None = None
    query_ms: int = 0


@dataclass
class MetricPoint:
    timestamp: str
    value: float


@dataclass
class MetricSeries:
    series_id: str
    provider_id: str
    provider_type: str
    metric: str
    service: str
    unit: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    points: list[MetricPoint] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    source_url: str = ""

    def compute_stats(self) -> "MetricSeries":
        """Summarise so the LLM never sees a raw point array.

        A 1h window at 15s step is 240 points per series: pure token waste, and the
        model reasons measurably worse over it than over {"p95": 2840, "delta_pct": 412}.
        """
        vals = [p.value for p in self.points if p.value is not None]
        if not vals:
            self.stats = {"count": 0}
            return self
        s = sorted(vals)
        n = len(s)

        def pct(q: float) -> float:
            if n == 1:
                return s[0]
            idx = min(n - 1, max(0, int(round(q * (n - 1)))))
            return s[idx]

        first_half = vals[: max(1, n // 2)]
        second_half = vals[max(1, n // 2):] or first_half
        mean_a = sum(first_half) / len(first_half)
        mean_b = sum(second_half) / len(second_half)
        delta_pct = ((mean_b - mean_a) / abs(mean_a) * 100.0) if mean_a else 0.0

        self.stats = {
            "count": n,
            "min": round(s[0], 4),
            "max": round(s[-1], 4),
            "mean": round(sum(vals) / n, 4),
            "p50": round(pct(0.50), 4),
            "p95": round(pct(0.95), 4),
            "last": round(vals[-1], 4),
            "delta_pct": round(delta_pct, 2),
        }
        return self


@dataclass
class SpanSummary:
    span_id: str
    operation: str
    service: str
    duration_ms: float
    status: str = "ok"
    error_message: str = ""


@dataclass
class TraceSummary:
    trace_id: str
    provider_id: str
    provider_type: str
    root_service: str
    root_operation: str
    start_time: str
    duration_ms: float
    span_count: int = 0
    error_count: int = 0
    status: str = "ok"
    services_touched: list[str] = field(default_factory=list)
    slowest_spans: list[SpanSummary] = field(default_factory=list)
    source_url: str = ""


@dataclass
class EventRecord:
    event_id: str
    provider_id: str
    provider_type: str
    kind: str
    timestamp: str
    service: str
    title: str
    description: str = ""
    actor: str = ""
    version: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    source_url: str = ""

    @classmethod
    def make(cls, provider_id: str, provider_type: str, kind: str, timestamp: str,
             service: str, title: str, **kw) -> "EventRecord":
        return cls(
            event_id=_digest(provider_id, kind, timestamp, service, title, length=16),
            provider_id=provider_id, provider_type=provider_type, kind=kind,
            timestamp=timestamp, service=service, title=title, **kw,
        )


# ── Provider health ──────────────────────────────────────────────────────────

@dataclass
class ProviderHealth:
    provider_id: str
    provider_type: str
    status: Literal["connected", "degraded", "failed", "not_configured"]
    latency_ms: int = 0
    message: str = ""
    checked_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Evidence ─────────────────────────────────────────────────────────────────

@dataclass
class EvidenceRecord:
    """The atom of the whole feature. Deterministic ids make rerun-dedupe and eval
    citation assertions possible; without them neither works."""
    evidence_id: str
    investigation_id: str
    signal: str
    provider_id: str
    provider_type: str
    service: str
    timestamp: str
    title: str
    summary: str                       # <= 280 chars — the ONLY field an LLM sees
    payload: dict = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    source_url: str = ""
    masked_fields: list[str] = field(default_factory=list)
    weight: float = 1.0
    collected_at: str = field(default_factory=_now)

    @classmethod
    def make(cls, investigation_id: str, signal: str, provider_id: str,
             provider_type: str, service: str, timestamp: str, title: str,
             summary: str, natural_key: str, payload: dict | None = None,
             labels: dict | None = None, source_url: str = "",
             weight: float = 1.0) -> "EvidenceRecord":
        return cls(
            evidence_id="ev_" + _digest(provider_id, signal, natural_key),
            investigation_id=investigation_id, signal=signal, provider_id=provider_id,
            provider_type=provider_type, service=service, timestamp=timestamp,
            title=title[:200], summary=(summary or "")[:280],
            payload=payload or {}, labels=labels or {}, source_url=source_url,
            weight=weight,
        )

    def index_row(self) -> dict:
        """The lightweight projection stored in DynamoDB and streamed to the SPA."""
        return {
            "evidenceId": self.evidence_id,
            "investigationId": self.investigation_id,
            "signal": self.signal,
            "kind": _SIGNAL_TO_KIND.get(self.signal, self.signal),
            "provider": self.provider_type,
            "service": self.service,
            "timestamp": self.timestamp,
            "title": self.title,
            "summary": self.summary,
            "sourceUrl": self.source_url,
            "hasMasked": bool(self.masked_fields),
            "weight": self.weight,
        }

    def to_dict(self) -> dict:
        return asdict(self)


# The SPA groups evidence by "kind"; deploy/config are both `events` signals.
_SIGNAL_TO_KIND = {"logs": "log", "metrics": "metric", "traces": "trace", "events": "deploy"}


# ── Investigation spec ───────────────────────────────────────────────────────

@dataclass
class InvestigationSpec:
    """Typed payload carried in `AgentContext.extra["investigation"]`.

    Stored in `extra` rather than as new AgentContext fields on purpose: ~350
    `from src.…` imports construct AgentContext, and widening it would ripple.
    """
    investigation_id: str
    title: str = ""
    services: list[str] = field(default_factory=list)
    window: TimeWindow = field(default_factory=lambda: TimeWindow.last(60))
    symptom: str = ""
    incident_id: str = ""
    severity: str = "high"
    provider_ids: list[str] = field(default_factory=list)
    runbook_id: str = ""
    project_id: str = ""
    filter: str = ""
    masking_enabled: bool = True

    @property
    def service(self) -> str:
        return self.services[0] if self.services else ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["window"] = self.window.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "InvestigationSpec":
        w = d.get("window") or {}
        window = (TimeWindow(start=w["start"], end=w["end"])
                  if w.get("start") and w.get("end") else TimeWindow.last(60))
        return cls(
            investigation_id=d.get("investigation_id") or d.get("investigationId", ""),
            title=d.get("title", ""),
            services=list(d.get("services") or []),
            window=window,
            symptom=d.get("symptom", ""),
            incident_id=d.get("incident_id") or d.get("incidentId", ""),
            severity=d.get("severity", "high"),
            provider_ids=list(d.get("provider_ids") or d.get("providerIds") or []),
            runbook_id=d.get("runbook_id") or d.get("runbookId", ""),
            project_id=d.get("project_id") or d.get("projectId", ""),
            filter=d.get("filter", ""),
            masking_enabled=bool(d.get("masking_enabled", True)),
        )

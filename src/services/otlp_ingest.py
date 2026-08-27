"""OTLP ingest — parse Claude Code OpenTelemetry exports into AURA usage records.

Claude Code emits native telemetry when CLAUDE_CODE_ENABLE_TELEMETRY=1. AURA
receives it over OTLP/HTTP + JSON (no protobuf dependency) and turns it into
token-usage rows plus daily rollups.

═══════════════════════════════════════════════════════════════════════════════
WIRE SHAPES — captured empirically from Claude Code 2.1.247 with
OTEL_METRICS_EXPORTER=console / OTEL_LOGS_EXPORTER=console.
Anthropic documents the metric *names* but not the attribute keys, so this is
the ground truth this parser is built against. Re-run the console exporter and
update this block if a Claude Code upgrade changes anything.

Resource attributes (both signals):
    host.arch, os.type, os.version, service.name="claude-code", service.version

Common data-point / log attributes:
    user.id (hashed), session.id, organization.id, user.email,
    user.account_uuid, user.account_id, terminal.type
    app.entrypoint  — ONLY when OTEL_METRICS_INCLUDE_ENTRYPOINT=true

METRICS (all COUNTER, delta temporality by default):
    claude_code.token.usage     unit=tokens
        + model         e.g. "claude-haiku-4-5-20251001", "claude-opus-5[1m]"
        + type          "input" | "output" | "cacheRead" | "cacheCreation"
        + query_source  "main" | "auxiliary" | "sdk" | "generate_session_title"
        + effort        e.g. "high"   (main queries only)
    claude_code.cost.usage      unit=USD    + model, query_source  (NO type)
    claude_code.session.count               + start_type="fresh"
    claude_code.active_time.total unit=s    + type="cli"

EVENTS (logs exporter, body.stringValue = the event name):
    claude_code.api_request   ← PRIMARY SOURCE for usage rows
        model, input_tokens, output_tokens, cache_read_tokens,
        cache_creation_tokens, cost_usd, cost_usd_micros, duration_ms,
        request_id, client_request_id, speed, query_source,
        event.name, event.timestamp, event.sequence, prompt.id
    claude_code.user_prompt        prompt_length, prompt="<REDACTED>" unless
                                   OTEL_LOG_USER_PROMPTS=1 (we never set it)
    claude_code.assistant_response
    claude_code.api_error          (not yet observed in capture)

WHY EVENTS ARE PRIMARY, NOT METRICS
    api_request gives one row per request with Claude Code's own cost_usd, a
    stable request_id for idempotency, and duration_ms. Metrics are pre-summed
    per export window with no request identity, and their cacheCreation type
    does not distinguish 5-minute from 1-hour cache writes (priced 1.25x vs 2x
    of input), so cost computed from metric tokens would be wrong. We therefore
    trust Claude Code's reported cost and use metrics only to reconcile.
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from src.config_settings import calculate_cost_v2, normalize_model_id

log = logging.getLogger(__name__)

# Metric names we understand. Anything else is ignored (forward-compatible).
TOKEN_METRIC = "claude_code.token.usage"
COST_METRIC = "claude_code.cost.usage"
SESSION_METRIC = "claude_code.session.count"
ACTIVE_TIME_METRIC = "claude_code.active_time.total"

API_REQUEST_EVENT = "claude_code.api_request"
API_ERROR_EVENT = "claude_code.api_error"

# claude_code.token.usage `type` → our field name.
_TOKEN_TYPE_FIELDS: dict[str, str] = {
    "input": "inputTokens",
    "output": "outputTokens",
    "cacheread": "cacheReadTokens",
    "cachecreation": "cacheCreationTokens",
}

# Attribute aliases. Claude Code currently uses the first spelling in each
# list; the alternatives guard against a rename in a future version.
_MODEL_KEYS = ("model", "gen_ai.request.model", "gen_ai.response.model")
_TYPE_KEYS = ("type", "token_type", "claude_code.token.type")
_SESSION_KEYS = ("session.id", "session_id")


# ─────────────────────────────────────────────────────────────────────────────
# OTLP/JSON primitives
# ─────────────────────────────────────────────────────────────────────────────

def _anyval(v: Any) -> Any:
    """Decode an OTLP AnyValue into a plain Python value."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return 0
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return 0.0
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [_anyval(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return _attrs(v["kvlistValue"].get("values", []))
    return None


def _attrs(items: Iterable[dict] | None) -> dict[str, Any]:
    """Flatten an OTLP KeyValue list into a dict."""
    out: dict[str, Any] = {}
    for kv in items or []:
        if isinstance(kv, dict) and "key" in kv:
            out[kv["key"]] = _anyval(kv.get("value"))
    return out


def _first(d: dict, keys: tuple[str, ...], default: Any = "") -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def _point_value(dp: dict) -> float:
    """Numeric value of a NumberDataPoint (asInt or asDouble)."""
    if "asInt" in dp:
        try:
            return float(int(dp["asInt"]))
        except (TypeError, ValueError):
            return 0.0
    if "asDouble" in dp:
        try:
            return float(dp["asDouble"])
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _ns_to_iso(ns: Any) -> str:
    """Convert a UnixNano timestamp (string or int) to an ISO-8601 UTC string."""
    try:
        seconds = int(ns) / 1_000_000_000
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _iter_number_points(metric: dict) -> Iterable[dict]:
    """Yield data points from whichever numeric shape the metric uses."""
    for kind in ("sum", "gauge"):
        block = metric.get(kind)
        if isinstance(block, dict):
            for dp in block.get("dataPoints", []) or []:
                yield dp


# ─────────────────────────────────────────────────────────────────────────────
# Tool attribution
# ─────────────────────────────────────────────────────────────────────────────

def tool_label(attrs: dict, resource: dict) -> str:
    """Classify which Claude Code surface produced this record.

    app.entrypoint is only present when OTEL_METRICS_INCLUDE_ENTRYPOINT=true,
    so fall back to terminal.type, which distinguishes the IDE from a TTY.
    """
    entrypoint = str(attrs.get("app.entrypoint") or "").lower()
    if entrypoint:
        if "vscode" in entrypoint or "extension" in entrypoint or "ide" in entrypoint:
            return "claude-code-ext"
        if "sdk" in entrypoint:
            return "claude-code-sdk"
        return "claude-code-cli"

    terminal = str(attrs.get("terminal.type") or "").lower()
    if "vscode" in terminal or "ide" in terminal:
        return "claude-code-ext"
    return "claude-code-cli"


# ─────────────────────────────────────────────────────────────────────────────
# Usage record
# ─────────────────────────────────────────────────────────────────────────────

class UsageRecord:
    """One normalized usage observation, ready to persist."""

    __slots__ = (
        "dedup_key", "timestamp", "session_id", "model", "tool", "query_source",
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_creation_tokens", "reported_cost_usd", "computed_cost_usd",
        "duration_ms", "status_code", "raw_attrs",
    )

    def __init__(self, **kw: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    @property
    def date(self) -> str:
        return (self.timestamp or "")[:10]

    @property
    def total_tokens(self) -> int:
        return (
            (self.input_tokens or 0) + (self.output_tokens or 0)
            + (self.cache_read_tokens or 0) + (self.cache_creation_tokens or 0)
        )

    @property
    def cost_usd(self) -> float:
        """Claude Code's own cost when present, else our computed estimate."""
        if self.reported_cost_usd is not None:
            return float(self.reported_cost_usd)
        return float(self.computed_cost_usd or 0.0)

    def as_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}


def _dedup_key(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# Logs / events  →  per-request usage rows  (PRIMARY)
# ─────────────────────────────────────────────────────────────────────────────

def parse_logs(payload: dict) -> list[UsageRecord]:
    """Extract usage records from claude_code.api_request / api_error events."""
    records: list[UsageRecord] = []

    for rl in payload.get("resourceLogs", []) or []:
        resource = _attrs((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs", []) or []:
            for rec in sl.get("logRecords", []) or []:
                event = _anyval(rec.get("body")) or ""
                if event not in (API_REQUEST_EVENT, API_ERROR_EVENT):
                    continue

                attrs = _attrs(rec.get("attributes"))
                model = normalize_model_id(str(_first(attrs, _MODEL_KEYS, "unknown")))

                inp = int(attrs.get("input_tokens") or 0)
                out = int(attrs.get("output_tokens") or 0)
                c_read = int(attrs.get("cache_read_tokens") or 0)
                c_make = int(attrs.get("cache_creation_tokens") or 0)

                # Prefer cost_usd_micros — integer, no float-repr drift.
                reported: float | None = None
                if attrs.get("cost_usd_micros") is not None:
                    reported = int(attrs["cost_usd_micros"]) / 1_000_000
                elif attrs.get("cost_usd") is not None:
                    reported = float(attrs["cost_usd"])

                timestamp = (
                    attrs.get("event.timestamp")
                    or _ns_to_iso(rec.get("timeUnixNano") or rec.get("observedTimeUnixNano"))
                )

                # request_id is stable per upstream API call — the ideal dedup key.
                request_id = attrs.get("request_id") or attrs.get("client_request_id")
                session_id = str(_first(attrs, _SESSION_KEYS, ""))
                if request_id:
                    dedup = _dedup_key("req", request_id)
                else:
                    dedup = _dedup_key(
                        "evt", session_id, attrs.get("prompt.id", ""),
                        attrs.get("event.sequence", ""), timestamp, model,
                    )

                records.append(UsageRecord(
                    dedup_key=dedup,
                    timestamp=timestamp,
                    session_id=session_id,
                    model=model,
                    tool=tool_label(attrs, resource),
                    query_source=str(attrs.get("query_source") or ""),
                    input_tokens=inp,
                    output_tokens=out,
                    cache_read_tokens=c_read,
                    cache_creation_tokens=c_make,
                    reported_cost_usd=reported,
                    # Lower bound: the event reports one cache_creation_tokens
                    # figure without splitting 5-minute (1.25x) from 1-hour (2x)
                    # cache writes, so this prices all of it at the 5-minute rate.
                    # On cache-heavy traffic it will read below reported cost —
                    # that is expected, not a stale rate card. Reconciliation
                    # should compare on non-cache traffic only.
                    computed_cost_usd=calculate_cost_v2(model, inp, out, c_read, c_make),
                    duration_ms=int(attrs.get("duration_ms") or 0),
                    status_code=500 if event == API_ERROR_EVENT else 200,
                    raw_attrs={
                        k: v for k, v in attrs.items()
                        if k in ("speed", "effort", "prompt.id", "request_id",
                                 "error", "error_type", "status_code")
                    },
                ))

    return records


# ─────────────────────────────────────────────────────────────────────────────
# Metrics  →  reconciliation totals  (SECONDARY)
# ─────────────────────────────────────────────────────────────────────────────

def parse_metrics(payload: dict) -> list[UsageRecord]:
    """Fold claude_code.token.usage / cost.usage data points into per-window rows.

    One record per (session, model, query_source, export window).

    RECONCILIATION ONLY -- these must never be added to the usage counters. The
    same API call is reported through both signals, so counting both doubles
    every figure; see usage_rollup.record_reconciliation.
    """
    buckets: dict[tuple, UsageRecord] = {}

    for rm in payload.get("resourceMetrics", []) or []:
        resource = _attrs((rm.get("resource") or {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []) or []:
            for metric in sm.get("metrics", []) or []:
                name = metric.get("name") or ""
                if name not in (TOKEN_METRIC, COST_METRIC):
                    continue

                for dp in _iter_number_points(metric):
                    attrs = _attrs(dp.get("attributes"))
                    model = normalize_model_id(str(_first(attrs, _MODEL_KEYS, "unknown")))
                    session_id = str(_first(attrs, _SESSION_KEYS, ""))
                    query_source = str(attrs.get("query_source") or "")
                    start_ns = dp.get("startTimeUnixNano", "")
                    end_ns = dp.get("timeUnixNano", "")

                    key = (session_id, model, query_source, start_ns, end_ns)
                    rec = buckets.get(key)
                    if rec is None:
                        rec = UsageRecord(
                            dedup_key=_dedup_key(
                                "mtr", session_id, model, query_source, start_ns, end_ns,
                            ),
                            timestamp=_ns_to_iso(end_ns),
                            session_id=session_id,
                            model=model,
                            tool=tool_label(attrs, resource),
                            query_source=query_source,
                            input_tokens=0, output_tokens=0,
                            cache_read_tokens=0, cache_creation_tokens=0,
                            reported_cost_usd=None, computed_cost_usd=0.0,
                            duration_ms=0, status_code=200, raw_attrs={},
                        )
                        buckets[key] = rec

                    value = _point_value(dp)
                    if name == COST_METRIC:
                        rec.reported_cost_usd = (rec.reported_cost_usd or 0.0) + value
                        continue

                    token_type = str(_first(attrs, _TYPE_KEYS, "")).replace("_", "").lower()
                    field = _TOKEN_TYPE_FIELDS.get(token_type)
                    if field is None:
                        log.debug("otlp_ingest: unknown token type %r on %s", token_type, name)
                        continue
                    slot = {
                        "inputTokens": "input_tokens",
                        "outputTokens": "output_tokens",
                        "cacheReadTokens": "cache_read_tokens",
                        "cacheCreationTokens": "cache_creation_tokens",
                    }[field]
                    setattr(rec, slot, int(getattr(rec, slot) or 0) + int(value))

    for rec in buckets.values():
        rec.computed_cost_usd = calculate_cost_v2(
            rec.model, rec.input_tokens, rec.output_tokens,
            rec.cache_read_tokens, rec.cache_creation_tokens,
        )

    return [r for r in buckets.values() if r.total_tokens > 0 or r.reported_cost_usd]


def summarize_metrics(payload: dict) -> dict:
    """Session / active-time counters, for the sessions view."""
    sessions = 0
    active_seconds = 0.0
    for rm in payload.get("resourceMetrics", []) or []:
        for sm in rm.get("scopeMetrics", []) or []:
            for metric in sm.get("metrics", []) or []:
                name = metric.get("name") or ""
                if name not in (SESSION_METRIC, ACTIVE_TIME_METRIC):
                    continue
                for dp in _iter_number_points(metric):
                    if name == SESSION_METRIC:
                        sessions += int(_point_value(dp))
                    else:
                        active_seconds += _point_value(dp)
    return {"sessions": sessions, "activeSeconds": round(active_seconds, 3)}

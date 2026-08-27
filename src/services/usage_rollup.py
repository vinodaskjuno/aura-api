"""Daily usage rollups — the read path behind every usage dashboard.

Raw per-request rows live in `token-usage`; this module maintains pre-aggregated
counters in `usage-daily` so the dashboards never scan.

WHY ROLLUPS
    The previous analytics path did scan_items(limit=5000) and then filtered in
    Python. That silently truncates once a single busy day exceeds the cap — the
    dashboard shows a plausible number that is simply wrong — and it pays to read
    rows it throws away. Claude Code emits telemetry every 60s per active session,
    so that ceiling arrives fast.

KEY LAYOUT (table `usage-daily`, PK `pk`, SK `sk`)
    per-user   pk = "<userId>"   sk = "<date>#<tool>#<model>"
    org-wide   pk = "__org__"    sk = "<date>#<tool>#<model>#<userId>"

    Both a user view and the admin view are then a single Query on one partition
    with a sort-key prefix — no scans, no truncation.

IDEMPOTENCY
    ADD is not idempotent, and OTLP delivery is at-least-once. record_usage()
    therefore writes the raw event under a conditional put keyed on the event's
    dedup key and only bumps the counters when that put actually created a row.
    A retried export re-writes nothing and re-counts nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.config_settings import get_settings
from src.database import dynamo_client as db

log = logging.getLogger(__name__)

ORG_PK = "__org__"

# Highest code point, appended to make an inclusive upper bound on a
# sort-key range: "2026-08-27" + _SK_MAX sorts after every "2026-08-27#..."
_SK_MAX = "\uffff"

# Counter fields maintained on every rollup row, split by numeric type so
# coercion never has to guess from the field name. (A substring test on "Cost"
# silently truncated costUsd to 0 via int() — floats must be listed explicitly.)
FLOAT_FIELDS = ("costUsd", "reportedCostUsd")
INT_FIELDS = (
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheCreationTokens",
    "calls",
    "durationMs",
    "errors",
)
COUNTER_FIELDS = INT_FIELDS + FLOAT_FIELDS


def _rollup_table() -> str:
    return get_settings().usage_rollup_table


# ─────────────────────────────────────────────────────────────────────────────
# Write path
# ─────────────────────────────────────────────────────────────────────────────

def record_usage(user_id: str, rec, *, source: str = "claude-code-otel") -> bool:
    """Persist one usage observation and fold it into the daily rollups.

    `rec` is an otlp_ingest.UsageRecord. Returns True when the record was new
    (and therefore counted), False when it was a duplicate delivery.
    """
    date = rec.date or datetime.now(timezone.utc).date().isoformat()
    model = rec.model or "unknown"
    tool = rec.tool or "unknown"

    raw = {
        "userId": user_id,
        # dedup key in the sort key makes the conditional put the dedup gate
        "sortKey": f"{rec.timestamp}#{rec.dedup_key}",
        "sessionId": rec.session_id or "",
        "projectId": "",
        "model": model,
        "tool": tool,
        "source": source,
        "querySource": rec.query_source or "",
        "inputTokens": int(rec.input_tokens or 0),
        "outputTokens": int(rec.output_tokens or 0),
        "cacheReadTokens": int(rec.cache_read_tokens or 0),
        "cacheCreationTokens": int(rec.cache_creation_tokens or 0),
        # cost kept as a string, matching the existing token-usage convention
        "cost": str(round(rec.cost_usd, 6)),
        "computedCost": str(round(float(rec.computed_cost_usd or 0.0), 6)),
        "reportedCost": (
            str(round(float(rec.reported_cost_usd), 6))
            if rec.reported_cost_usd is not None else ""
        ),
        "latencyMs": int(rec.duration_ms or 0),
        "statusCode": int(rec.status_code or 200),
        "timestamp": rec.timestamp,
        "ttl": _ttl_90_days(),
    }
    if rec.raw_attrs:
        raw["attrs"] = {k: str(v) for k, v in rec.raw_attrs.items()}

    try:
        is_new = db.put_item_if_absent("token-usage", raw, ("userId", "sortKey"))
    except Exception as exc:
        log.warning("usage_rollup: raw write failed for %s: %s", user_id, exc)
        return False

    if not is_new:
        log.debug("usage_rollup: duplicate delivery %s, rollup skipped", rec.dedup_key)
        return False

    deltas = {
        "inputTokens": int(rec.input_tokens or 0),
        "outputTokens": int(rec.output_tokens or 0),
        "cacheReadTokens": int(rec.cache_read_tokens or 0),
        "cacheCreationTokens": int(rec.cache_creation_tokens or 0),
        "costUsd": round(rec.cost_usd, 6),
        "calls": 1,
        "durationMs": int(rec.duration_ms or 0),
        "errors": 1 if int(rec.status_code or 200) >= 400 else 0,
    }
    if rec.reported_cost_usd is not None:
        deltas["reportedCostUsd"] = round(float(rec.reported_cost_usd), 6)

    bump(user_id, date, tool, model, deltas)
    return True


def record_reconciliation(user_id: str, rec) -> None:
    """Record a metric-derived total for cross-checking, WITHOUT touching usage.

    Claude Code reports the same API call through BOTH signals: the
    claude_code.api_request event and the claude_code.token.usage /
    claude_code.cost.usage metrics. Adding both to the same counters
    double-counts every request -- verified against a live session, where one
    call produced an event at $0.083558 and a metric at $0.083557.

    Events are authoritative (per-request identity, a stable request_id, and the
    1-hour vs 5-minute cache split), so metric totals land in a separate
    "recon#" sort-key namespace. Dashboards read usage rows; recon rows exist
    only to detect dropped events by comparing the two totals.
    """
    date = rec.date or datetime.now(timezone.utc).date().isoformat()
    model = rec.model or "unknown"
    tool = rec.tool or "unknown"

    try:
        db.atomic_add_many(
            _rollup_table(),
            {"pk": user_id, "sk": f"recon#{date}#{tool}#{model}"},
            {
                "inputTokens": int(rec.input_tokens or 0),
                "outputTokens": int(rec.output_tokens or 0),
                "cacheReadTokens": int(rec.cache_read_tokens or 0),
                "cacheCreationTokens": int(rec.cache_creation_tokens or 0),
                "reportedCostUsd": round(float(rec.reported_cost_usd or 0.0), 6),
                "calls": 1,
            },
            set_fields={"date": date, "tool": tool, "model": model,
                        "userId": user_id, "kind": "reconciliation"},
        )
    except Exception as exc:
        log.warning("usage_rollup: reconciliation write failed for %s: %s", user_id, exc)


def bump(user_id: str, date: str, tool: str, model: str, deltas: dict) -> None:
    """Increment the per-user and org-wide rollup rows for one (date, tool, model)."""
    table = _rollup_table()

    # Labels are written alongside the counters so a reader never has to parse
    # the composite sort key back apart.
    labels = {"date": date, "tool": tool, "model": model}

    try:
        db.atomic_add_many(
            table,
            {"pk": user_id, "sk": f"{date}#{tool}#{model}"},
            deltas,
            set_fields={**labels, "userId": user_id},
        )
    except Exception as exc:
        log.warning("usage_rollup: user rollup failed for %s: %s", user_id, exc)

    try:
        db.atomic_add_many(
            table,
            {"pk": ORG_PK, "sk": f"{date}#{tool}#{model}#{user_id}"},
            deltas,
            set_fields={**labels, "userId": user_id},
        )
    except Exception as exc:
        log.warning("usage_rollup: org rollup failed for %s: %s", user_id, exc)


def _ttl_90_days() -> int:
    from time import time as _time
    return int(_time()) + 90 * 24 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# Read path
# ─────────────────────────────────────────────────────────────────────────────

# Period token -> number of days back. None means "no lower bound".
PERIOD_DAYS: dict[str, int | None] = {
    "today": 0,
    "7d": 6,
    "week": 6,
    "14d": 13,
    "30d": 29,
    "month": 29,
    "90d": 89,
    "all": None,
}


def period_bounds(period: str) -> tuple[str | None, str]:
    """Inclusive (from_date, to_date) as YYYY-MM-DD for a period token."""
    today = datetime.now(timezone.utc).date()
    days = PERIOD_DAYS.get(period, 6)
    if days is None:
        return None, today.isoformat()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def read_rollups(user_id: str | None, period: str = "7d", is_admin: bool = False) -> list[dict]:
    """Rollup rows for a period. Admins get every user's rows; users get their own."""
    from_date, to_date = period_bounds(period)
    pk = ORG_PK if is_admin else (user_id or "")
    if not pk:
        return []

    # Sort keys start with the ISO date, so a lexicographic range is a date range.
    # _SK_MAX as the upper bound includes everything on to_date itself.
    rows = db.query_range(
        _rollup_table(),
        pk_name="pk", pk_value=pk, sk_name="sk",
        sk_from=from_date if from_date else None,
        sk_to=to_date + _SK_MAX,
        limit=20_000,
    )
    # "recon#..." rows are metric-derived cross-check totals for the SAME
    # requests; including them would double every usage figure. They sort
    # after the date-prefixed usage rows, so filter rather than re-range.
    return [_coerce(r) for r in rows
            if not str(r.get("sk", "")).startswith("recon#")]


def read_reconciliation(user_id: str, period: str = "7d") -> list[dict]:
    """Metric-derived totals, for comparing against event-derived usage."""
    from_date, to_date = period_bounds(period)
    rows = db.query_range(
        _rollup_table(),
        pk_name="pk", pk_value=user_id, sk_name="sk",
        sk_from=f"recon#{from_date}" if from_date else "recon#",
        sk_to="recon#" + to_date + _SK_MAX,
        limit=20_000,
    )
    return [_coerce(r) for r in rows]


def _coerce(row: dict) -> dict:
    """Normalize a stored row: Decimals to int/float, counters always present."""
    out: dict = {
        "date": str(row.get("date") or (row.get("sk") or "")[:10]),
        "tool": str(row.get("tool") or "unknown"),
        "model": str(row.get("model") or "unknown"),
        "userId": str(row.get("userId") or row.get("pk") or ""),
    }
    for field in INT_FIELDS:
        try:
            out[field] = int(row.get(field, 0) or 0)
        except (TypeError, ValueError):
            out[field] = 0
    for field in FLOAT_FIELDS:
        try:
            out[field] = float(row.get(field, 0) or 0)
        except (TypeError, ValueError):
            out[field] = 0.0
    out["totalTokens"] = (
        out["inputTokens"] + out["outputTokens"]
        + out["cacheReadTokens"] + out["cacheCreationTokens"]
    )
    return out

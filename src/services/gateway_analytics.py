"""Gateway / usage analytics — aggregation helpers for the AIOps Gateway tab.

Reads the pre-aggregated `usage-daily` rollups maintained by usage_rollup, not
the raw event table. The previous implementation scanned `token-usage` with a
row cap and filtered in Python, which silently truncated once a busy day passed
the cap and charged for rows it discarded. Rollup reads are a single Query on
one partition with a sort-key date range.

Response shapes are supersets of the originals — existing callers keep working;
the added fields are the cache-token counts, which are the majority of Claude
Code's token volume and were previously invisible.

Audit-log drill-down still reads raw `token-usage` rows: it wants individual
requests, which is exactly what rollups have collapsed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.database import dynamo_client as db
from src.services import usage_rollup

log = logging.getLogger(__name__)

_TOKEN_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens")


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rows(user_id: str | None, period: str, is_admin: bool) -> list[dict]:
    return usage_rollup.read_rollups(user_id, period, is_admin)


def _blank(**identity) -> dict:
    return {
        **identity,
        "inputTokens": 0, "outputTokens": 0,
        "cacheReadTokens": 0, "cacheCreationTokens": 0,
        "totalTokens": 0, "costUsd": 0.0, "calls": 0, "errors": 0, "durationMs": 0,
    }


def _accumulate(target: dict, row: dict) -> None:
    for key in _TOKEN_KEYS:
        target[key] += row.get(key, 0)
    target["totalTokens"] += row.get("totalTokens", 0)
    target["costUsd"] += row.get("costUsd", 0.0)
    target["calls"] += row.get("calls", 0)
    target["errors"] += row.get("errors", 0)
    target["durationMs"] += row.get("durationMs", 0)


def _finalize(bucket: dict) -> dict:
    """Round money and derive the ratios the dashboards display."""
    calls = bucket.get("calls", 0) or 0
    total = bucket.get("totalTokens", 0) or 0
    bucket["costUsd"] = round(bucket.get("costUsd", 0.0), 6)
    bucket["cacheHitRate"] = (
        round(bucket.get("cacheReadTokens", 0) / total * 100, 1) if total else 0.0
    )
    bucket["avgLatencyMs"] = int(bucket.get("durationMs", 0) / calls) if calls else 0
    bucket["errorRate"] = round(bucket.get("errors", 0) / calls * 100, 1) if calls else 0.0
    bucket.pop("durationMs", None)
    return bucket


def _grouped(rows: list[dict], *keys: str) -> list[dict]:
    """Group rollup rows by one or more identity fields, sorted by cost."""
    buckets: dict[tuple, dict] = {}
    for row in rows:
        identity = {k: row.get(k, "unknown") for k in keys}
        bucket_key = tuple(identity.values())
        bucket = buckets.get(bucket_key)
        if bucket is None:
            bucket = _blank(**identity)
            buckets[bucket_key] = bucket
        _accumulate(bucket, row)
    return sorted(
        (_finalize(b) for b in buckets.values()),
        key=lambda x: x["costUsd"],
        reverse=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Overview
# ─────────────────────────────────────────────────────────────────────────────

def get_overview(user_id: str | None, period: str = "today", is_admin: bool = False) -> dict:
    rows = _rows(user_id, period, is_admin)

    total = _blank()
    for row in rows:
        _accumulate(total, row)
    _finalize(total)

    claude_rows = [r for r in rows if str(r.get("tool", "")).startswith("claude-code")]
    claude_cost = sum(r.get("costUsd", 0.0) for r in claude_rows)

    return {
        "period": period,
        "totalInputTokens": total["inputTokens"],
        "totalOutputTokens": total["outputTokens"],
        "totalCacheReadTokens": total["cacheReadTokens"],
        "totalCacheCreationTokens": total["cacheCreationTokens"],
        "totalTokens": total["totalTokens"],
        "totalCostUsd": total["costUsd"],
        "totalCalls": total["calls"],
        "uniqueUsers": len({r.get("userId", "") for r in rows if r.get("userId")}),
        "uniqueModels": len({r.get("model", "") for r in rows if r.get("model")}),
        "cacheHitRate": total["cacheHitRate"],
        "avgLatencyMs": total["avgLatencyMs"],
        "errorRate": total["errorRate"],
        # Claude Code slice, called out because it is the reason this exists
        "claudeCodeCalls": sum(r.get("calls", 0) for r in claude_rows),
        "claudeCodeCostUsd": round(claude_cost, 6),
        # Retained for backwards compatibility with the existing UI fields
        "gatewayCalls": total["calls"],
        "gatewayCostUsd": total["costUsd"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Breakdowns
# ─────────────────────────────────────────────────────────────────────────────

def get_by_model(user_id: str | None, period: str = "7d", is_admin: bool = False) -> list[dict]:
    return _grouped(_rows(user_id, period, is_admin), "model")


def get_by_tool(user_id: str | None, period: str = "7d", is_admin: bool = False) -> list[dict]:
    return _grouped(_rows(user_id, period, is_admin), "tool")


def get_by_tool_model(user_id: str | None, period: str = "7d", is_admin: bool = False) -> list[dict]:
    """The model x tool matrix — per-model usage attributed to the tool that spent it."""
    return _grouped(_rows(user_id, period, is_admin), "tool", "model")


def get_by_user(period: str = "7d") -> list[dict]:
    return _grouped(_rows(None, period, is_admin=True), "userId")


def get_claude_code_usage(user_id: str | None, period: str = "7d", is_admin: bool = False) -> dict:
    """Claude Code only, split by model and by surface (CLI vs VS Code extension)."""
    rows = [
        r for r in _rows(user_id, period, is_admin)
        if str(r.get("tool", "")).startswith("claude-code")
    ]

    total = _blank()
    for row in rows:
        _accumulate(total, row)
    _finalize(total)

    return {
        "period": period,
        "totals": total,
        "byModel": _grouped(rows, "model"),
        "bySurface": _grouped(rows, "tool"),
        "byModelSurface": _grouped(rows, "tool", "model"),
        "timeseries": _timeseries(rows),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Timeseries
# ─────────────────────────────────────────────────────────────────────────────

def _timeseries(rows: list[dict]) -> list[dict]:
    by_day = {b["date"]: b for b in _grouped(rows, "date")}
    return sorted(by_day.values(), key=lambda x: x["date"])


def get_timeseries(user_id: str | None, period: str = "14d", is_admin: bool = False) -> list[dict]:
    """Daily series, zero-filled so charts show gaps as zero rather than closing them."""
    rows = _rows(user_id, period, is_admin)
    present = {b["date"]: b for b in _grouped(rows, "date")}

    from_date, to_date = usage_rollup.period_bounds(period)
    if not from_date:
        return sorted(present.values(), key=lambda x: x["date"])

    start = datetime.fromisoformat(from_date).date()
    end = datetime.fromisoformat(to_date).date()
    series: list[dict] = []
    day = start
    while day <= end:
        iso = day.isoformat()
        series.append(present.get(iso) or _finalize(_blank(date=iso)))
        day += timedelta(days=1)
    return series


# ─────────────────────────────────────────────────────────────────────────────
# Audit logs — raw per-request drill-down
# ─────────────────────────────────────────────────────────────────────────────

def get_audit_logs(
    user_id: str | None,
    is_admin: bool,
    filter_user: str | None = None,
    filter_model: str | None = None,
    filter_tool: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Individual request rows. Reads raw events — rollups have collapsed these."""
    try:
        if filter_user:
            # Targeted user: a Query on their partition, not a table scan.
            items = db.query_range(
                "token-usage", pk_name="userId", pk_value=filter_user,
                sk_name="sortKey",
                sk_from=from_ts or None,
                sk_to=f"{to_ts}￿" if to_ts else None,
                limit=min(limit * 5, 5000),
            )
        elif is_admin:
            items = db.scan_items("token-usage", limit=min(limit * 10, 5000))
        else:
            items = db.query_range(
                "token-usage", pk_name="userId", pk_value=user_id or "",
                sk_name="sortKey",
                sk_from=from_ts or None,
                sk_to=f"{to_ts}￿" if to_ts else None,
                limit=min(limit * 5, 2000),
            )
    except Exception as exc:
        log.warning("get_audit_logs failed: %s", exc)
        items = []

    if from_ts:
        items = [i for i in items if i.get("timestamp", "") >= from_ts]
    if to_ts:
        items = [i for i in items if i.get("timestamp", "") <= to_ts]
    if filter_model:
        items = [i for i in items if filter_model.lower() in str(i.get("model", "")).lower()]
    if filter_tool:
        items = [i for i in items if i.get("tool", "") == filter_tool]

    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return items[:limit]

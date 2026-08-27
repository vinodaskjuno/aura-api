"""Gateway analytics — DynamoDB aggregation helpers for the AIOps Gateway tab."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from src.database import dynamo_client as db

log = logging.getLogger(__name__)


def _cutoff(period: str) -> str | None:
    now = datetime.now(timezone.utc)
    if period == "today":
        cut = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        cut = now - timedelta(days=7)
    elif period == "14d":
        cut = now - timedelta(days=14)
    elif period == "30d":
        cut = now - timedelta(days=30)
    else:
        return None
    return cut.isoformat()


def _filter(items: list[dict], period: str) -> list[dict]:
    cutoff = _cutoff(period)
    if not cutoff:
        return items
    return [i for i in items if i.get("timestamp", "") >= cutoff]


def _scan_usage(user_id: str | None = None, limit: int = 10_000) -> list[dict]:
    try:
        if user_id:
            return db.query_items("token-usage", "userId", user_id, limit=limit)
        return db.scan_items("token-usage", limit=limit)
    except Exception as exc:
        log.warning("gateway_analytics scan_usage failed: %s", exc)
        return []


# ─── Overview ────────────────────────────────────────────────────────────────

def get_overview(user_id: str | None, period: str = "today", is_admin: bool = False) -> dict:
    items = _scan_usage(None if is_admin else user_id)
    items = _filter(items, period)

    total_input = sum(int(i.get("inputTokens", 0)) for i in items)
    total_output = sum(int(i.get("outputTokens", 0)) for i in items)
    total_cost = sum(float(i.get("cost", 0)) for i in items)
    total_calls = len(items)
    unique_users = len({i.get("userId", "") for i in items})

    gateway_items = [i for i in items if i.get("source") == "gateway"]
    gateway_cost = sum(float(i.get("cost", 0)) for i in gateway_items)

    return {
        "period": period,
        "totalInputTokens": total_input,
        "totalOutputTokens": total_output,
        "totalTokens": total_input + total_output,
        "totalCostUsd": round(total_cost, 4),
        "totalCalls": total_calls,
        "uniqueUsers": unique_users,
        "gatewayCalls": len(gateway_items),
        "gatewayCostUsd": round(gateway_cost, 4),
    }


# ─── By model ────────────────────────────────────────────────────────────────

def get_by_model(user_id: str | None, period: str = "7d", is_admin: bool = False) -> list[dict]:
    items = _scan_usage(None if is_admin else user_id)
    items = _filter(items, period)

    by_model: dict[str, dict] = {}
    for i in items:
        m = i.get("model", "unknown")
        if m not in by_model:
            by_model[m] = {"model": m, "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "calls": 0}
        by_model[m]["inputTokens"] += int(i.get("inputTokens", 0))
        by_model[m]["outputTokens"] += int(i.get("outputTokens", 0))
        by_model[m]["costUsd"] += float(i.get("cost", 0))
        by_model[m]["calls"] += 1

    result = sorted(by_model.values(), key=lambda x: x["costUsd"], reverse=True)
    for r in result:
        r["costUsd"] = round(r["costUsd"], 4)
    return result


# ─── By tool ─────────────────────────────────────────────────────────────────

def get_by_tool(user_id: str | None, period: str = "7d", is_admin: bool = False) -> list[dict]:
    items = _scan_usage(None if is_admin else user_id)
    items = _filter(items, period)

    by_tool: dict[str, dict] = {}
    for i in items:
        source = i.get("source", "direct")
        raw_tool = i.get("tool", "")
        tool = raw_tool if (source == "gateway" and raw_tool and raw_tool != "unknown") else (
            "aura-plugin" if source == "direct" else "other"
        )
        if tool not in by_tool:
            by_tool[tool] = {"tool": tool, "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "calls": 0}
        by_tool[tool]["inputTokens"] += int(i.get("inputTokens", 0))
        by_tool[tool]["outputTokens"] += int(i.get("outputTokens", 0))
        by_tool[tool]["costUsd"] += float(i.get("cost", 0))
        by_tool[tool]["calls"] += 1

    result = sorted(by_tool.values(), key=lambda x: x["costUsd"], reverse=True)
    for r in result:
        r["costUsd"] = round(r["costUsd"], 4)
    return result


# ─── By user (admin) ─────────────────────────────────────────────────────────

def get_by_user(period: str = "7d") -> list[dict]:
    items = _scan_usage(None)
    items = _filter(items, period)

    by_user: dict[str, dict] = {}
    for i in items:
        uid = i.get("userId", "unknown")
        if uid not in by_user:
            by_user[uid] = {"userId": uid, "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "calls": 0}
        by_user[uid]["inputTokens"] += int(i.get("inputTokens", 0))
        by_user[uid]["outputTokens"] += int(i.get("outputTokens", 0))
        by_user[uid]["costUsd"] += float(i.get("cost", 0))
        by_user[uid]["calls"] += 1

    result = sorted(by_user.values(), key=lambda x: x["costUsd"], reverse=True)
    for r in result:
        r["costUsd"] = round(r["costUsd"], 4)
    return result


# ─── Timeseries ──────────────────────────────────────────────────────────────

def get_timeseries(user_id: str | None, period: str = "14d", is_admin: bool = False) -> list[dict]:
    items = _scan_usage(None if is_admin else user_id)
    items = _filter(items, period)

    by_day: dict[str, dict] = {}
    for i in items:
        day = (i.get("timestamp") or "")[:10]
        if not day:
            continue
        if day not in by_day:
            by_day[day] = {"date": day, "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "calls": 0}
        by_day[day]["inputTokens"] += int(i.get("inputTokens", 0))
        by_day[day]["outputTokens"] += int(i.get("outputTokens", 0))
        by_day[day]["costUsd"] += float(i.get("cost", 0))
        by_day[day]["calls"] += 1

    result = sorted(by_day.values(), key=lambda x: x["date"])
    for r in result:
        r["costUsd"] = round(r["costUsd"], 4)
    return result


# ─── Audit logs ──────────────────────────────────────────────────────────────

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
    try:
        if is_admin or filter_user:
            items = db.scan_items("token-usage", limit=min(limit * 10, 5000))
        else:
            items = db.query_items("token-usage", "userId", user_id or "", limit=min(limit * 5, 2000))
    except Exception:
        items = []

    if from_ts:
        items = [i for i in items if i.get("timestamp", "") >= from_ts]
    if to_ts:
        items = [i for i in items if i.get("timestamp", "") <= to_ts]
    if filter_user:
        items = [i for i in items if i.get("userId", "") == filter_user]
    if filter_model:
        items = [i for i in items if filter_model.lower() in i.get("model", "").lower()]
    if filter_tool:
        items = [i for i in items if i.get("tool", "") == filter_tool]

    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return items[:limit]

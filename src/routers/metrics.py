"""Metrics router — token usage and cost tracking per user."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.database import dynamo_client as db
from src.config_settings import MODEL_PRICING, calculate_cost

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/metrics", tags=["metrics"])


class TokenUsageRecord(BaseModel):
    sessionId: str
    projectId: str = ""
    model: str
    inputTokens: int
    outputTokens: int


@router.post("/tokens", status_code=201)
def record_token_usage(body: TokenUsageRecord, user: dict = Depends(get_current_user)):
    """Record token usage for a single LLM invocation."""
    user_id = user.get("userId", "unknown")
    now = datetime.now(timezone.utc).isoformat()
    message_id = str(uuid.uuid4())[:8]
    cost = calculate_cost(body.model, body.inputTokens, body.outputTokens)
    item = {
        "userId": user_id,
        "sortKey": f"{now}#{message_id}",
        "sessionId": body.sessionId,
        "projectId": body.projectId,
        "model": body.model,
        "inputTokens": body.inputTokens,
        "outputTokens": body.outputTokens,
        "cost": str(cost),
        "timestamp": now,
    }
    try:
        db.put_item("token-usage", item)
    except Exception as e:
        log.warning("Failed to record token usage: %s", e)
    return {"recorded": True, "cost": cost}


@router.get("/tokens")
def get_token_metrics(
    period: str = Query("today", description="today | week | month | all"),
    user: dict = Depends(get_current_user),
):
    """Get aggregated token usage and cost for the current user."""
    user_id = user.get("userId", "unknown")
    now = datetime.now(timezone.utc)

    # Determine cutoff timestamp
    if period == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        cutoff = now - timedelta(days=7)
    elif period == "month":
        cutoff = now - timedelta(days=30)
    else:
        cutoff = None

    try:
        items = db.query_items("token-usage", "userId", user_id, limit=1000)
    except Exception:
        items = []

    # Filter by period
    if cutoff:
        cutoff_iso = cutoff.isoformat()
        items = [i for i in items if i.get("timestamp", "") >= cutoff_iso]

    total_input = sum(int(i.get("inputTokens", 0)) for i in items)
    total_output = sum(int(i.get("outputTokens", 0)) for i in items)
    total_cost = sum(float(i.get("cost", 0)) for i in items)
    total_sessions = len(set(i.get("sessionId", "") for i in items))

    # By model breakdown
    by_model: dict = {}
    for i in items:
        m = i.get("model", "unknown")
        if m not in by_model:
            by_model[m] = {"inputTokens": 0, "outputTokens": 0, "cost": 0.0, "calls": 0}
        by_model[m]["inputTokens"] += int(i.get("inputTokens", 0))
        by_model[m]["outputTokens"] += int(i.get("outputTokens", 0))
        by_model[m]["cost"] += float(i.get("cost", 0))
        by_model[m]["calls"] += 1

    return {
        "period": period,
        "totalInputTokens": total_input,
        "totalOutputTokens": total_output,
        "totalCost": round(total_cost, 4),
        "totalSessions": total_sessions,
        "totalCalls": len(items),
        "byModel": [{"model": k, **v, "cost": round(v["cost"], 4)} for k, v in by_model.items()],
    }


def _period_cutoff(period: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    return None


def _filter_by_period(items: list[dict], period: str) -> list[dict]:
    cutoff = _period_cutoff(period)
    if not cutoff:
        return items
    cutoff_iso = cutoff.isoformat()
    return [i for i in items if i.get("timestamp", "") >= cutoff_iso]


@router.get("/tokens/projects")
def get_project_metrics(
    period: str = Query("today"),
    user: dict = Depends(get_current_user),
):
    """Aggregated token usage grouped by project for the calling user."""
    user_id = user.get("userId", "unknown")
    is_admin = user.get("role", "") in ("admin", "super_admin")

    try:
        if is_admin:
            items = db.scan_items("token-usage", limit=5000)
        else:
            items = db.query_items("token-usage", "userId", user_id, limit=2000)
    except Exception:
        items = []

    items = _filter_by_period(items, period)

    by_project: dict = {}
    for i in items:
        pid = i.get("projectId") or "__no_project__"
        if pid not in by_project:
            by_project[pid] = {
                "projectId": pid,
                "projectName": pid if pid != "__no_project__" else "(no project)",
                "inputTokens": 0, "outputTokens": 0, "cost": 0.0, "sessions": set(),
            }
        by_project[pid]["inputTokens"] += int(i.get("inputTokens", 0))
        by_project[pid]["outputTokens"] += int(i.get("outputTokens", 0))
        by_project[pid]["cost"] += float(i.get("cost", 0))
        by_project[pid]["sessions"].add(i.get("sessionId", ""))

    result = []
    for row in sorted(by_project.values(), key=lambda x: x["cost"], reverse=True):
        result.append({
            "projectId": row["projectId"],
            "projectName": row["projectName"],
            "inputTokens": row["inputTokens"],
            "outputTokens": row["outputTokens"],
            "cost": round(row["cost"], 4),
            "sessions": len(row["sessions"]),
        })
    return result


@router.get("/tokens/tool-breakdown")
def get_tool_breakdown(
    period: str = Query("today", description="today | week | month | all"),
    user: dict = Depends(get_current_user),
):
    """Aggregated token usage and cost grouped by tool source for the calling user.

    Returns usage from all AI tools that route through the AURA gateway:
    aura-plugin, claude-ext, claude-cli, codex-cli, gemini-cli, copilot, etc.
    Entries with tool='unknown' are shown as 'other'.
    """
    user_id = user.get("userId", "unknown")

    try:
        items = db.query_items("token-usage", "userId", user_id, limit=5000)
    except Exception:
        items = []

    items = _filter_by_period(items, period)

    # Only include records that originated from the gateway (have a 'tool' attribute)
    # Records from the old /api/metrics/tokens path have no 'tool' field — treat as 'aura-plugin'
    by_tool: dict = {}
    for i in items:
        source = i.get("source", "direct")
        raw_tool = i.get("tool", "")
        if source == "gateway":
            tool = raw_tool if raw_tool and raw_tool != "unknown" else "other"
        else:
            # Direct API call recorded via /api/metrics/tokens — counts as aura-plugin
            tool = "aura-plugin"

        if tool not in by_tool:
            by_tool[tool] = {"tool": tool, "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0, "calls": 0}
        by_tool[tool]["inputTokens"] += int(i.get("inputTokens", 0))
        by_tool[tool]["outputTokens"] += int(i.get("outputTokens", 0))
        by_tool[tool]["costUsd"] += float(i.get("cost", 0))
        by_tool[tool]["calls"] += 1

    result = sorted(by_tool.values(), key=lambda x: x["costUsd"], reverse=True)
    for row in result:
        row["costUsd"] = round(row["costUsd"], 4)
    return {"period": period, "breakdown": result}


@router.get("/tokens/users")
def get_user_metrics(
    period: str = Query("today"),
    user: dict = Depends(get_current_user),
):
    """Aggregated token usage grouped by user (admin only)."""
    if user.get("role", "") not in ("admin", "super_admin"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        items = db.scan_items("token-usage", limit=5000)
    except Exception:
        items = []

    items = _filter_by_period(items, period)

    by_user: dict = {}
    for i in items:
        uid = i.get("userId", "unknown")
        if uid not in by_user:
            by_user[uid] = {
                "userId": uid,
                "username": uid,
                "inputTokens": 0, "outputTokens": 0, "cost": 0.0, "sessions": set(),
            }
        by_user[uid]["inputTokens"] += int(i.get("inputTokens", 0))
        by_user[uid]["outputTokens"] += int(i.get("outputTokens", 0))
        by_user[uid]["cost"] += float(i.get("cost", 0))
        by_user[uid]["sessions"].add(i.get("sessionId", ""))

    result = []
    for row in sorted(by_user.values(), key=lambda x: x["cost"], reverse=True):
        result.append({
            "userId": row["userId"],
            "username": row["username"],
            "inputTokens": row["inputTokens"],
            "outputTokens": row["outputTokens"],
            "cost": round(row["cost"], 4),
            "sessions": len(row["sessions"]),
        })
    return result

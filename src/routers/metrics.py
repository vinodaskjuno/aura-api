"""Metrics router — token usage and cost tracking per user."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.database import dynamo_client as db
from src.config_settings import (
    MODEL_PRICING,
    calculate_cost_v2,
    normalize_model_id,
)
from src.services import usage_rollup

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/metrics", tags=["metrics"])


class TokenUsageRecord(BaseModel):
    sessionId: str
    projectId: str = ""
    model: str
    inputTokens: int
    outputTokens: int
    # Cache tokens — usually the bulk of an agentic session's volume, and priced
    # very differently (cache read 0.1x input, cache write 1.25x-2x).
    cacheReadTokens: int = 0
    cacheCreationTokens: int = 0
    # Provenance. These were previously absent from the model, so Pydantic
    # silently DROPPED them: copilotTracker has always posted
    # source="copilot-estimate", it never persisted, and the tool breakdown
    # therefore mis-attributed every Copilot request to "aura-plugin".
    source: str = "direct"
    tool: str = ""
    # Set when the caller's provider reported its own cost; preferred over our
    # computed estimate.
    reportedCostUsd: float | None = None


def _usage_item(user_id: str, body: TokenUsageRecord) -> tuple[dict, float]:
    """Build a token-usage row plus its effective cost."""
    now = datetime.now(timezone.utc).isoformat()
    message_id = str(uuid.uuid4())[:8]
    model = normalize_model_id(body.model)
    computed = calculate_cost_v2(
        model, body.inputTokens, body.outputTokens,
        body.cacheReadTokens, body.cacheCreationTokens,
    )
    cost = body.reportedCostUsd if body.reportedCostUsd is not None else computed

    item = {
        "userId": user_id,
        "sortKey": f"{now}#{message_id}",
        "sessionId": body.sessionId,
        "projectId": body.projectId,
        "model": model,
        "inputTokens": body.inputTokens,
        "outputTokens": body.outputTokens,
        "cacheReadTokens": body.cacheReadTokens,
        "cacheCreationTokens": body.cacheCreationTokens,
        "cost": str(round(cost, 6)),
        "computedCost": str(computed),
        "timestamp": now,
        "source": body.source,
        "tool": body.tool or _tool_from_source(body.source),
    }
    return item, cost


def _tool_from_source(source: str) -> str:
    """Map a reported source to a tool label when the client did not send one."""
    src = (source or "").lower()
    if "copilot" in src:
        return "copilot"
    if "claude" in src:
        return "claude-code"
    if "codex" in src:
        return "codex-cli"
    if "gemini" in src:
        return "gemini-cli"
    return "aura-plugin"


@router.post("/tokens", status_code=201)
def record_token_usage(body: TokenUsageRecord, user: dict = Depends(get_current_user)):
    """Record token usage for a single LLM invocation."""
    user_id = user.get("userId", "unknown")
    item, cost = _usage_item(user_id, body)
    try:
        db.put_item("token-usage", item)
        usage_rollup.bump(
            user_id, item["timestamp"][:10], item["tool"], item["model"],
            {
                "inputTokens": body.inputTokens,
                "outputTokens": body.outputTokens,
                "cacheReadTokens": body.cacheReadTokens,
                "cacheCreationTokens": body.cacheCreationTokens,
                "costUsd": round(cost, 6),
                "calls": 1,
            },
        )
    except Exception as e:
        log.warning("Failed to record token usage: %s", e)
    return {"recorded": True, "cost": cost}


@router.post("/tokens/batch", status_code=201)
def record_token_usage_batch(
    body: list[TokenUsageRecord],
    user: dict = Depends(get_current_user),
):
    """Record several invocations in one request.

    Clients that aggregate locally before flushing (the plugin's Copilot tracker
    batches per model on a 60s timer) previously had to issue one HTTP request
    per model. Telemetry never fails the caller: partial success is reported in
    the body rather than raised.
    """
    user_id = user.get("userId", "unknown")
    recorded = 0
    total_cost = 0.0
    failed = 0

    for record in body[:500]:
        try:
            item, cost = _usage_item(user_id, record)
            db.put_item("token-usage", item)
            usage_rollup.bump(
                user_id, item["timestamp"][:10], item["tool"], item["model"],
                {
                    "inputTokens": record.inputTokens,
                    "outputTokens": record.outputTokens,
                    "cacheReadTokens": record.cacheReadTokens,
                    "cacheCreationTokens": record.cacheCreationTokens,
                    "costUsd": round(cost, 6),
                    "calls": 1,
                },
            )
            recorded += 1
            total_cost += cost
        except Exception as e:
            failed += 1
            log.warning("Batch token record failed: %s", e)

    return {
        "recorded": recorded,
        "failed": failed,
        "submitted": len(body),
        "cost": round(total_cost, 6),
    }


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

    def _int(row: dict, key: str) -> int:
        try:
            return int(row.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    total_input = sum(_int(i, "inputTokens") for i in items)
    total_output = sum(_int(i, "outputTokens") for i in items)
    total_cache_read = sum(_int(i, "cacheReadTokens") for i in items)
    total_cache_create = sum(_int(i, "cacheCreationTokens") for i in items)
    total_cost = sum(float(i.get("cost", 0) or 0) for i in items)
    total_sessions = len(set(i.get("sessionId", "") for i in items))

    # By model breakdown. Models are canonicalized so that a Bedrock ID, a dated
    # snapshot and the bare alias collapse into one row instead of three.
    by_model: dict = {}
    for i in items:
        m = normalize_model_id(str(i.get("model", "unknown")))
        bucket = by_model.setdefault(m, {
            "inputTokens": 0, "outputTokens": 0,
            "cacheReadTokens": 0, "cacheCreationTokens": 0,
            "cost": 0.0, "calls": 0,
        })
        bucket["inputTokens"] += _int(i, "inputTokens")
        bucket["outputTokens"] += _int(i, "outputTokens")
        bucket["cacheReadTokens"] += _int(i, "cacheReadTokens")
        bucket["cacheCreationTokens"] += _int(i, "cacheCreationTokens")
        bucket["cost"] += float(i.get("cost", 0) or 0)
        bucket["calls"] += 1

    total_tokens = total_input + total_output + total_cache_read + total_cache_create
    return {
        "period": period,
        "totalInputTokens": total_input,
        "totalOutputTokens": total_output,
        "totalCacheReadTokens": total_cache_read,
        "totalCacheCreationTokens": total_cache_create,
        "totalTokens": total_tokens,
        "totalCost": round(total_cost, 6),
        "totalSessions": total_sessions,
        "totalCalls": len(items),
        "cacheHitRate": (
            round(total_cache_read / total_tokens * 100, 1) if total_tokens else 0.0
        ),
        "byModel": sorted(
            [{"model": k, **v, "cost": round(v["cost"], 6)} for k, v in by_model.items()],
            key=lambda r: r["cost"], reverse=True,
        ),
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
    from src.services import gateway_analytics
    user_id = user.get("userId", "unknown")
    breakdown = gateway_analytics.get_by_tool(user_id, period, is_admin=False)
    return {"period": period, "breakdown": breakdown}


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

"""AIOps AI Gateway sub-router — usage analytics, providers, budgets, keys, health, audit logs.

All endpoints live under /api/aiops/gateway (registered in main.py).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.database import dynamo_client as db
from src.services import gateway_analytics
from src.services.provider_health import get_latest_health, run_health_probes

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/aiops/gateway", tags=["aiops-gateway"])


def _admin_only(user: dict) -> None:
    if user.get("role", "") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")


# ─────────────────────────────────────────────────────────────────────────────
# Usage — Overview
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/usage/overview")
def usage_overview(
    period: str = Query("today", description="today | 7d | 14d | 30d | all"),
    user: dict = Depends(get_current_user),
):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    return gateway_analytics.get_overview(user.get("userId"), period, is_admin)


# ─────────────────────────────────────────────────────────────────────────────
# Usage — By model
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/usage/by-model")
def usage_by_model(
    period: str = Query("7d"),
    user: dict = Depends(get_current_user),
):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    return {"period": period, "byModel": gateway_analytics.get_by_model(user.get("userId"), period, is_admin)}


# ─────────────────────────────────────────────────────────────────────────────
# Usage — By tool
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/usage/by-tool")
def usage_by_tool(
    period: str = Query("7d"),
    user: dict = Depends(get_current_user),
):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    return {"period": period, "byTool": gateway_analytics.get_by_tool(user.get("userId"), period, is_admin)}


# ─────────────────────────────────────────────────────────────────────────────
# Usage — By user (admin only)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/usage/by-user")
def usage_by_user(
    period: str = Query("7d"),
    user: dict = Depends(get_current_user),
):
    _admin_only(user)
    return {"period": period, "byUser": gateway_analytics.get_by_user(period)}


# ─────────────────────────────────────────────────────────────────────────────
# Usage — Timeseries
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/usage/timeseries")
def usage_timeseries(
    period: str = Query("14d"),
    user: dict = Depends(get_current_user),
):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    return {"period": period, "timeseries": gateway_analytics.get_timeseries(user.get("userId"), period, is_admin)}


# ─────────────────────────────────────────────────────────────────────────────
# Providers
# ─────────────────────────────────────────────────────────────────────────────

class ProviderRequest(BaseModel):
    name: str
    protocol: str = "openai"  # "anthropic" | "openai"
    baseUrl: str
    apiKey: str = ""
    enabled: bool = True
    description: str = ""


@router.get("/providers")
def list_providers(user: dict = Depends(get_current_user)):
    try:
        items = db.scan_items("gateway-providers", limit=100)
    except Exception:
        items = []
    # Mask API keys
    for item in items:
        if item.get("apiKey"):
            k = item["apiKey"]
            item["apiKey"] = k[:4] + "…" + k[-4:] if len(k) > 8 else "****"
    return items


@router.post("/providers", status_code=201)
def add_provider(body: ProviderRequest, user: dict = Depends(get_current_user)):
    _admin_only(user)
    provider_id = f"provider-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "providerId": provider_id,
        "name": body.name,
        "protocol": body.protocol,
        "baseUrl": body.baseUrl,
        "apiKey": body.apiKey,
        "enabled": body.enabled,
        "description": body.description,
        "createdAt": now,
        "createdBy": user.get("userId", "unknown"),
    }
    db.put_item("gateway-providers", item)
    item["apiKey"] = body.apiKey[:4] + "…" if len(body.apiKey) > 4 else "****"
    return item


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, user: dict = Depends(get_current_user)):
    _admin_only(user)
    try:
        db.delete_item("gateway-providers", {"providerId": provider_id})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Budgets
# ─────────────────────────────────────────────────────────────────────────────

class BudgetRequest(BaseModel):
    entityType: str = "user"   # "user" | "team" | "model"
    entityId: str              # userId, teamId, or model name
    limitUsd: float
    period: str = "monthly"    # "daily" | "weekly" | "monthly"
    action: str = "alert"      # "alert" | "downgrade" | "block"
    enabled: bool = True


@router.get("/budgets")
def list_budgets(user: dict = Depends(get_current_user)):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    try:
        if is_admin:
            items = db.scan_items("gateway-budgets", limit=500)
        else:
            items = db.query_items("gateway-budgets", "entityId", user.get("userId", ""), limit=50)
    except Exception:
        items = []
    return items


@router.post("/budgets", status_code=201)
def upsert_budget(body: BudgetRequest, user: dict = Depends(get_current_user)):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    if not is_admin and body.entityId != user.get("userId"):
        raise HTTPException(status_code=403, detail="Can only set budgets for yourself")
    pk = f"{body.entityType}#{body.entityId}"
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "pk": pk,
        "entityType": body.entityType,
        "entityId": body.entityId,
        "limitUsd": str(body.limitUsd),
        "period": body.period,
        "action": body.action,
        "enabled": body.enabled,
        "updatedAt": now,
        "updatedBy": user.get("userId", "unknown"),
    }
    db.put_item("gateway-budgets", item)
    return item


@router.delete("/budgets/{entity_type}/{entity_id}", status_code=204)
def delete_budget(entity_type: str, entity_id: str, user: dict = Depends(get_current_user)):
    _admin_only(user)
    pk = f"{entity_type}#{entity_id}"
    try:
        db.delete_item("gateway-budgets", {"pk": pk})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/budgets/status")
def budget_status(user: dict = Depends(get_current_user)):
    """Current spend vs limit for the calling user."""
    from src.routers.budget import _get_config, _get_user_budget
    user_id = user.get("userId", "unknown")
    config = _get_config()
    budget = _get_user_budget(user_id)
    tier = budget.get("currentTier", 1)
    spent_key = f"tier{tier}SpendUSD"
    limit_key = f"tier{tier}LimitUSD"
    spent_usd = round(budget.get(spent_key, 0.0), 4)
    limit_usd = float(config.get(limit_key, 500))
    pct = round((spent_usd / limit_usd) * 100, 1) if limit_usd > 0 else 0
    return {"tier": f"Tier {tier}", "spentUsd": spent_usd, "limitUsd": limit_usd, "pct": pct}


# ─────────────────────────────────────────────────────────────────────────────
# Virtual Keys (admin view of all users' keys)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/keys")
def list_keys(user: dict = Depends(get_current_user)):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    try:
        if is_admin:
            items = db.scan_items("gateway-api-keys", limit=1000)
        else:
            items = db.query_items("gateway-api-keys", "userId", user.get("userId", ""), index_name="userId-index", limit=50)
    except Exception:
        items = []
    # Strip key hash — show only hint
    for item in items:
        item.pop("keyHash", None)
    return items


@router.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: str, user: dict = Depends(get_current_user)):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    try:
        existing = db.get_item("gateway-api-keys", {"keyId": key_id})
    except Exception:
        existing = None
    if not existing:
        raise HTTPException(status_code=404, detail="Key not found")
    if not is_admin and existing.get("userId") != user.get("userId"):
        raise HTTPException(status_code=403, detail="Cannot revoke another user's key")
    db.update_item("gateway-api-keys", {"keyId": key_id}, {"active": False})


# ─────────────────────────────────────────────────────────────────────────────
# Provider Health
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health")
def provider_health(user: dict = Depends(get_current_user)):
    return {"providers": get_latest_health()}


@router.post("/health/probe", status_code=202)
async def trigger_health_probe(user: dict = Depends(get_current_user)):
    _admin_only(user)
    import asyncio
    asyncio.create_task(run_health_probes())
    return {"triggered": True}


# ─────────────────────────────────────────────────────────────────────────────
# Audit Logs
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/audit-logs")
def audit_logs(
    filter_user:  str | None = Query(None),
    filter_model: str | None = Query(None),
    filter_tool:  str | None = Query(None),
    from_ts:      str | None = Query(None),
    to_ts:        str | None = Query(None),
    limit:        int        = Query(100, ge=1, le=1000),
    user: dict = Depends(get_current_user),
):
    is_admin = user.get("role", "") in ("admin", "super_admin")
    logs = gateway_analytics.get_audit_logs(
        user_id=user.get("userId"),
        is_admin=is_admin,
        filter_user=filter_user,
        filter_model=filter_model,
        filter_tool=filter_tool,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
    )
    return {"total": len(logs), "logs": logs}

"""Credits router — VSIX token usage recording and credit balance."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.database import dynamo_client as db
from src.config_settings import MODEL_PRICING, calculate_cost
from src.routers.budget import update_user_budget, _get_config, _get_user_budget
from src.routers.metrics import _filter_by_period

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/credits", tags=["credits"])

# 1 credit = $0.001 USD  (matches VSIX CREDIT_VALUE_USD constant)
_CREDIT_VALUE_USD = 0.001


class RecordBody(BaseModel):
    taskId: str
    agentGroupId: str = ""
    modelId: str
    inputTokens: int
    outputTokens: int
    costUsd: float


@router.post("/record", status_code=201)
def record_task_cost(body: RecordBody, user: dict = Depends(get_current_user)):
    """Record per-task token usage from the VSIX into DynamoDB."""
    user_id = user.get("userId", "unknown")
    now = datetime.now(timezone.utc).isoformat()
    record_id = str(uuid.uuid4())[:8]

    # Recalculate cost server-side; fall back to client-supplied value for unknown models
    cost = calculate_cost(body.modelId, body.inputTokens, body.outputTokens)
    if cost == 0.0 and body.costUsd > 0:
        cost = round(body.costUsd, 6)

    item = {
        "userId": user_id,
        "sortKey": f"{now}#{record_id}",
        "taskId": body.taskId,
        "agentGroupId": body.agentGroupId,
        "model": body.modelId,
        "inputTokens": body.inputTokens,
        "outputTokens": body.outputTokens,
        "cost": str(cost),
        "timestamp": now,
        "source": "vsix",
    }
    try:
        db.put_item("token-usage", item)
    except Exception as exc:
        log.warning("credits/record: DynamoDB write failed: %s", exc)

    try:
        update_user_budget(user_id, body.modelId, cost)
    except Exception as exc:
        log.warning("credits/record: update_user_budget failed: %s", exc)

    credits_consumed = max(1, int(round(cost / _CREDIT_VALUE_USD)))
    return {"recorded": True, "cost": cost, "creditsConsumed": credits_consumed}


@router.get("/balance")
def get_balance(user: dict = Depends(get_current_user)):
    """Return the calling user's credit balance derived from their tier spend."""
    user_id = user.get("userId", "unknown")
    budget = _get_user_budget(user_id)
    config = _get_config()

    tier = int(budget.get("currentTier", 1))
    limit_usd = sum(
        float(config.get(f"tier{t}LimitUSD", 500.0)) for t in range(1, 4)
    )
    tier_spend_usd = sum(
        float(budget.get(f"tier{t}SpendUSD", 0.0)) for t in range(1, 4)
    )

    limit_credits = int(limit_usd / _CREDIT_VALUE_USD)
    used_credits = int(tier_spend_usd / _CREDIT_VALUE_USD)
    available_credits = max(0, limit_credits - used_credits)

    period_start = budget.get("periodStart", datetime.now(timezone.utc).strftime("%Y-%m"))
    year, month = (int(p) for p in period_start.split("-"))
    if month == 12:
        reset_year, reset_month = year + 1, 1
    else:
        reset_year, reset_month = year, month + 1
    reset_at = f"{reset_year}-{reset_month:02d}-01T00:00:00Z"

    return {
        "available": available_credits,
        "used": used_credits,
        "limit": limit_credits,
        "resetAt": reset_at,
        "currency": "credits",
        "tier": tier,
        "spendUsd": round(tier_spend_usd, 4),
        "limitUsd": round(limit_usd, 4),
    }


@router.get("/usage")
def get_usage(
    period: str = Query("today", description="today | week | month | all"),
    user: dict = Depends(get_current_user),
):
    """Return token usage aggregated by period for the calling user."""
    user_id = user.get("userId", "unknown")
    try:
        items = db.query_items("token-usage", "userId", user_id, limit=1000)
    except Exception:
        items = []

    items = _filter_by_period(items, period)
    total_input = sum(int(i.get("inputTokens", 0)) for i in items)
    total_output = sum(int(i.get("outputTokens", 0)) for i in items)
    total_cost = sum(float(i.get("cost", 0)) for i in items)
    total_credits = max(1, int(round(total_cost / _CREDIT_VALUE_USD))) if total_cost > 0 else 0

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
        "totalCredits": total_credits,
        "totalCalls": len(items),
        "byModel": [
            {"model": k, **v, "cost": round(v["cost"], 4)}
            for k, v in by_model.items()
        ],
    }


@router.get("/rates")
def get_rates():
    """Return per-model pricing rates in VSIX CreditRate format (no auth required)."""
    rates = []
    for model_id, pricing in MODEL_PRICING.items():
        input_usd_per_1k = round(pricing["input"] / 1000, 8)
        output_usd_per_1k = round(pricing["output"] / 1000, 8)
        rates.append({
            "modelId": model_id,
            "inputUsdPer1k": input_usd_per_1k,
            "outputUsdPer1k": output_usd_per_1k,
            "creditsPerInputToken": input_usd_per_1k,
            "creditsPerOutputToken": output_usd_per_1k,
        })
    return {"rates": rates}

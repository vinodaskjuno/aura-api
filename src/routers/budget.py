"""Budget & Tier router — org-level spending limits and tier enforcement."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.database import dynamo_client as db
from src.config_settings import TIER_MODELS, TIER_DEFAULT_MODELS, get_model_tier

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/budget", tags=["budget"])

_ORG_ID = "global"

# ── Default tier config ───────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "orgId": _ORG_ID,
    "tier1LimitUSD": "500.0",
    "tier2LimitUSD": "200.0",
    "tier3LimitUSD": "9999.0",
    "alertThreshold": "0.8",
    "tier1Models": list(TIER_MODELS.get(1, [])),
    "tier2Models": list(TIER_MODELS.get(2, [])),
    "tier3Models": list(TIER_MODELS.get(3, [])),
}


class BudgetConfigUpdate(BaseModel):
    tier1LimitUSD: float | None = None
    tier2LimitUSD: float | None = None
    tier3LimitUSD: float | None = None
    alertThreshold: float | None = None
    tier1Models: list[str] | None = None
    tier2Models: list[str] | None = None
    tier3Models: list[str] | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_config() -> dict:
    record = db.get_item("budget-config", {"orgId": _ORG_ID})
    if not record:
        return dict(DEFAULT_CONFIG)
    return {**DEFAULT_CONFIG, **record}


def _get_user_budget(user_id: str) -> dict:
    record = db.get_item("user-budgets", {"userId": user_id})
    if not record:
        now_month = datetime.now(timezone.utc).strftime("%Y-%m")
        return {
            "userId": user_id,
            "currentTier": 1,
            "tier1SpendUSD": 0.0,
            "tier2SpendUSD": 0.0,
            "tier3SpendUSD": 0.0,
            "periodStart": now_month,
            "lastAlertTier": 0,
        }
    # Convert Decimal fields to float for arithmetic
    for f in ("tier1SpendUSD", "tier2SpendUSD", "tier3SpendUSD"):
        record[f] = float(record.get(f, 0))
    record["currentTier"] = int(record.get("currentTier", 1))
    record["lastAlertTier"] = int(record.get("lastAlertTier", 0))
    return record


def _tier_for_model(model_id: str, config: dict) -> int:
    """Return the tier number for a model based on the org config."""
    tier_lists = {
        1: config.get("tier1Models", TIER_MODELS.get(1, [])),
        2: config.get("tier2Models", TIER_MODELS.get(2, [])),
        3: config.get("tier3Models", TIER_MODELS.get(3, [])),
    }
    for tier, models in tier_lists.items():
        if model_id in models:
            return tier
    return get_model_tier(model_id)  # fallback to static config


def update_user_budget(user_id: str, model_id: str, cost: float) -> None:
    """Atomically increment spend for the tier that owns model_id."""
    config = _get_config()
    tier = _tier_for_model(model_id, config)
    field = f"tier{tier}SpendUSD"
    try:
        # Ensure record exists first
        budget = _get_user_budget(user_id)
        now_month = datetime.now(timezone.utc).strftime("%Y-%m")
        if budget.get("periodStart", "") != now_month:
            # New billing period — reset all spend
            db.put_item("user-budgets", {
                "userId": user_id,
                "currentTier": 1,
                "tier1SpendUSD": Decimal("0"),
                "tier2SpendUSD": Decimal("0"),
                "tier3SpendUSD": Decimal("0"),
                "periodStart": now_month,
                "lastAlertTier": 0,
            })
        else:
            if not db.get_item("user-budgets", {"userId": user_id}):
                db.put_item("user-budgets", {
                    "userId": user_id,
                    "currentTier": 1,
                    "tier1SpendUSD": Decimal("0"),
                    "tier2SpendUSD": Decimal("0"),
                    "tier3SpendUSD": Decimal("0"),
                    "periodStart": now_month,
                    "lastAlertTier": 0,
                })
        db.atomic_add("user-budgets", {"userId": user_id}, field, Decimal(str(round(cost, 6))))
        _maybe_alert(user_id, tier, config)
    except Exception as exc:
        log.warning("update_user_budget failed for %s: %s", user_id, exc)


def _maybe_alert(user_id: str, tier: int, config: dict) -> None:
    """Fire the budget-threshold notification.

    `alertThreshold` and `lastAlertTier` have both existed since this router was
    written — the threshold was computed and the tier was read and written, but
    nothing ever dispatched anything. This is the missing half.
    """
    try:
        budget = _get_user_budget(user_id)
        limit = float(config.get(f"tier{tier}LimitUSD", 0) or 0)
        if limit <= 0:
            return
        spend = float(budget.get(f"tier{tier}SpendUSD", 0) or 0)
        ratio = spend / limit
        threshold = float(config.get("alertThreshold", 0.8) or 0.8)
        if ratio < threshold or int(budget.get("lastAlertTier", 0)) == tier:
            return

        from src.services.notifications import dispatcher
        from src.services.notifications.base import Notification
        dispatcher.send_sync(Notification(
            kind="budget_threshold",
            severity="high" if ratio >= 1.0 else "medium",
            title=f"LLM budget at {ratio:.0%} for tier {tier}",
            body=f"User {user_id} has spent ${spend:.2f} of the ${limit:.2f} "
                 f"tier-{tier} limit this period.",
            fields=[{"label": "Tier", "value": str(tier)},
                    {"label": "Spend", "value": f"${spend:.2f}"},
                    {"label": "Limit", "value": f"${limit:.2f}"}],
            dedupe_key=f"budget:{user_id}:{tier}:{budget.get('periodStart','')}",
        ), user_id=user_id)
        db.update_item("user-budgets", {"userId": user_id}, {"lastAlertTier": tier})
        log.info("Budget alert dispatched for %s at tier %d (%.0f%%)", user_id, tier, ratio * 100)
    except Exception as exc:  # noqa: BLE001
        log.debug("budget alert skipped: %s", exc)


async def check_and_enforce_budget(
    user_id: str, requested_model: str, settings
) -> tuple[str, list[dict]]:
    """
    Check the user's budget and possibly switch the model to a cheaper tier.
    Returns (resolved_model_id, list_of_ws_events_to_emit).
    """
    events: list[dict] = []
    if not user_id or not requested_model:
        return requested_model, events

    try:
        config = _get_config()
        budget = _get_user_budget(user_id)
        tier = _tier_for_model(requested_model, config)

        limit_key = f"tier{tier}LimitUSD"
        spend_key = f"tier{tier}SpendUSD"
        limit = float(config.get(limit_key, 9999))
        spend = budget.get(spend_key, 0.0)
        alert_threshold = float(config.get("alertThreshold", 0.8))

        if limit <= 0:
            return requested_model, events

        pct = spend / limit if limit > 0 else 0.0

        if pct >= 1.0:
            # Tier exhausted — switch to next tier
            next_tier = min(tier + 1, 3)
            next_model = TIER_DEFAULT_MODELS.get(tier, requested_model)
            events.append({
                "type": "tier_switch",
                "fromTier": tier,
                "toTier": next_tier,
                "reason": f"Tier {tier} budget exhausted (${spend:.2f}/${limit:.2f})",
                "newModel": next_model,
            })
            # Update user's current tier
            try:
                db.update_item("user-budgets", {"userId": user_id}, {"currentTier": next_tier})
            except Exception:
                pass
            return next_model, events

        if pct >= alert_threshold and budget.get("lastAlertTier", 0) != tier:
            events.append({
                "type": "budget_warning",
                "tier": tier,
                "spentUSD": round(spend, 2),
                "limitUSD": round(limit, 2),
                "pct": round(pct * 100),
            })
            try:
                db.update_item("user-budgets", {"userId": user_id}, {"lastAlertTier": tier})
            except Exception:
                pass

    except Exception as exc:
        log.warning("check_and_enforce_budget failed: %s", exc)

    return requested_model, events


# ── REST endpoints ────────────────────────────────────────────────────────────

@router.get("/config")
def get_budget_config(user: dict = Depends(get_current_user)):
    """Return org-level tier configuration."""
    return _get_config()


@router.put("/config")
def update_budget_config(body: BudgetConfigUpdate, user: dict = Depends(get_current_user)):
    """Update org-level tier limits and model assignments (admin only)."""
    if user.get("role", "") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    current = _get_config()
    updates: dict = {}
    if body.tier1LimitUSD is not None:
        updates["tier1LimitUSD"] = str(body.tier1LimitUSD)
    if body.tier2LimitUSD is not None:
        updates["tier2LimitUSD"] = str(body.tier2LimitUSD)
    if body.tier3LimitUSD is not None:
        updates["tier3LimitUSD"] = str(body.tier3LimitUSD)
    if body.alertThreshold is not None:
        updates["alertThreshold"] = str(body.alertThreshold)
    if body.tier1Models is not None:
        updates["tier1Models"] = body.tier1Models
    if body.tier2Models is not None:
        updates["tier2Models"] = body.tier2Models
    if body.tier3Models is not None:
        updates["tier3Models"] = body.tier3Models

    merged = {**current, **updates, "orgId": _ORG_ID}
    db.put_item("budget-config", merged)
    return merged


@router.get("/me")
def get_my_budget(user: dict = Depends(get_current_user)):
    """Return the calling user's spend and tier status."""
    user_id = user.get("userId", "unknown")
    config = _get_config()
    budget = _get_user_budget(user_id)
    return {
        "userId": user_id,
        "currentTier": budget.get("currentTier", 1),
        "periodStart": budget.get("periodStart", ""),
        "spend": {
            "tier1": round(budget.get("tier1SpendUSD", 0.0), 4),
            "tier2": round(budget.get("tier2SpendUSD", 0.0), 4),
            "tier3": round(budget.get("tier3SpendUSD", 0.0), 4),
        },
        "limits": {
            "tier1": float(config.get("tier1LimitUSD", 500)),
            "tier2": float(config.get("tier2LimitUSD", 200)),
            "tier3": float(config.get("tier3LimitUSD", 9999)),
        },
        "alertThreshold": float(config.get("alertThreshold", 0.8)),
    }


@router.post("/users/{user_id}/reset")
def reset_user_budget(user_id: str, user: dict = Depends(get_current_user)):
    """Reset a user's spend counters and billing period (admin only)."""
    if user.get("role", "") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    now_month = datetime.now(timezone.utc).strftime("%Y-%m")
    db.put_item("user-budgets", {
        "userId": user_id,
        "currentTier": 1,
        "tier1SpendUSD": Decimal("0"),
        "tier2SpendUSD": Decimal("0"),
        "tier3SpendUSD": Decimal("0"),
        "periodStart": now_month,
        "lastAlertTier": 0,
    })
    return {"ok": True, "userId": user_id}


@router.get("/users")
def get_all_user_budgets(user: dict = Depends(get_current_user)):
    """Return budget status for all users (admin only)."""
    if user.get("role", "") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    config = _get_config()
    items = db.scan_items("user-budgets", limit=500)
    result = []
    for b in items:
        uid = b.get("userId", "")
        result.append({
            "userId": uid,
            "currentTier": int(b.get("currentTier", 1)),
            "periodStart": b.get("periodStart", ""),
            "spend": {
                "tier1": round(float(b.get("tier1SpendUSD", 0)), 4),
                "tier2": round(float(b.get("tier2SpendUSD", 0)), 4),
                "tier3": round(float(b.get("tier3SpendUSD", 0)), 4),
            },
            "limits": {
                "tier1": float(config.get("tier1LimitUSD", 500)),
                "tier2": float(config.get("tier2LimitUSD", 200)),
                "tier3": float(config.get("tier3LimitUSD", 9999)),
            },
        })
    return result

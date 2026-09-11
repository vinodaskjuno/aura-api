"""The role-aware dashboard endpoint.

One call per page load, returning exactly the blocks the caller's role needs.
The role is read off the token rather than passed in — a client cannot ask for
another role's dashboard, and there is nothing to spoof.

Why one endpoint and not one per role: a role is a server-side concept that
already lives in auth. Putting it in the URL would mean the UI deciding which
role it is, and would make the payload cacheable per-URL across users, which it
must not be.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from .auth import require_permission
from ..services import role_metrics

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/view")
def get_dashboard_view(user: dict = Depends(require_permission("dashboard"))):
    """Everything the landing page renders, for this user's role.

    Never raises on a data problem: `build_view` degrades each block on its own
    and reports which sources failed in `degraded`. The dashboard is the landing
    page, and a 500 here looks to a client like the whole product is down.
    """
    return role_metrics.build_view(user)


@router.get("/attention")
def get_attention(user: dict = Depends(require_permission("dashboard"))):
    """Just the attention rail — for polling without refetching every block."""
    view = role_metrics.build_view(user)
    return {"attention": view.get("attention", []),
            "headline": view.get("headline", {}),
            "generatedAt": view.get("generatedAt")}

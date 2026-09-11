"""The DevMate landing payload.

One call, the same envelope as the dashboard, plus a `hero` carrying the project
cards. The role is read off the token rather than the URL — a client cannot ask
for a different variant, and cost visibility is decided here rather than being
sent to everyone and hidden in the browser, which is what DevMate did before.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from .auth import require_permission
from ..services import role_metrics

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/devmate", tags=["devmate"])


@router.get("/view")
def get_devmate_view(user: dict = Depends(require_permission("dev_workspace"))):
    """Everything DevMate's landing screen renders, for this user.

    Never raises on a data problem: `build_devmate_view` degrades each source on
    its own and reports which ones failed in `degraded`.
    """
    return role_metrics.build_devmate_view(user)

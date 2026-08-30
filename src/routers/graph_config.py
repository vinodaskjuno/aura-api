"""Graph backend routing — read source, write targets, divergence, drain.

Switching the read source takes effect without restarting the backend: the setting
lives in DynamoDB, not in the lru_cached Settings object, and is re-read on a short
TTL. See src/graph/graph_config.py.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.graph import backends, graph_config, outbox, wipe
from src.routers.auth import require_permission

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/graph-config", tags=["graph-config"])


class ConfigRequest(BaseModel):
    readSource: str
    writeTargets: list[str]


class WipeRequest(BaseModel):
    # Repeated in the body so a mis-wired button cannot escalate scope: the UI has
    # to name the destructive action, not merely reach the endpoint.
    scope: str
    # Required for the full wipe only. The operator types the word, exactly as
    # reset-dev.sh makes them type the environment name.
    confirm: str = ""


def _safe_uri(uri: str) -> str:
    """Strip any userinfo. A Bolt URI can carry credentials, and this response is
    rendered in the UI and logged."""
    if "@" in uri and "//" in uri:
        scheme, _, rest = uri.partition("//")
        return f"{scheme}//{rest.rsplit('@', 1)[-1]}"
    return uri


def _backend_status() -> list[dict]:
    out = []
    for name in backends.configured_names():
        backend = backends.get_backend(name)
        out.append({
            "name": name,
            "dialect": backend.dialect.name if backend else name,
            "available": bool(backend and backend.is_available()),
            "uri": _safe_uri(backend.config.uri) if backend else "",
            "supportsFulltext": bool(backend and backend.dialect.supports_fulltext),
        })
    return out


@router.get("")
def get_graph_config(_: dict = Depends(require_permission("settings"))):
    config = graph_config.get_config(refresh=True)
    return {
        **config.as_dict(),
        "backends": _backend_status(),
        "pending": outbox.depth(),
    }


@router.put("")
def update_graph_config(body: ConfigRequest,
                        user: dict = Depends(require_permission("settings"))):
    known = set(backends.configured_names())
    if body.readSource not in known:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown backend {body.readSource!r}. Configured: {sorted(known) or 'none'}")

    unknown = [t for t in body.writeTargets if t not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown write target(s): {unknown}")

    if body.readSource not in body.writeTargets:
        # Reading from a store nobody writes to serves data that only gets staler.
        raise HTTPException(
            status_code=400,
            detail="The read source must also be a write target, otherwise it stops "
                   "receiving updates.")

    # Refuse to point reads at a store known to be behind. This is the whole reason
    # the outbox reports depth rather than just retrying quietly.
    pending = outbox.depth().get(body.readSource, 0)
    if pending:
        raise HTTPException(
            status_code=409,
            detail=f"{body.readSource} has {pending} write(s) pending replay. Drain the "
                   f"outbox first, or it will serve stale data.")

    backend = backends.get_backend(body.readSource)
    if backend is None or not backend.is_available():
        raise HTTPException(status_code=503,
                            detail=f"{body.readSource} is not reachable right now.")

    config = graph_config.set_config(body.readSource, body.writeTargets, user["username"])
    log.info("graph read source set to %s by %s", config.read_source, user["username"])
    return {**config.as_dict(), "backends": _backend_status(), "pending": outbox.depth()}


@router.get("/pending")
def get_pending(_: dict = Depends(require_permission("settings"))):
    return {"pending": outbox.depth()}


@router.post("/drain/{backend_name}")
def drain_backend(backend_name: str,
                  _: dict = Depends(require_permission("settings"))):
    if backend_name not in set(backends.configured_names()):
        raise HTTPException(status_code=404, detail=f"Unknown backend {backend_name!r}")
    return outbox.drain(backend_name)


# ── Danger zone: graph wipe ──────────────────────────────────────────────────
#
# Four independent gates, all of which must pass. The first is the one that keeps
# a future production deployment safe, because it denies by default rather than
# relying on somebody remembering to add a block.

_CONFIRM_WORD = "DELETE"


def _wipe_allowed() -> bool:
    from src.config_settings import get_settings
    return bool(getattr(get_settings(), "allow_graph_wipe", False))


@router.get("/wipe-status")
def wipe_status(_: dict = Depends(require_permission("settings"))):
    """Whether this server has been armed for wipes, and what it would affect.

    The UI renders the button disabled with this explanation rather than hiding it,
    so an operator can tell "not permitted here" from "feature missing".
    """
    config = graph_config.get_config()
    return {
        "enabled": _wipe_allowed(),
        "reason": "" if _wipe_allowed() else
                  "Graph wipe is not enabled on this server (ALLOW_GRAPH_WIPE).",
        "targets": list(config.write_targets),
        "scopes": list(wipe.SCOPES),
        "demoSources": list(wipe.DEMO_SOURCES),
        "confirmWord": _CONFIRM_WORD,
    }


@router.post("/wipe")
def wipe_graph_endpoint(body: WipeRequest,
                        user: dict = Depends(require_permission("settings"))):
    if not _wipe_allowed():
        raise HTTPException(
            status_code=403,
            detail="Graph wipe is not enabled on this server. It is switched on per "
                   "environment via ALLOW_GRAPH_WIPE and is off by default.")

    if body.scope not in wipe.SCOPES:
        raise HTTPException(status_code=400,
                            detail=f"scope must be one of {list(wipe.SCOPES)}")

    # Only the irreversible scope demands the typed word. Making the recoverable one
    # equally tedious would train operators to type it without reading.
    if body.scope == wipe.SCOPE_ALL and body.confirm.strip() != _CONFIRM_WORD:
        raise HTTPException(
            status_code=400,
            detail=f"Type {_CONFIRM_WORD} to confirm. This deletes every node in "
                   "every configured engine, including the in-graph audit history, "
                   "and cannot be undone without an EFS snapshot.")

    log.warning("GRAPH WIPE scope=%s requested by %s", body.scope, user["username"])
    report = wipe.wipe_graph(body.scope, user["username"])
    if not report.get("results"):
        raise HTTPException(status_code=503,
                            detail="No graph engine is reachable, so nothing was wiped.")
    return report

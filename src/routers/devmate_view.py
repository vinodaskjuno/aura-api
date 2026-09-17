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


@router.get("/projects/{project_id}/changes")
def get_project_changes(project_id: str, limit: int = 100,
                        user: dict = Depends(require_permission("dev_workspace"))):
    """Every decision taken on this project's proposed changes.

    `record_decision` has written eleven fields per decision since it was added —
    path, ±lines, who proposed it, in which session, who decided, when, and which way
    — and the ONLY reader in the codebase reduced all of it to a single percentage.
    Nothing ever rendered a row. This is that row.

    Sorted newest-first, and each entry carries the session so a change can be walked
    back to the conversation that produced it.
    """
    from src.database import dynamo_client as db

    try:
        # A Query on the partition key, not a scan: the table is keyed
        # (projectId, proposalId), so one project's ledger is one partition.
        rows = db.query_items("devmate-proposals", "projectId", project_id,
                              limit=max(1, min(int(limit), 500)))
    except Exception as exc:                                  # noqa: BLE001
        log.warning("proposal ledger query failed for %s: %s", project_id, exc)
        rows = []

    rows.sort(key=lambda r: str(r.get("decidedAt") or r.get("proposedAt") or ""),
              reverse=True)
    return {
        "projectId": project_id,
        "changes": [{
            "proposalId": str(r.get("proposalId") or ""),
            "path": str(r.get("path") or ""),
            "additions": int(r.get("additions") or 0),
            "deletions": int(r.get("deletions") or 0),
            "decision": str(r.get("decision") or ""),
            "decidedAt": str(r.get("decidedAt") or ""),
            "decidedBy": str(r.get("decidedBy") or ""),
            "proposedAt": str(r.get("proposedAt") or ""),
            "sessionId": str(r.get("sessionId") or ""),
            "userId": str(r.get("userId") or ""),
            # Filled from the moment a commit is recorded against a proposal; see
            # `git_ops`. Empty on every row written before that, and the UI says so
            # rather than implying the change was never committed.
            "commitSha": str(r.get("commitSha") or ""),
            "prUrl": str(r.get("prUrl") or ""),
            "href": (f"/dev-chat?session={r.get('sessionId')}"
                     if r.get("sessionId") else ""),
        } for r in rows[:limit]],
    }


@router.get("/projects/{project_id}/observability")
def get_project_observability(project_id: str,
                              user: dict = Depends(require_permission("dev_workspace"))):
    """Traces, spend, local-run state and telemetry health for one project — one call.

    Stitched here rather than in the browser because the three sources are three
    different stores with three different failure modes, and a page that fires three
    requests shows three different kinds of empty. Each section degrades on its own and
    says so, following `build_devmate_view`'s rule: absent data reports `unavailable`,
    never a confident zero.
    """
    out: dict = {"projectId": project_id, "degraded": []}

    def section(name, fn, default):
        try:
            return fn()
        except Exception as exc:                              # noqa: BLE001
            log.warning("observability section %s failed for %s: %s",
                        name, project_id, exc)
            out["degraded"].append(name)
            return default

    def traces():
        from src.aiobs import service as aiobs
        rows = aiobs.get_store().list_traces(project_id, limit=200)
        errors = sum(1 for r in rows if str(r.get("status") or "") == "error")
        return {
            "count": len(rows),
            "errorRate": round(100.0 * errors / len(rows), 1) if rows else None,
            "costUsd": round(sum(float(r.get("costUsd") or 0) for r in rows), 4),
            "tokens": sum(int(r.get("totalTokens") or 0) for r in rows),
            # Honest about the window, exactly as `/summary` is: this is recent
            # activity, not a total, and at 200 rows the difference matters.
            "exact": len(rows) < 200,
        }

    def spend():
        from src.services import gateway_analytics
        is_admin = user.get("role", "") in ("admin", "super_admin")
        rows = gateway_analytics.get_by_project(user.get("userId"), "7d", is_admin)
        mine = next((r for r in rows if str(r.get("projectId")) == project_id), None)
        return mine or {"projectId": project_id, "costUsd": 0, "requests": 0}

    def run():
        from src.qatest import queue
        apps = []
        for row in queue.list_runner_state():
            for app in row.get("apps") or []:
                if str(app.get("projectId") or "") == project_id:
                    apps.append({**{k: v for k, v in app.items() if k != "logTail"},
                                 "runner": row.get("name") or row.get("runner") or "",
                                 "ownerId": row.get("ownerId", ""),
                                 "stale": bool(row.get("stale"))})
        return {"apps": apps, "running": bool(apps)}

    def telemetry():
        from src.aiobs import ingest_status
        return ingest_status.status_for(project_id)

    def checks():
        """Policy, summarised. Folded in here rather than left as a fifth request
        because the status strip needs one number from it, and four panels each
        fetching their own source is what produced four different kinds of empty.

        `applicable: false` is preserved rather than flattened to zero: a project
        that ships no IaC and one that passed everything are different facts, and
        only one of them is reassuring."""
        from src.qatest import appserver, policy

        root, _checked = appserver.locate(project_id)
        if root is None:
            return {"applicable": False, "reason": "no working copy on this server"}
        plan = policy.plan_checks(root)
        if not plan:
            return {"applicable": False,
                    "reason": "this project declares no infrastructure-as-code"}
        # The same two calls `/api/qa/projects/{id}/policy` makes, so the strip and the
        # panel can never derive a different headline from the same evaluation.
        verdicts = [policy.run_check(root, check)[0] for check in plan]
        resources = policy.resource_view(root)
        return {
            "applicable": True,
            "passed": sum(1 for ok in verdicts if ok),
            "total": len(verdicts),
            "resources": len(resources),
            "findings": sum(1 for r in resources
                            if r["applicable"] and r["passed"] < r["applicable"]),
            # Its own number, never folded into "passing": a resource no control reads
            # was not assessed, which is a different fact from having passed.
            "notChecked": sum(1 for r in resources if not r["applicable"]),
        }

    out["traces"] = section("traces", traces, {"count": None})
    out["spend"] = section("spend", spend, {"costUsd": None})
    out["run"] = section("run", run, {"apps": [], "running": False})
    out["telemetry"] = section("telemetry", telemetry, {"state": "unknown"})
    out["checks"] = section("checks", checks, {"applicable": None})
    # Cost follows the same rule as the landing page rather than inventing a second one.
    if user.get("role", "") not in ("admin", "super_admin"):
        out["spend"] = {"restricted": True}
    return out

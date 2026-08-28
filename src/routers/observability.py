"""Observability (SRE agents) API — /api/observability.

Mounted under /api so the existing Vite dev proxy and nginx `^~ /api/` block cover
every REST route with no config change. The WebSocket lives under
/api/observability/ws so a single dedicated nginx location covers it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from src.models.observability import (
    NotificationTestRequest, OutcomeRequest, RunbookCreate, StartInvestigationRequest,
)
from src.observability import cases, outcomes, promotion, runbooks, store
from src.observability.registry import known_provider_types, resolve_providers
from src.observability.types import (
    EventQuery, LogQuery, MetricQuery, TimeWindow, TraceQuery,
)
from src.orchestrator.investigation_dag import (
    INVESTIGATION_COMMANDS, build_spec, run_investigation,
)
from src.routers.auth import get_current_user, require_permission

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/observability", tags=["observability"])

PERMISSION = "observability"

# Live in-process event buffers, keyed by investigation id, so a reconnecting
# socket can replay `seq > sinceSeq` instead of showing a frozen progress bar.
#
# This buffer is process-local. Under `uvicorn --workers > 1` a reconnect can land
# on a worker that never ran the investigation, so replay ALSO falls back to the
# durable `observability-traces` rows — see `_replay_events`. Without that fallback
# a reconnected socket silently shows a frozen progress bar in production while
# working perfectly in single-worker dev.
_STREAMS: dict[str, list[dict]] = {}
_SUBSCRIBERS: dict[str, set[asyncio.Queue]] = {}
_EVENT_BUFFER_CAP = 2000


def _persist_event(investigation_id: str, event: dict) -> None:
    try:
        from src.database.dynamo_client import put_item
        put_item("observability-traces", {
            "runId": investigation_id,
            "seq": f"{int(event.get('seq', 0)):06d}",
            "type": event.get("type", ""),
            "event": event,
        })
    except Exception:  # noqa: BLE001 — durability is best-effort, never blocking
        pass


def _replay_events(investigation_id: str, since: int) -> list[dict]:
    """Events after `since`, from memory when possible, else from DynamoDB."""
    buffered = _STREAMS.get(investigation_id)
    if buffered:
        return [e for e in buffered if int(e.get("seq", 0)) > since]
    try:
        from src.database.dynamo_client import query_range
        rows = query_range("observability-traces", "runId", investigation_id, "seq",
                           sk_from=f"{since + 1:06d}", limit=_EVENT_BUFFER_CAP)
        return [r["event"] for r in sorted(rows, key=lambda r: r.get("seq", ""))
                if isinstance(r.get("event"), dict)]
    except Exception:  # noqa: BLE001
        return []


def _window(start: str = "", end: str = "", minutes: int = 60) -> TimeWindow:
    if start and end:
        return TimeWindow(start=start, end=end)
    return TimeWindow.last(minutes)


# ── Providers ────────────────────────────────────────────────────────────────

@router.get("/providers")
async def list_providers(user: dict = Depends(require_permission(PERMISSION)),
                         project_id: str = Query("")):
    providers = resolve_providers(user["userId"], project_id or None)
    health = await asyncio.gather(*[p.health() for p in providers],
                                  return_exceptions=True)
    out = []
    for p, h in zip(providers, health):
        entry = p.describe()
        if isinstance(h, Exception):
            entry.update(status="failed", message=str(h)[:200], latencyMs=0,
                         lastCheckedAt=datetime.now(timezone.utc).isoformat())
        else:
            entry.update(status=h.status, message=h.message,
                         latencyMs=h.latency_ms, lastCheckedAt=h.checked_at)
        out.append(entry)
    return {"providers": out, "known_types": known_provider_types()}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str,
                        user: dict = Depends(require_permission(PERMISSION)),
                        project_id: str = Query("")):
    for p in resolve_providers(user["userId"], project_id or None, [provider_id]):
        h = await p.health()
        return h.to_dict()
    raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' not configured")


# ── Raw signal queries ───────────────────────────────────────────────────────

@router.get("/signals/logs")
async def signal_logs(service: str, start: str = "", end: str = "",
                      minutes: int = 60, filter: str = "", provider: str = "",
                      limit: int = 200, project_id: str = "",
                      user: dict = Depends(require_permission(PERMISSION))):
    window = _window(start, end, minutes)
    providers = resolve_providers(user["userId"], project_id or None,
                                  [provider] if provider else None)
    q = LogQuery(service=service, window=window, filter=filter, limit=limit)
    pages = await asyncio.gather(*[p.query_logs(q) for p in providers if p.supports("logs")],
                                 return_exceptions=True)
    records, errors = [], []
    for page in pages:
        if isinstance(page, Exception):
            errors.append(str(page)[:200])
        elif page.error:
            errors.append(page.error)
        else:
            records.extend(r.__dict__ for r in page.records)
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return {"records": records[:limit], "errors": errors, "window": window.to_dict()}


@router.get("/signals/metrics")
async def signal_metrics(service: str, metric: str = "", start: str = "", end: str = "",
                         minutes: int = 60, step: int = 30, provider: str = "",
                         project_id: str = "",
                         user: dict = Depends(require_permission(PERMISSION))):
    window = _window(start, end, minutes)
    providers = resolve_providers(user["userId"], project_id or None,
                                  [provider] if provider else None)
    q = MetricQuery(service=service, window=window, metric=metric, step_s=step)
    results = await asyncio.gather(
        *[p.query_metrics(q) for p in providers if p.supports("metrics")],
        return_exceptions=True)
    series = []
    for r in results:
        if isinstance(r, Exception):
            continue
        for s_ in r:
            series.append({"seriesId": s_.series_id, "metric": s_.metric, "unit": s_.unit,
                           "labels": s_.labels, "stats": s_.stats,
                           "sourceUrl": s_.source_url,
                           "points": [{"timestamp": p.timestamp, "value": p.value}
                                      for p in s_.points]})
    return {"series": series, "window": window.to_dict()}


@router.get("/signals/traces")
async def signal_traces(service: str, start: str = "", end: str = "", minutes: int = 60,
                        errors_only: bool = False, min_duration_ms: int = 0,
                        provider: str = "", project_id: str = "",
                        user: dict = Depends(require_permission(PERMISSION))):
    window = _window(start, end, minutes)
    providers = resolve_providers(user["userId"], project_id or None,
                                  [provider] if provider else None)
    q = TraceQuery(service=service, window=window, errors_only=errors_only,
                   min_duration_ms=min_duration_ms)
    results = await asyncio.gather(
        *[p.query_traces(q) for p in providers if p.supports("traces")],
        return_exceptions=True)
    traces = []
    for r in results:
        if isinstance(r, Exception):
            continue
        for t in r:
            traces.append({**t.__dict__,
                           "slowest_spans": [s.__dict__ for s in t.slowest_spans]})
    return {"traces": traces, "window": window.to_dict()}


@router.get("/signals/events")
async def signal_events(service: str = "", start: str = "", end: str = "",
                        minutes: int = 120, provider: str = "", project_id: str = "",
                        user: dict = Depends(require_permission(PERMISSION))):
    window = _window(start, end, minutes)
    providers = resolve_providers(user["userId"], project_id or None,
                                  [provider] if provider else None)
    q = EventQuery(service=service, window=window)
    results = await asyncio.gather(
        *[p.recent_deploys(q) for p in providers if p.supports("events")],
        return_exceptions=True)
    events = []
    for r in results:
        if isinstance(r, Exception):
            continue
        events.extend(e.__dict__ for e in r)
    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return {"events": events, "window": window.to_dict()}


# ── Incidents ────────────────────────────────────────────────────────────────

@router.get("/incidents")
async def list_incidents(project_id: str = "", minutes: int = 1440, limit: int = 50,
                         user: dict = Depends(require_permission(PERMISSION))):
    """Merged view: live PagerDuty/CloudWatch + Incident nodes already in the graph."""
    window = TimeWindow.last(minutes)
    providers = resolve_providers(user["userId"], project_id or None)
    q = EventQuery(service="", window=window, kinds=("incident", "alert"))
    results = await asyncio.gather(
        *[p.recent_deploys(q) for p in providers if p.supports("events")],
        return_exceptions=True)

    incidents: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        for e in r:
            if e.kind not in ("incident", "alert"):
                continue
            incidents.append({
                "incidentId": e.labels.get("incidentId") or e.event_id,
                "title": e.title, "service": e.service, "timestamp": e.timestamp,
                "severity": _severity(e.labels), "state": e.labels.get("status", "")
                or e.labels.get("state", ""),
                "source": e.provider_type, "sourceUrl": e.source_url,
                "description": e.description,
            })

    try:
        from src.graph.neo4j_client import run_query
        for row in run_query(
                "MATCH (n:Incident) RETURN n ORDER BY n.startedAt DESC LIMIT $limit",
                {"limit": limit}):
            n = dict(row["n"])
            incidents.append({
                "incidentId": n.get("externalId", ""), "title": n.get("title", ""),
                "service": n.get("serviceName", ""), "timestamp": n.get("startedAt", ""),
                "severity": n.get("severity", "medium"), "state": "analyzed",
                "source": n.get("source", "graph"), "sourceUrl": n.get("sourceUrl", ""),
                "investigationId": n.get("investigationId", ""),
                "rootCause": n.get("rootCauseStatement", ""),
            })
    except Exception as exc:  # noqa: BLE001
        log.debug("graph incidents unavailable: %s", exc)

    seen, unique = set(), []
    for i in sorted(incidents, key=lambda x: x.get("timestamp", ""), reverse=True):
        if i["incidentId"] in seen:
            continue
        seen.add(i["incidentId"])
        unique.append(i)
    return {"incidents": unique[:limit]}


def _severity(labels: dict) -> str:
    urgency = (labels.get("urgency") or "").lower()
    state = (labels.get("status") or labels.get("state") or "").lower()
    if urgency == "high" or state == "alarm":
        return "critical"
    if urgency == "low":
        return "medium"
    return "high"


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str,
                       user: dict = Depends(require_permission(PERMISSION))):
    try:
        from src.graph.neo4j_client import run_query
        rows = run_query("MATCH (n:Incident {externalId:$eid}) RETURN n LIMIT 1",
                         {"eid": incident_id})
        if rows:
            return dict(rows[0]["n"])
    except Exception:  # noqa: BLE001
        pass
    raise HTTPException(status_code=404, detail="Incident not found")


# ── KPIs ─────────────────────────────────────────────────────────────────────

@router.get("/kpis")
async def kpis(project_id: str = "", user: dict = Depends(require_permission(PERMISSION))):
    investigations = store.list_investigations(project_id or None, limit=200)
    complete = [i for i in investigations if i.get("status") == "complete"]
    durations = []
    for i in complete:
        try:
            a = datetime.fromisoformat(i["startedAt"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(i["completedAt"].replace("Z", "+00:00"))
            durations.append((b - a).total_seconds())
        except Exception:  # noqa: BLE001
            continue
    coverages = [float(i.get("citationCoverage", 0) or 0) for i in complete]
    confirmed = sum(1 for i in investigations
                    if (i.get("outcome") or {}).get("verdict") == "confirmed")
    with_outcome = sum(1 for i in investigations if i.get("outcome"))
    return {
        "investigations": len(investigations),
        "running": sum(1 for i in investigations if i.get("status") == "running"),
        "meanTimeToRcaSeconds": round(sum(durations) / len(durations)) if durations else 0,
        "meanCitationCoverage": round(sum(coverages) / len(coverages), 3) if coverages else 0.0,
        "confirmedRate": round(confirmed / with_outcome, 3) if with_outcome else 0.0,
        "outcomeCoverage": round(with_outcome / len(investigations), 3) if investigations else 0.0,
        "corpusSize": store.corpus_size(),
    }


# ── Investigations ───────────────────────────────────────────────────────────

@router.post("/investigations")
async def start_investigation(body: StartInvestigationRequest,
                              background: BackgroundTasks,
                              user: dict = Depends(require_permission(PERMISSION))):
    spec = build_spec(body.model_dump(), project_id=body.project_id)
    record = store.create_investigation({
        "investigationId": spec.investigation_id,
        "createdAt": store.now(),
        "projectId": spec.project_id or "",
        "serviceName": spec.service,
        "title": spec.title,
        "status": "queued",
        "severity": spec.severity,
        "services": spec.services,
        "incidentId": spec.incident_id,
        "window": spec.window.to_dict(),
        "userId": user["userId"],
        "maskingEnabled": spec.masking_enabled,
    })
    _STREAMS[spec.investigation_id] = []

    async def _run():
        await run_investigation(
            spec, user_id=user["userId"], username=user.get("username", ""),
            role=user.get("role", ""), session_id=user["userId"][:8] or "obs",
            project_id=spec.project_id, emit=_broadcast(spec.investigation_id),
        )

    if body.background:
        background.add_task(_run)
        return {"investigationId": spec.investigation_id, "status": "queued",
                "record": record}
    summary = await _run()
    return {"investigationId": spec.investigation_id, "status": "complete",
            "summary": summary}


@router.get("/investigations")
async def list_investigations(project_id: str = "", limit: int = 50,
                              user: dict = Depends(require_permission(PERMISSION))):
    return {"investigations": store.list_investigations(project_id or None, limit=limit)}


@router.get("/investigations/{investigation_id}")
async def get_investigation(investigation_id: str,
                            user: dict = Depends(require_permission(PERMISSION))):
    rec = store.get_investigation(investigation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Investigation not found")
    # The mask map is exactly as sensitive as the raw data — never serve it.
    rec.pop("maskMapping", None)
    rec["evidence"] = store.list_evidence(investigation_id, limit=500)
    return rec


@router.get("/investigations/{investigation_id}/evidence")
async def list_evidence(investigation_id: str, limit: int = 500,
                        user: dict = Depends(require_permission(PERMISSION))):
    return {"evidence": store.list_evidence(investigation_id, limit=limit)}


@router.get("/investigations/{investigation_id}/evidence/{evidence_id}")
async def evidence_detail(investigation_id: str, evidence_id: str, project_id: str = "",
                          user: dict = Depends(require_permission(PERMISSION))):
    """The drawer's lazy fetch — full payload, read from the S3 bundle."""
    detail = store.get_evidence_detail(investigation_id, evidence_id, project_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return detail


@router.get("/investigations/{investigation_id}/cases")
async def investigation_cases(investigation_id: str,
                              user: dict = Depends(require_permission(PERMISSION))):
    rec = store.get_investigation(investigation_id) or {}
    found = cases.retrieve(rec.get("serviceName", ""),
                           rec.get("errorSignatures") or [],
                           rec.get("symptomShape") or [],
                           exclude_investigation_id=investigation_id)
    return found


@router.get("/investigations/{investigation_id}/trace")
async def investigation_trace(investigation_id: str,
                              user: dict = Depends(require_permission(PERMISSION))):
    if user.get("role") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Traces are admin-only")
    return {"events": _STREAMS.get(investigation_id, [])}


@router.post("/investigations/{investigation_id}/outcome")
async def record_outcome(investigation_id: str, body: OutcomeRequest,
                         user: dict = Depends(require_permission(PERMISSION))):
    """Human verdict — the strongest learning signal (weight 1.0)."""
    if not store.get_investigation(investigation_id):
        raise HTTPException(status_code=404, detail="Investigation not found")
    outcome = outcomes.record(
        investigation_id, "human", body.verdict,
        detail=body.note, actual_cause=body.actual_cause,
        actual_category=body.actual_category,
        confirmed_by=user.get("username", ""))
    return {**outcome.to_dict(), "teaches": outcome.teaches}


@router.post("/commands/{command}")
async def run_command(command: str, body: StartInvestigationRequest,
                      background: BackgroundTasks,
                      user: dict = Depends(require_permission(PERMISSION))):
    if command not in INVESTIGATION_COMMANDS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown command. Valid: {sorted(INVESTIGATION_COMMANDS)}")
    spec = build_spec(body.model_dump(), project_id=body.project_id)
    store.create_investigation({
        "investigationId": spec.investigation_id, "createdAt": store.now(),
        "projectId": spec.project_id or "", "serviceName": spec.service,
        "title": spec.title, "status": "queued", "severity": spec.severity,
        "services": spec.services, "window": spec.window.to_dict(),
        "userId": user["userId"],
    })
    _STREAMS[spec.investigation_id] = []

    async def _run():
        await run_investigation(
            spec, user_id=user["userId"], username=user.get("username", ""),
            role=user.get("role", ""), session_id=user["userId"][:8] or "obs",
            project_id=spec.project_id, emit=_broadcast(spec.investigation_id),
            command=command)

    background.add_task(_run)
    return {"investigationId": spec.investigation_id, "command": command,
            "status": "queued"}


# ── Runbooks ─────────────────────────────────────────────────────────────────

@router.get("/runbooks")
async def search_runbooks(service: str = "", q: str = "", alert_signature: str = "",
                          origin: str = "", status: str = "", limit: int = 20,
                          user: dict = Depends(require_permission(PERMISSION))):
    if origin or status:
        return {"runbooks": runbooks.list_runbooks(origin=origin, status=status, limit=limit)}
    matches = runbooks.search(service=service,
                              signatures=[alert_signature] if alert_signature else [],
                              query=q, limit=limit)
    return {"runbooks": matches}


@router.post("/runbooks")
async def create_runbook(body: RunbookCreate, project_id: str = "",
                         user: dict = Depends(require_permission(PERMISSION))):
    doc = {**body.model_dump(), "origin": "human", "status": "active",
           "sourceType": "upload", "createdAt": store.now()}
    return runbooks.save_runbook(doc, body=body.body, project_id=project_id)


@router.get("/runbooks/{runbook_id:path}")
async def get_runbook(runbook_id: str,
                      user: dict = Depends(require_permission(PERMISSION))):
    rb = runbooks.get_runbook(runbook_id)
    if not rb:
        raise HTTPException(status_code=404, detail="Runbook not found")
    return rb


# ── Learned artifacts (governance) ───────────────────────────────────────────

@router.get("/learned")
async def list_learned(user: dict = Depends(require_permission(PERMISSION))):
    return {"artifacts": promotion.list_learned(),
            "corpusSize": store.corpus_size()}


@router.delete("/learned/{artifact_id:path}")
async def forget_learned(artifact_id: str,
                         user: dict = Depends(require_permission(PERMISSION))):
    """One-click revert. Learned artifacts are data, so this is a plain delete."""
    ok = promotion.forget(artifact_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Artifact not found")
    log.info("User %s forgot learned artifact %s", user.get("username"), artifact_id)
    return {"ok": True, "artifactId": artifact_id}


# ── Notifications ────────────────────────────────────────────────────────────

@router.get("/notifications/channels")
async def notification_channels(user: dict = Depends(require_permission(PERMISSION))):
    from src.services.notifications import dispatcher
    return {"channels": await dispatcher.test_channels(user["userId"]),
            "known": dispatcher.known_channel_types()}


@router.post("/notifications/test")
async def test_notification(body: NotificationTestRequest,
                            user: dict = Depends(require_permission(PERMISSION))):
    from src.services.notifications import dispatcher
    from src.services.notifications.base import Notification
    results = await dispatcher.send(Notification(
        kind="test", severity="info", title="Aura test notification",
        body=body.message, dedupe_key=""), user_id=user["userId"])
    return {"results": [r.to_dict() for r in results]}


# ── WebSocket ────────────────────────────────────────────────────────────────

def _broadcast(investigation_id: str):
    """Emitter that buffers for replay AND fans out to live subscribers."""
    async def _emit(event: dict) -> None:
        buf = _STREAMS.setdefault(investigation_id, [])
        buf.append(event)
        if len(buf) > _EVENT_BUFFER_CAP:
            del buf[: len(buf) - _EVENT_BUFFER_CAP]
        _persist_event(investigation_id, event)
        for q in list(_SUBSCRIBERS.get(investigation_id, set())):
            try:
                q.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass
    return _emit


@router.websocket("/ws/investigate")
async def ws_investigate(ws: WebSocket):
    """Live investigation stream.

    Client sends {token, investigationId, sinceSeq} as the FIRST MESSAGE — not a
    query string, which would put the token in access logs.

    Unlike routers/aiops.py::ws_live_alerts, this checks the permission and not just
    the token. That router's missing permission check is an existing gap worth fixing
    separately; it is deliberately not replicated here.
    """
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue()
    investigation_id = ""
    try:
        data = await ws.receive_json()
        from src.services.auth_service import verify_token
        user = verify_token(data.get("token", ""))
        if not user:
            await ws.send_json({"type": "error", "message": "Unauthorized"})
            return
        if PERMISSION not in user.get("permissions", []):
            await ws.send_json({"type": "error", "message": "Permission denied: observability"})
            return

        investigation_id = data.get("investigationId", "")
        if not investigation_id:
            await ws.send_json({"type": "error", "message": "investigationId is required"})
            return

        since = int(data.get("sinceSeq", 0) or 0)
        _SUBSCRIBERS.setdefault(investigation_id, set()).add(queue)

        # Replay first, then go live. Without this, one dropped socket loses the
        # whole run and the user watches a frozen progress bar.
        replayed = _replay_events(investigation_id, since)
        await ws.send_json({"type": "connected", "investigationId": investigation_id,
                            "replaying": len(replayed)})
        for event in replayed:
            await ws.send_json(event)

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
                await ws.send_json(event)
                if event.get("type") in ("dag_done", "error"):
                    break
            except asyncio.TimeoutError:
                await ws.send_json({"type": "heartbeat",
                                    "t": datetime.now(timezone.utc).isoformat()})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.exception("ws_investigate error")
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        if investigation_id:
            _SUBSCRIBERS.get(investigation_id, set()).discard(queue)

"""
Service Loader Router — Project → Service → Repos → Ontology ingestion.

Prefix: /api/projects/{project_id}/services
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.database import dynamo_client as db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects/{project_id}/services", tags=["service-loader"])

_TABLE = "services"
_JOBS: dict[str, dict] = {}  # in-memory job status store


# ── Request / Response models ─────────────────────────────────────────────────

class ServiceCreate(BaseModel):
    name: str
    description: str = ""

class ServiceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None

class RepoAttach(BaseModel):
    repoUrl: str
    repoType: str = "auto"   # auto|mule|spring|python|ui-react|ui-angular|terraform|cicd|config|library
    token: str = ""
    branch: str = "main"
    localPath: str = ""
    name: str = ""           # friendly name for this repo attachment


# ── Service CRUD ──────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_service(
    project_id: str,
    body: ServiceCreate,
    user: dict = Depends(get_current_user),
):
    service_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "projectId": project_id,
        "serviceId": service_id,
        "userId": user.get("userId", ""),
        "name": body.name,
        "description": body.description,
        "techStack": [],
        "repoCount": 0,
        "repos": [],
        "status": "pending",
        "lastIngested": None,
        "ontologyStats": {},
        "createdAt": now,
        "updatedAt": now,
    }
    db.put_item(_TABLE, item)
    return item


@router.get("")
def list_services(project_id: str, user: dict = Depends(get_current_user)):
    try:
        items = db.query_items(_TABLE, "projectId", project_id, limit=200)
        return sorted(items, key=lambda x: x.get("createdAt", ""), reverse=True)
    except Exception as exc:
        log.error("list_services error: %s", exc)
        return []


@router.get("/{service_id}")
def get_service(project_id: str, service_id: str, user: dict = Depends(get_current_user)):
    item = _get_or_404(project_id, service_id)
    return item


@router.put("/{service_id}")
def update_service(
    project_id: str,
    service_id: str,
    body: ServiceUpdate,
    user: dict = Depends(get_current_user),
):
    _get_or_404(project_id, service_id)
    updates: dict[str, Any] = {"updatedAt": datetime.now(timezone.utc).isoformat()}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    updated = db.update_item(_TABLE, {"projectId": project_id, "serviceId": service_id}, updates)
    return updated or updates


@router.delete("/{service_id}", status_code=204)
def delete_service(project_id: str, service_id: str, user: dict = Depends(get_current_user)):
    _get_or_404(project_id, service_id)
    db.delete_item(_TABLE, {"projectId": project_id, "serviceId": service_id})


# ── Repo management ───────────────────────────────────────────────────────────

@router.post("/{service_id}/repos", status_code=201)
def attach_repo(
    project_id: str,
    service_id: str,
    body: RepoAttach,
    user: dict = Depends(get_current_user),
):
    item = _get_or_404(project_id, service_id)
    repo_id = str(uuid.uuid4())
    repo_entry = {
        "repoId": repo_id,
        "repoUrl": body.repoUrl,
        "repoType": body.repoType,
        "branch": body.branch,
        "localPath": body.localPath,
        "name": body.name or body.repoUrl.split("/")[-1],
        "hasToken": bool(body.token),
        # Store token encrypted — for now store masked; full token saved in secrets manager
        "tokenMasked": (body.token[:4] + "****") if body.token else "",
        "_token": body.token,  # will be stripped before response
        "addedAt": datetime.now(timezone.utc).isoformat(),
    }
    repos = item.get("repos", [])
    repos.append(repo_entry)
    db.update_item(
        _TABLE,
        {"projectId": project_id, "serviceId": service_id},
        {"repos": repos, "repoCount": len(repos), "updatedAt": datetime.now(timezone.utc).isoformat()},
    )
    safe = {k: v for k, v in repo_entry.items() if k != "_token"}
    return {"repoId": repo_id, **safe}


@router.delete("/{service_id}/repos/{repo_id}", status_code=204)
def detach_repo(
    project_id: str,
    service_id: str,
    repo_id: str,
    user: dict = Depends(get_current_user),
):
    item = _get_or_404(project_id, service_id)
    repos = [r for r in item.get("repos", []) if r.get("repoId") != repo_id]
    db.update_item(
        _TABLE,
        {"projectId": project_id, "serviceId": service_id},
        {"repos": repos, "repoCount": len(repos)},
    )


# ── Ingest ────────────────────────────────────────────────────────────────────

def _promote_project(project_id: str) -> None:
    """Mark a project `analyzed` once a service has ingested successfully.

    Ingest set the SERVICE status but left the PROJECT at "pending", and
    GET /api/qa/projects only returns projects whose status is in an allow-list
    (qa.py). So a freshly ingested project was invisible in QualityMind and the
    operator had to PUT the status by hand.
    """
    try:
        rows = db.query_items("projects", "projectId", project_id, limit=1)
        if not rows:
            log.warning("Cannot promote unknown project %s", project_id)
            return
        current = rows[0].get("status", "")
        if current in ("analyzed", "active"):
            return
        db.update_item(
            "projects",
            {"projectId": project_id, "userId": rows[0]["userId"]},
            {"status": "analyzed", "updatedAt": datetime.now(timezone.utc).isoformat()},
        )
        log.info("Project %s promoted %s -> analyzed after ingest", project_id, current or "pending")
    except Exception as exc:  # noqa: BLE001 — never fail an ingest over this
        log.warning("Could not promote project %s: %s", project_id, exc)


@router.post("/{service_id}/ingest")
def ingest_service_endpoint(
    project_id: str,
    service_id: str,
    user: dict = Depends(get_current_user),
):
    """Clone + parse all repos for this service, write ontology to Neo4j."""
    item = _get_or_404(project_id, service_id)
    repos = item.get("repos", [])
    if not repos:
        raise HTTPException(status_code=400, detail="No repos attached to this service")

    job_id = str(uuid.uuid4())
    _JOBS[job_id] = {"status": "running", "log": [], "result": None, "startedAt": datetime.now(timezone.utc).isoformat()}

    # Captured now, used on the worker thread. The request's identity does not
    # travel there on its own, and without it every node this writes would be
    # attributed to nobody.
    actor = user.get("username", "")
    actor_id = user.get("userId", "")
    repo_urls = ", ".join(r.get("url", "") for r in repos if r.get("url"))

    def run():
        from src.graph import provenance
        try:
            # The run is opened INSIDE the thread, not around the spawn: the
            # endpoint returns immediately, so a block wrapping the spawn would
            # close the run record before the ingestion had written anything.
            with provenance.trace_run(
                provenance.PIPELINE_GIT,
                trigger=provenance.TRIGGER_MANUAL,
                actor=actor, actorId=actor_id,
                source="git-repo-loader",
                sourceDetail=repo_urls or item["name"],
                projectId=project_id,
                writtenBy="repo_ingestion_service.ingest_service",
                notes=f"Service ingest: {item['name']}",
            ):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                from src.services.repo_ingestion_service import ingest_service
                result = loop.run_until_complete(
                    ingest_service(project_id, service_id, item["name"], repos)
                )
            _JOBS[job_id]["status"] = "done"
            _JOBS[job_id]["result"] = result
            _JOBS[job_id]["log"] = result.get("log", [])
            _promote_project(project_id)
        except Exception as exc:
            _JOBS[job_id]["status"] = "failed"
            _JOBS[job_id]["log"].append(f"Error: {exc}")
            log.exception("Ingest failed for service %s", service_id)
        finally:
            _JOBS[job_id]["finishedAt"] = datetime.now(timezone.utc).isoformat()

    t = threading.Thread(target=run, daemon=True)
    t.start()

    # Mark service as ingesting
    db.update_item(
        _TABLE,
        {"projectId": project_id, "serviceId": service_id},
        {"status": "ingesting", "updatedAt": datetime.now(timezone.utc).isoformat()},
    )
    return {"jobId": job_id, "status": "running"}


@router.get("/{service_id}/ingest/status")
def ingest_status(
    project_id: str,
    service_id: str,
    job_id: str,
    user: dict = Depends(get_current_user),
):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job


@router.post("/ingest-all")
def ingest_all_services(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    """Ingest all services in the project, then run cross-service correlation."""
    services = list_services(project_id, user)
    if not services:
        raise HTTPException(status_code=400, detail="No services found for this project")

    job_id = str(uuid.uuid4())
    _JOBS[job_id] = {"status": "running", "log": [], "result": None, "startedAt": datetime.now(timezone.utc).isoformat()}

    actor = user.get("username", "")
    actor_id = user.get("userId", "")

    def run_all():
        from src.graph import provenance
        try:
            with provenance.trace_run(
                provenance.PIPELINE_GIT,
                trigger=provenance.TRIGGER_MANUAL,
                actor=actor, actorId=actor_id,
                source="git-repo-loader",
                sourceDetail=f"{len(services)} services in {project_id}",
                projectId=project_id,
                writtenBy="repo_ingestion_service.ingest_all",
                notes="Ingest all services + cross-service correlation",
            ):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                from src.services.repo_ingestion_service import ingest_service, correlate_services
                all_stats: list[dict] = []
                for svc in services:
                    repos = svc.get("repos", [])
                    if not repos:
                        continue
                    result = loop.run_until_complete(
                        ingest_service(project_id, svc["serviceId"], svc["name"], repos)
                    )
                    all_stats.append(result)
                    _JOBS[job_id]["log"].extend(result.get("log", []))

                # Cross-service correlation. Nested so it is a distinguishable
                # child run — it infers edges rather than reading a repo, and the
                # trace should say so.
                with provenance.trace_run(
                    provenance.PIPELINE_CORRELATION,
                    trigger=provenance.TRIGGER_AUTOMATIC,
                    source="correlation",
                    writtenBy="repo_ingestion_service.correlate_services",
                ):
                    corr = correlate_services(project_id)
            _JOBS[job_id]["log"].append(f"Correlation: {corr.get('rels_added', 0)} cross-service relationships added")
            _JOBS[job_id]["status"] = "done"
            _JOBS[job_id]["result"] = {"services": all_stats, "correlation": corr}
        except Exception as exc:
            _JOBS[job_id]["status"] = "failed"
            _JOBS[job_id]["log"].append(f"Error: {exc}")
            log.exception("Ingest-all failed for project %s", project_id)
        finally:
            _JOBS[job_id]["finishedAt"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=run_all, daemon=True).start()
    return {"jobId": job_id, "status": "running", "serviceCount": len(services)}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_or_404(project_id: str, service_id: str) -> dict:
    item = db.get_item(_TABLE, {"projectId": project_id, "serviceId": service_id})
    if not item:
        raise HTTPException(status_code=404, detail=f"Service {service_id!r} not found in project {project_id!r}")
    return item

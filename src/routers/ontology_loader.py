"""Data Loader API — MCP ingestion, API ingestion, file upload, versions, scheduler.

Routes:
  POST /api/ontology/load/mcp          Ingest via selected MCP connectors
  POST /api/ontology/load/api          Ingest from external API endpoint
  POST /api/ontology/load/file         Upload JSON / Excel (.xlsx) / CSV
  GET  /api/ontology/versions          List OntologyVersion records (paginated)
  GET  /api/ontology/versions/{id}     Version detail with stats and diff
  GET  /api/ontology/nodes/{id}/version  Which version introduced this node
  GET  /api/ontology/schedule/status   Scheduler state + next-run + history
  POST /api/ontology/schedule          Update cron expression and enabled flag
  POST /api/ontology/schedule/run-now  Manually trigger a scheduled job
"""
from __future__ import annotations

import io
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from src.routers.auth import get_current_user, require_permission

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ontology", tags=["ontology-loader"])


# ── Request/response models ────────────────────────────────────────────────────

class McpLoadRequest(BaseModel):
    # Connector ids of the caller's registered MCP servers. Named `sources` for
    # backwards compatibility with the existing UI payload — it used to carry eight
    # hardcoded strings ("git", "servicenow", ...) which were never read.
    sources: list[str] = []
    delta_since: str | None = None
    notes: str = ""
    # Escape hatch, off by default: run the old synthetic generator instead of talking
    # to real servers. Kept because it is the only way to populate a graph with no
    # connectors configured, and because removing it would break the demo fixtures.
    synthetic: bool = False


class ApiLoadRequest(BaseModel):
    url: str
    auth_type: str = "none"       # none | bearer | basic | apikey
    token: str | None = None
    username: str | None = None
    password: str | None = None
    api_key_header: str | None = None
    api_key_value: str | None = None
    notes: str = ""


class ScheduleConfigRequest(BaseModel):
    job_id: str
    cron: str
    enabled: bool


# ── MCP load ───────────────────────────────────────────────────────────────────

@router.post("/load/mcp")
async def load_via_mcp(
    body: McpLoadRequest,
    user: dict = Depends(require_permission("ontology_maintain")),
):
    """Ingest from selected MCP connectors, fully attributed."""
    from src.graph import provenance
    from src.graph import neo4j_client as neo4j

    if not neo4j.is_available():
        raise HTTPException(503, "Neo4j not available")

    sources = body.sources or []
    with provenance.trace_run(
        provenance.PIPELINE_MCP,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"],
        actorId=user.get("userId", ""),
        source="mcp",
        sourceDetail=", ".join(sources) or "synthetic generator",
        connectorIds=sources,
        writtenBy="ontology_loader.load_via_mcp",
        notes=body.notes,
    ) as run:
        try:
            if body.synthetic or not sources:
                # The old path. `run_full_load` reads from connectors/mock_mcp, a
                # random.seed() generator — nothing here ever touched a real MCP server,
                # and `sources` was accepted and then never passed on.
                from src.connectors.ingestion_service import run_full_load
                result = run_full_load(delta_since=body.delta_since)
                extra = {}
            else:
                import asyncio

                from src.mcp_client.ingest import ingest_from_mcp

                # In an executor, NOT inline. This route is `async def`, so there is a
                # running event loop, and ingest_from_mcp drives the async MCP client with
                # asyncio.run() — which raises "cannot be called from a running event
                # loop". A thread is also where the blocking Neo4j writes belong.
                #
                # traced_callable, not a bare lambda: a plain executor hand-off starts
                # with an empty context, so everything the ingest wrote would land
                # unattributed while this run recorded that it had happened.
                uid = user.get("userId", "")
                result = await asyncio.get_event_loop().run_in_executor(
                    None, provenance.traced_callable(ingest_from_mcp, uid, sources))
                extra = {
                    # What the loader could not map, reported rather than swallowed.
                    "skipped": result.get("skipped", 0),
                    "sources": result.get("sources", []),
                    "errors": result.get("errors", []),
                }
                for message in result.get("errors", []):
                    run.fail(message)

            stats = {
                "nodesAdded": result.get("nodes_added", 0),
                "nodesUpdated": result.get("nodes_updated", 0),
                "relsAdded": result.get("rels_added", 0),
                "totalNodes": result.get("total_nodes", 0),
            }
            run.record_stats(**stats)
            return {"versionId": run.runId, "versionNumber": run.versionNumber,
                    **stats, **extra}
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("MCP load failed")
            raise HTTPException(500, str(exc))


# ── API load ───────────────────────────────────────────────────────────────────

@router.post("/load/api")
async def load_via_api(
    body: ApiLoadRequest,
    user: dict = Depends(require_permission("ontology_maintain")),
):
    """Fetch JSON from an external REST API and load into Neo4j."""
    import httpx
    from src.graph import provenance
    from src.graph import neo4j_client as neo4j

    if not neo4j.is_available():
        raise HTTPException(503, "Neo4j not available")

    headers: dict[str, str] = {}
    auth = None
    if body.auth_type == "bearer" and body.token:
        headers["Authorization"] = f"Bearer {body.token}"
    elif body.auth_type == "basic" and body.username:
        auth = (body.username, body.password or "")
    elif body.auth_type == "apikey" and body.api_key_header:
        headers[body.api_key_header] = body.api_key_value or ""

    with provenance.trace_run(
        provenance.PIPELINE_API,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"],
        actorId=user.get("userId", ""),
        source="api_ingest",
        sourceDetail=body.url,
        sourceRecordId=body.url,
        writtenBy="ontology_loader.load_via_api",
        notes=body.notes,
    ) as run:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(body.url, headers=headers, auth=auth)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise HTTPException(400, f"API fetch failed: {exc}")

        try:
            from src.services.file_load_service import load_json_records
            stats = load_json_records(
                data if isinstance(data, list) else data.get("nodes", []), run.runId)
            run.record_stats(**stats)
            return {"versionId": run.runId, "versionNumber": run.versionNumber, **stats}
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("API load failed")
            raise HTTPException(500, str(exc))


# ── File upload load ───────────────────────────────────────────────────────────

@router.post("/load/file")
async def load_via_file(
    file: UploadFile = File(...),
    notes: str = Form(""),
    user: dict = Depends(require_permission("ontology_maintain")),
):
    """Upload a JSON / Excel (.xlsx) / CSV file and load into Neo4j."""
    from src.graph import provenance
    from src.graph import neo4j_client as neo4j

    if not neo4j.is_available():
        raise HTTPException(503, "Neo4j not available")
    if not file.filename:
        raise HTTPException(400, "No filename")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, "File too large — maximum 50 MB")

    with provenance.trace_run(
        provenance.PIPELINE_FILE,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"],
        actorId=user.get("userId", ""),
        source="file_upload",
        sourceDetail=file.filename,
        writtenBy="ontology_loader.load_via_file",
        notes=notes,
        fileInfo={"name": file.filename, "size": len(raw),
                  "type": file.content_type or ""},
    ) as run:
        try:
            from src.services.file_load_service import load_file_bytes
            stats = load_file_bytes(raw, file.filename, run.runId)
            run.record_stats(**stats)
            return {
                "versionId": run.runId,
                "versionNumber": run.versionNumber,
                "filename": file.filename,
                **stats,
            }
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("File load failed")
            raise HTTPException(500, str(exc))


# ── Version records ────────────────────────────────────────────────────────────

@router.get("/versions")
def get_versions(
    limit: int = 50,
    offset: int = 0,
    _: dict = Depends(get_current_user),
):
    from src.services.ontology_version_service import list_versions
    return list_versions(limit=limit, offset=offset)


@router.get("/versions/{version_id}")
def get_version_detail(
    version_id: str,
    _: dict = Depends(get_current_user),
):
    from src.services.ontology_version_service import get_version
    record = get_version(version_id)
    if not record:
        raise HTTPException(404, f"Version {version_id!r} not found")
    return record


@router.get("/nodes/{node_id}/version")
def get_node_version(
    node_id: str,
    _: dict = Depends(get_current_user),
):
    """Return the version record that introduced or last modified this Neo4j node."""
    from src.graph import neo4j_client as neo4j
    node = neo4j.get_node_by_id(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    version_id = node.get("versionId")
    if not version_id:
        return {"versionId": None, "versionNumber": None, "loadMethod": "unknown", "note": "pre-versioning node"}
    from src.services.ontology_version_service import get_version
    return get_version(version_id) or {"versionId": version_id}


# ── Scheduler status + config ──────────────────────────────────────────────────

@router.get("/schedule/status")
def get_schedule_status(
    _: dict = Depends(require_permission("ontology_maintain")),
):
    from src.scheduler.jobs import get_job_list, get_job_history
    jobs = get_job_list()
    return {
        "jobs": [
            {**j, "history": get_job_history(j["id"])[:5]}
            for j in jobs
        ]
    }


@router.post("/schedule")
def update_schedule(
    body: ScheduleConfigRequest,
    _: dict = Depends(require_permission("ontology_maintain")),
):
    """Persist cron expression and enabled flag.  Actual reschedule requires server restart."""
    from src.services.ontology_version_service import save_scheduler_config
    save_scheduler_config(body.job_id, body.cron, body.enabled)
    return {"ok": True, "jobId": body.job_id, "cron": body.cron, "enabled": body.enabled}


@router.post("/schedule/run-now")
def run_job_now(
    job_id: str,
    _: dict = Depends(require_permission("ontology_maintain")),
):
    """Trigger a scheduled job immediately in a background thread."""
    from src.scheduler.jobs import JOB_DEFS
    job_map = {j["id"]: j for j in JOB_DEFS}
    if job_id not in job_map:
        raise HTTPException(404, f"Job {job_id!r} not found")
    import threading
    t = threading.Thread(target=job_map[job_id]["fn"], daemon=True)
    t.start()
    return {"ok": True, "jobId": job_id, "message": "Job started in background"}

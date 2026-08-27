"""Scheduler management API endpoints."""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException

from src.routers.auth import get_current_user, require_permission
from src.scheduler.jobs import get_job_list, get_job_history, JOB_DEFS, _job_status

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/jobs")
def list_jobs(_: dict = Depends(get_current_user)):
    return get_job_list()


@router.post("/jobs/{job_id}/run")
def trigger_job(job_id: str, user: dict = Depends(require_permission("ontology_maintain"))):
    job_def = next((j for j in JOB_DEFS if j["id"] == job_id), None)
    if not job_def:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if _job_status.get(job_id) == "running":
        raise HTTPException(status_code=409, detail=f"Job {job_id!r} is already running")
    # Run in background thread so the HTTP response returns immediately
    t = threading.Thread(target=job_def["fn"], daemon=True, name=f"job-{job_id}")
    t.start()
    return {"ok": True, "job_id": job_id, "status": "triggered", "triggered_by": user["username"]}


@router.get("/jobs/{job_id}/history")
def job_history(job_id: str, _: dict = Depends(get_current_user)):
    if not any(j["id"] == job_id for j in JOB_DEFS):
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return get_job_history(job_id)

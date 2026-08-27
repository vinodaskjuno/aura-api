"""APScheduler job definitions for the ontology universe.

Jobs:
  ontology_delta_job      — daily 02:00 UTC: delta ingestion from all connectors
  correlation_refresh_job — daily 03:00 UTC: re-run correlation on recent changes

Each job stores its run state in Neo4j under a SchedulerState node.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# In-memory job history (last 10 runs per job) — falls back when Neo4j is unavailable
_job_history: dict[str, list[dict]] = {
    "ontology_delta_job": [],
    "correlation_refresh_job": [],
}
_job_status: dict[str, str] = {
    "ontology_delta_job": "idle",
    "correlation_refresh_job": "idle",
}
_job_last_run: dict[str, str | None] = {
    "ontology_delta_job": None,
    "correlation_refresh_job": None,
}


def _record_run(job_id: str, result: dict, duration_s: float):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(duration_s, 2),
        "status": "error" if result.get("error") else "success",
        "result": result,
    }
    history = _job_history.setdefault(job_id, [])
    history.insert(0, entry)
    if len(history) > 10:
        history.pop()
    _job_last_run[job_id] = entry["timestamp"]
    _job_status[job_id] = "idle"
    # Persist to DynamoDB so lastRun survives server restarts
    try:
        from src.services.ontology_version_service import save_scheduler_state
        save_scheduler_state(job_id, {
            "lastRun": entry["timestamp"],
            "lastStatus": entry["status"],
            "lastDuration": duration_s,
        })
    except Exception as exc:
        log.debug("Scheduler DynamoDB persist skipped: %s", exc)


def _load_last_run(job_id: str) -> str | None:
    """Restore lastRun from DynamoDB on startup."""
    try:
        from src.services.ontology_version_service import load_scheduler_state
        state = load_scheduler_state(job_id)
        return state.get("lastRun") if state else None
    except Exception:
        return None


def ontology_delta_job():
    """Run delta ingestion — ingest only items changed since last run."""
    import time
    log.info("[Scheduler] ontology_delta_job starting")
    _job_status["ontology_delta_job"] = "running"
    start = time.monotonic()
    last_run = _job_last_run.get("ontology_delta_job")
    try:
        from src.connectors.ingestion_service import run_full_load
        result = run_full_load(delta_since=last_run)
    except Exception as exc:
        log.exception("[Scheduler] ontology_delta_job failed")
        result = {"error": str(exc)}
    duration = time.monotonic() - start
    _record_run("ontology_delta_job", result, duration)
    log.info("[Scheduler] ontology_delta_job done in %.1fs", duration)


def correlation_refresh_job():
    """Re-run correlation engine on recently modified nodes."""
    import time
    log.info("[Scheduler] correlation_refresh_job starting")
    _job_status["correlation_refresh_job"] = "running"
    start = time.monotonic()
    try:
        from src.connectors.correlation_engine import run_correlation
        result = run_correlation()
    except Exception as exc:
        log.exception("[Scheduler] correlation_refresh_job failed")
        result = {"error": str(exc)}
    duration = time.monotonic() - start
    _record_run("correlation_refresh_job", result, duration)
    log.info("[Scheduler] correlation_refresh_job done in %.1fs", duration)


# ── Job metadata (for the scheduler API) ─────────────────────────────────────

JOB_DEFS = [
    {
        "id": "ontology_delta_job",
        "name": "Ontology Delta Ingestion",
        "description": "Ingests new/updated entities from all MCP connectors since last run",
        "schedule": "0 2 * * *",
        "schedule_human": "Daily at 02:00 UTC",
        "fn": ontology_delta_job,
    },
    {
        "id": "correlation_refresh_job",
        "name": "Correlation Engine Refresh",
        "description": "Re-runs the two-pass correlation engine on recently modified nodes",
        "schedule": "0 3 * * *",
        "schedule_human": "Daily at 03:00 UTC",
        "fn": correlation_refresh_job,
    },
]


def get_job_list() -> list[dict]:
    result = []
    for j in JOB_DEFS:
        result.append({
            "id": j["id"],
            "name": j["name"],
            "description": j["description"],
            "schedule": j["schedule"],
            "schedule_human": j["schedule_human"],
            "status": _job_status.get(j["id"], "idle"),
            "last_run": _job_last_run.get(j["id"]),
            "next_run": _compute_next_run(j["schedule"]),
        })
    return result


def get_job_history(job_id: str) -> list[dict]:
    return _job_history.get(job_id, [])


def _compute_next_run(cron: str) -> str | None:
    """Very simple next-run estimate — returns tomorrow's scheduled time."""
    try:
        parts = cron.split()
        minute, hour = int(parts[0]), int(parts[1])
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            from datetime import timedelta
            next_run = next_run + timedelta(days=1)
        return next_run.isoformat()
    except Exception:
        return None


def setup_scheduler(app=None):
    """Wire APScheduler into the FastAPI lifespan.  Non-fatal if APScheduler not installed."""
    # Restore lastRun from DynamoDB so delta_since works across restarts
    for job in JOB_DEFS:
        persisted = _load_last_run(job["id"])
        if persisted and _job_last_run.get(job["id"]) is None:
            _job_last_run[job["id"]] = persisted
            log.info("Scheduler: restored lastRun for %s from DynamoDB: %s", job["id"], persisted)

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone="UTC")
        for job in JOB_DEFS:
            parts = job["schedule"].split()
            scheduler.add_job(
                job["fn"],
                "cron",
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                id=job["id"],
                replace_existing=True,
                misfire_grace_time=3600,
            )
        # Gateway provider health probe — every 60 seconds
        try:
            from src.services.provider_health import schedule_health_probes
            schedule_health_probes(scheduler)
        except Exception as exc:
            log.warning("Gateway health probe scheduling skipped: %s", exc)

        scheduler.start()
        log.info("APScheduler started with %d jobs", len(JOB_DEFS))
        return scheduler
    except ImportError:
        log.warning("APScheduler not installed — scheduled jobs disabled. Run: pip install apscheduler")
        return None
    except Exception as exc:
        log.warning("APScheduler setup failed: %s", exc)
        return None

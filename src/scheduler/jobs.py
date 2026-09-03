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
    "online_eval_job": [],
}
_job_status: dict[str, str] = {
    "ontology_delta_job": "idle",
    "correlation_refresh_job": "idle",
    "online_eval_job": "idle",
}
_job_last_run: dict[str, str | None] = {
    "ontology_delta_job": None,
    "correlation_refresh_job": None,
    "online_eval_job": None,
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
        from src.graph import provenance
        # The actor is the job, not the last person who happened to log in. A
        # scheduled write attributed to a user is worse than one attributed to
        # nobody — it is a plausible-looking lie.
        with provenance.trace_run(
            provenance.PIPELINE_MCP,
            trigger=provenance.TRIGGER_SCHEDULED,
            actor="scheduler:ontology_delta_job",
            source="scheduler",
            sourceDetail=f"delta since {last_run or 'never'}",
            writtenBy="scheduler.ontology_delta_job",
            notes="Nightly ontology delta ingestion",
        ):
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
        from src.graph import provenance
        with provenance.trace_run(
            provenance.PIPELINE_CORRELATION,
            trigger=provenance.TRIGGER_SCHEDULED,
            actor="scheduler:correlation_refresh_job",
            source="correlation",
            sourceDetail="scheduled correlation refresh",
            writtenBy="scheduler.correlation_refresh_job",
        ):
            result = run_correlation()
    except Exception as exc:
        log.exception("[Scheduler] correlation_refresh_job failed")
        result = {"error": str(exc)}
    duration = time.monotonic() - start
    _record_run("correlation_refresh_job", result, duration)
    log.info("[Scheduler] correlation_refresh_job done in %.1fs", duration)


def online_eval_job():
    """Score a sample of recent LLM traces with the configured judges.

    A no-op unless online evaluation has been switched on, and bounded per sweep,
    because every judged trace is a billable model call. Sampling is deterministic
    by trace id, so a sweep that reruns does not re-score what it already paid for.
    """
    import time
    log.info("[Scheduler] online_eval_job starting")
    _job_status["online_eval_job"] = "running"
    start = time.monotonic()
    try:
        from src.aiobs import online_eval
        result = online_eval.run_sweep()
    except Exception as exc:
        log.exception("[Scheduler] online_eval_job failed")
        result = {"error": str(exc)}
    duration = time.monotonic() - start
    _record_run("online_eval_job", result, duration)
    log.info("[Scheduler] online_eval_job done in %.1fs: %s", duration, result.get("status"))


def qa_reaper_job():
    """Mark QA runs whose self-hosted runner stopped reporting as `abandoned`.

    Without this the Results tab lies: a laptop that sleeps mid-run leaves the row at
    `running` for ever, while the S3 prefix it left behind has no `report.json` and is
    therefore invisible to every reader. Enabled by default because it only ever
    corrects state that is already wrong — unlike the graph-loading jobs, it writes no
    content.
    """
    start = time.monotonic()
    try:
        from src.config_settings import get_settings
        from src.qatest import queue
        reaped = queue.reap(getattr(get_settings(), "qa_run_stale_after_s", 900))
        result = {"status": "ok", "reaped": reaped}
    except Exception as exc:                                  # noqa: BLE001
        result = {"error": str(exc)}
    duration = time.monotonic() - start
    _record_run("qa_reaper_job", result, duration)
    if result.get("reaped"):
        log.info("[Scheduler] qa_reaper_job marked %s run(s) abandoned",
                 result["reaped"])


# ── Job metadata (for the scheduler API) ─────────────────────────────────────

JOB_DEFS = [
    {
        "id": "ontology_delta_job",
        "name": "Ontology Delta Ingestion",
        "description": ("Loads entities into the knowledge graph. DISABLED by default: "
                        "its data source is the synthetic generator in "
                        "connectors/mock_mcp, so leaving it on quietly refills an "
                        "emptied graph with ~700 fake nodes overnight. Enable it "
                        "deliberately from Data Loader, or load from a real MCP "
                        "connector instead."),
        "schedule": "0 2 * * *",
        "schedule_human": "Daily at 02:00 UTC",
        "default_enabled": False,
        "fn": ontology_delta_job,
    },
    {
        "id": "correlation_refresh_job",
        "name": "Correlation Engine Refresh",
        "description": ("Re-links recently modified nodes. DISABLED by default for the "
                        "same reason as ontology_delta_job — it exists to re-correlate "
                        "what that job writes, so on its own it has nothing to do."),
        "schedule": "0 3 * * *",
        "schedule_human": "Daily at 03:00 UTC",
        "default_enabled": False,
        "fn": correlation_refresh_job,
    },
    {
        "id": "qa_reaper_job",
        "name": "QA Run Reaper",
        "description": ("Marks QualityMind runs whose self-hosted runner stopped "
                        "reporting as abandoned, so the Results tab stops showing a "
                        "run that will never finish. Corrects state only; writes no "
                        "content."),
        "schedule": "*/5 * * * *",
        "schedule_human": "Every 5 minutes",
        "fn": qa_reaper_job,
    },
    {
        "id": "online_eval_job",
        "name": "Online LLM Evaluation",
        "description": ("Scores a sample of recent LLM traces with the configured "
                        "judges. Disabled by default; each judged trace is a "
                        "billable model call."),
        # Hourly rather than daily: online evaluation exists to catch quality
        # regressions while they are still happening, and a daily sweep would
        # surface one up to a day late.
        "schedule": "17 * * * *",
        "schedule_human": "Hourly at :17",
        "fn": online_eval_job,
    },
]


def get_job_list() -> list[dict]:
    result = []
    for j in JOB_DEFS:
        # `next_run` is computed from the cron string, not read from APScheduler, so it
        # would happily advertise "tomorrow at 02:00" for a job that is not scheduled
        # at all. That is the same lie the enabled flag itself used to tell, and it
        # matters most for exactly the jobs now disabled by default: the screen would
        # promise a graph load that will never happen.
        enabled = job_is_enabled(j)
        result.append({
            "id": j["id"],
            "name": j["name"],
            "description": j["description"],
            "schedule": j["schedule"],
            "schedule_human": j["schedule_human"] if enabled else "Disabled",
            "enabled": enabled,
            "status": _job_status.get(j["id"], "idle"),
            "last_run": _job_last_run.get(j["id"]),
            "next_run": _compute_next_run(j["schedule"]) if enabled else None,
        })
    return result


def get_job_history(job_id: str) -> list[dict]:
    return _job_history.get(job_id, [])


def _compute_next_run(cron: str) -> str | None:
    """Next-run estimate for the shapes this file actually uses.

    Handles `M H * * *` (daily) and `*/N * * * *` (every N minutes). The second case
    used to fall into the except and return None, which meant a job that really was
    scheduled reported no next run — the mirror image of the bug where a DISABLED job
    advertised one.
    """
    from datetime import timedelta

    try:
        parts = cron.split()
        now = datetime.now(timezone.utc)

        if parts[0].startswith("*/"):
            step = int(parts[0][2:])
            if step <= 0:
                return None
            minute = ((now.minute // step) + 1) * step
            base = now.replace(second=0, microsecond=0)
            return (base.replace(minute=0) + timedelta(minutes=minute)).isoformat()

        minute = int(parts[0])

        if parts[1] == "*":
            # Hourly at a fixed minute, e.g. online_eval_job's "17 * * * *". This also
            # used to return None, so a job running every hour claimed no next run.
            next_run = now.replace(minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(hours=1)
            return next_run.isoformat()

        next_run = now.replace(hour=int(parts[1]), minute=minute,
                               second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run + timedelta(days=1)
        return next_run.isoformat()
    except Exception:
        return None


def job_is_enabled(job: dict) -> bool:
    """Whether a job should actually be scheduled.

    This function is why `POST /api/ontology/schedule` now means something. It persisted
    `{cron, enabled}` via save_scheduler_config, but load_scheduler_config had ZERO
    callers — setup_scheduler read only `lastRun`. So turning a job off in the UI worked
    until the next task restart, and then silently stopped working.

    Combined with `default_enabled`, this is also what keeps a deployed backend from
    seeding the knowledge graph: the two graph-writing jobs default to off, and nothing
    schedules a graph write unless someone turned it on deliberately.
    """
    default = bool(job.get("default_enabled", True))
    try:
        from src.services.ontology_version_service import load_scheduler_config
        config = load_scheduler_config(job["id"]) or {}
    except Exception as exc:                              # noqa: BLE001
        log.warning("Scheduler: could not read config for %s (%s); using default %s",
                    job["id"], exc, default)
        return default
    if "enabled" not in config:
        return default
    return bool(config.get("enabled"))


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
            if not job_is_enabled(job):
                log.info("Scheduler: %s is disabled — not scheduled", job["id"])
                continue
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

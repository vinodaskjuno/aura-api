"""Ontology versioning service — create and query OntologyVersion records.

DynamoDB table: aura-ontology-versions
  PK: versionId (uuid)
  GSI: versionNumber-index (versionNumber) for sorted listing
  GSI: actor-timestamp-index (actor, startedAt)

Table: aura-scheduler-state
  PK: jobId (string)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_VERSION_TABLE = "ontology-versions"
_SCHEDULER_TABLE = "scheduler-state"
# Constant PK for the feed GSI — every run row carries it so "newest first"
# is a query against one partition instead of a scan.
_FEED = "all"


# ── Version record helpers ───────────────────────────────────────────────────

def _next_version_number() -> str:
    """A human-friendly label like "v1.42" for the run.

    Cosmetic only. `runId` is the identity everything else joins on, because this
    read-then-increment races: two runs starting together get the same label. That
    is tolerable for a caption and unacceptable for a key, which is why nothing
    references a run by its number.
    """
    from src.database.dynamo_client import query_items, scan_items
    try:
        try:
            items = query_items(
                _VERSION_TABLE, pk_name="feed", pk_value=_FEED,
                index_name="feed-startedAt-index", limit=50,
            )
        except Exception:
            items = scan_items(_VERSION_TABLE, limit=200)
        minors = []
        for item in items:
            try:
                minors.append(int(str(item.get("versionNumber", "v0.0.0")).lstrip("v").split(".")[1]))
            except (ValueError, IndexError):
                pass
        return f"v1.{max(minors) + 1}.0" if minors else "v1.0.0"
    except Exception:
        return "v1.0.0"


def create_version_record(
    load_method: str,
    actor: str,
    sources: list[str] | None = None,
    notes: str = "",
    file_info: dict | None = None,
    *,
    run_id: str | None = None,
    trigger: str = "unknown",
    pipeline: str = "",
    source_detail: str = "",
    project_id: str = "",
    written_by: str = "",
    parent_run_id: str = "",
) -> dict:
    """Create a new run record and return it.  Status defaults to 'in_progress'.

    `run_id` lets the caller supply the id it has already stamped onto the nodes,
    so the record and the graph agree. `graph/provenance.py` always passes one;
    without it the record would describe a run nothing points at, which is the bug
    the MCP and API load routes shipped with.
    """
    from src.database.dynamo_client import put_item
    version_id = run_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "versionId": version_id,
        "versionNumber": _next_version_number(),
        "loadMethod": load_method,
        "sources": sources or [],
        "actor": actor,
        "startedAt": now,
        "finishedAt": None,
        "status": "in_progress",
        # Constant partition key for the "all runs, newest first" feed. Without it
        # listing means a full scan, which `list_versions` did — silently capped at
        # 200 rows, so the history simply stopped being complete once we passed it.
        "feed": _FEED,
        "trigger": trigger,
        "pipeline": pipeline or load_method,
        "sourceDetail": source_detail,
        "projectId": project_id,
        "writtenBy": written_by,
        "parentRunId": parent_run_id,
        "durationMs": None,
        "errors": [],
        "stats": {
            "nodesAdded": 0,
            "nodesUpdated": 0,
            "relsAdded": 0,
            "totalNodes": 0,
        },
        "diffSummary": {},
        "notes": notes,
        "fileInfo": file_info or {},
    }
    try:
        put_item(_VERSION_TABLE, record)
    except Exception as exc:
        log.warning("create_version_record: DynamoDB write failed: %s", exc)
    return record


def finish_version_record(
    version_id: str,
    status: str,
    stats: dict[str, int],
    diff_summary: dict[str, int] | None = None,
    *,
    duration_ms: int | None = None,
    errors: list[str] | None = None,
) -> None:
    """Update a run record with final stats and mark it finished.

    `errors` are recorded rather than dropped. Ingestion deliberately survives a bad
    record, so a run that skipped half its input still reports `success` — and until
    the errors are stored alongside it, nothing downstream can tell the difference.
    """
    from src.database.dynamo_client import update_item
    now = datetime.now(timezone.utc).isoformat()
    changes: dict[str, Any] = {
        "status": status,
        "finishedAt": now,
        "stats": stats,
        "diffSummary": diff_summary or {},
    }
    if duration_ms is not None:
        changes["durationMs"] = duration_ms
    if errors:
        changes["errors"] = errors[:100]
    try:
        update_item(_VERSION_TABLE, {"versionId": version_id}, changes)
    except Exception as exc:
        log.warning("finish_version_record: DynamoDB update failed: %s", exc)


def list_versions(
    limit: int = 50,
    offset: int = 0,
    *,
    pipeline: str | None = None,
    trigger: str | None = None,
    actor: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Runs newest-first, optionally filtered.

    Queries the feed GSI rather than scanning. The scan this replaces took the first
    200 rows the table happened to return and sorted those, so once the ledger passed
    200 runs the "history" silently became an arbitrary subset — which is precisely
    the failure a provenance feature cannot ship with.

    Falls back to the scan when the GSI is missing, so a deployment that has not yet
    picked up the new index keeps working instead of returning nothing.
    """
    from src.database.dynamo_client import query_items, scan_items
    want = limit + offset
    try:
        items = query_items(
            _VERSION_TABLE, pk_name="feed", pk_value=_FEED,
            index_name="feed-startedAt-index",
            limit=max(want * 3, 100),
        )
    except Exception as exc:
        log.warning("list_versions: feed index unavailable (%s); falling back to scan", exc)
        try:
            items = scan_items(_VERSION_TABLE, limit=500)
        except Exception as inner:
            log.warning("list_versions failed: %s", inner)
            return []

    if pipeline:
        items = [i for i in items if (i.get("pipeline") or i.get("loadMethod")) == pipeline]
    if trigger:
        items = [i for i in items if i.get("trigger") == trigger]
    if actor:
        items = [i for i in items if i.get("actor") == actor]
    if status:
        items = [i for i in items if i.get("status") == status]

    items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
    return items[offset: offset + limit]


def get_version(version_id: str) -> dict | None:
    """Fetch a single version record by PK."""
    try:
        from src.database.dynamo_client import get_item
        return get_item(_VERSION_TABLE, {"versionId": version_id})
    except Exception as exc:
        log.warning("get_version failed: %s", exc)
        return None


# ── Scheduler state persistence ──────────────────────────────────────────────

def save_scheduler_state(job_id: str, state: dict) -> None:
    """Persist scheduler run state to DynamoDB so it survives restarts."""
    try:
        from src.database.dynamo_client import put_item
        record = {"jobId": job_id, **state, "updatedAt": datetime.now(timezone.utc).isoformat()}
        put_item(_SCHEDULER_TABLE, record)
    except Exception as exc:
        log.warning("save_scheduler_state [%s]: %s", job_id, exc)


def load_scheduler_state(job_id: str) -> dict | None:
    """Load persisted scheduler state from DynamoDB."""
    try:
        from src.database.dynamo_client import get_item
        return get_item(_SCHEDULER_TABLE, {"jobId": job_id})
    except Exception as exc:
        log.warning("load_scheduler_state [%s]: %s", job_id, exc)
        return None


def save_scheduler_config(job_id: str, cron: str, enabled: bool) -> None:
    """Persist cron expression and enabled flag for a job."""
    try:
        from src.database.dynamo_client import put_item
        put_item(_SCHEDULER_TABLE, {
            "jobId": f"{job_id}:config",
            "cronExpression": cron,
            "enabled": enabled,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        log.warning("save_scheduler_config [%s]: %s", job_id, exc)


def load_scheduler_config(job_id: str) -> dict | None:
    """Load persisted scheduler config from DynamoDB."""
    try:
        from src.database.dynamo_client import get_item
        return get_item(_SCHEDULER_TABLE, {"jobId": f"{job_id}:config"})
    except Exception as exc:
        log.warning("load_scheduler_config [%s]: %s", job_id, exc)
        return None

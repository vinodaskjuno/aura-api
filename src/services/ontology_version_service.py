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


# ── Version record helpers ───────────────────────────────────────────────────

def _next_version_number() -> str:
    """Increment the minor version based on the latest record."""
    try:
        from src.database.dynamo_client import scan_items
        items = scan_items(_VERSION_TABLE, limit=200)
        if not items:
            return "v1.0.0"
        nums = []
        for item in items:
            vn = item.get("versionNumber", "v0.0.0")
            try:
                parts = vn.lstrip("v").split(".")
                nums.append(int(parts[1]))
            except Exception:
                pass
        minor = (max(nums) + 1) if nums else 0
        return f"v1.{minor}.0"
    except Exception:
        return f"v1.0.0"


def create_version_record(
    load_method: str,
    actor: str,
    sources: list[str] | None = None,
    notes: str = "",
    file_info: dict | None = None,
) -> dict:
    """Create a new OntologyVersion record and return it.  Status defaults to 'in_progress'."""
    from src.database.dynamo_client import put_item
    version_id = str(uuid.uuid4())
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
) -> None:
    """Update a version record with final stats and mark it finished."""
    from src.database.dynamo_client import update_item
    now = datetime.now(timezone.utc).isoformat()
    try:
        update_item(
            _VERSION_TABLE,
            {"versionId": version_id},
            {
                "status": status,
                "finishedAt": now,
                "stats": stats,
                "diffSummary": diff_summary or {},
            },
        )
    except Exception as exc:
        log.warning("finish_version_record: DynamoDB update failed: %s", exc)


def list_versions(limit: int = 50, offset: int = 0) -> list[dict]:
    """Return versions ordered newest-first."""
    try:
        from src.database.dynamo_client import scan_items
        items = scan_items(_VERSION_TABLE, limit=200)
        items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
        return items[offset: offset + limit]
    except Exception as exc:
        log.warning("list_versions failed: %s", exc)
        return []


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

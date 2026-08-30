"""Evaluation datasets — versioned sets of {input, expected} items.

Seedable directly from captured production traces, which is the point: the most
useful eval set is the one built from requests that actually went wrong, and
retyping those by hand is why eval sets go stale.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

TABLE = "ai-datasets"
# One row per dataset holds its metadata; item rows sit under the same partition.
META = "__meta__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(name: str, project_id: str, actor: str, description: str = "") -> dict:
    from src.database import dynamo_client as db
    dataset_id = f"ds-{uuid.uuid4().hex[:12]}"
    meta = {
        "datasetId": dataset_id, "itemId": META, "name": name,
        "projectId": project_id, "description": description,
        "createdAt": _now(), "createdBy": actor, "itemCount": 0,
    }
    db.put_item(TABLE, meta)
    return meta


def add_item(dataset_id: str, input_text: str, expected: str = "",
             metadata: dict | None = None) -> dict:
    from src.database import dynamo_client as db
    item = {
        "datasetId": dataset_id,
        # Time-ordered ids keep insertion order stable without a sort field.
        "itemId": f"it-{_now()}-{uuid.uuid4().hex[:6]}",
        "input": (input_text or "")[:100_000],
        "expected": (expected or "")[:100_000],
        "metadata": metadata or {},
        "createdAt": _now(),
    }
    db.put_item(TABLE, item)
    _bump_count(dataset_id, 1)
    return item


def _bump_count(dataset_id: str, delta: int) -> None:
    """Denormalised so a list view never has to count items per dataset."""
    from src.database import dynamo_client as db
    try:
        meta = db.get_item(TABLE, {"datasetId": dataset_id, "itemId": META}) or {}
        db.update_item(TABLE, {"datasetId": dataset_id, "itemId": META},
                       {"itemCount": max(0, int(meta.get("itemCount") or 0) + delta),
                        "updatedAt": _now()})
    except Exception as exc:  # noqa: BLE001 — a stale count must not fail the write
        log.debug("itemCount bump failed for %s: %s", dataset_id, exc)


def get(dataset_id: str) -> dict | None:
    from src.database import dynamo_client as db
    return db.get_item(TABLE, {"datasetId": dataset_id, "itemId": META})


def items(dataset_id: str, limit: int = 1000) -> list[dict]:
    from src.database import dynamo_client as db
    rows = db.query_items(TABLE, "datasetId", dataset_id, limit=limit) or []
    return sorted((r for r in rows if r.get("itemId") != META),
                  key=lambda r: r.get("itemId", ""))


def list_datasets(project_id: str = "", limit: int = 100) -> list[dict]:
    from src.database import dynamo_client as db
    rows = db.scan_items(TABLE, limit=limit * 20) or []
    metas = [r for r in rows if r.get("itemId") == META]
    if project_id:
        metas = [r for r in metas if r.get("projectId") == project_id]
    return sorted(metas, key=lambda r: r.get("createdAt", ""), reverse=True)[:limit]


def delete_item(dataset_id: str, item_id: str) -> bool:
    from src.database import dynamo_client as db
    try:
        db.delete_item(TABLE, {"datasetId": dataset_id, "itemId": item_id})
        _bump_count(dataset_id, -1)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_item %s/%s: %s", dataset_id, item_id, exc)
        return False


def seed_from_traces(dataset_id: str, project_id: str, trace_ids: list[str]) -> int:
    """Build eval items from real captured traces.

    The trace's own output becomes `expected` — a regression baseline, not a
    ground truth. It says "this is what we do today"; a human still has to correct
    the ones that were wrong, which is exactly the workflow annotation is for.
    """
    from src.aiobs import service
    store = service.get_store()
    added = 0
    for trace_id in trace_ids:
        trace = store.get_trace(project_id, trace_id)
        if not trace:
            continue
        add_item(
            dataset_id,
            input_text=trace.get("inputPreview") or "",
            expected=trace.get("outputPreview") or "",
            metadata={"sourceTraceId": trace_id,
                      "seededFrom": "trace",
                      "originalCostUsd": float(trace.get("costUsd") or 0)},
        )
        added += 1
    return added

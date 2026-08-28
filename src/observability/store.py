"""Persistence for investigations and evidence.

Split by DynamoDB's 400KB item cap: 500 log records is roughly 2MB, so payloads go
to S3 and only the light index rows go to DynamoDB.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.database.dynamo_client import (
    put_item, query_items, query_range, scan_items, update_item,
)
from src.observability.types import EvidenceRecord

log = logging.getLogger(__name__)

_INVESTIGATIONS = "observability-investigations"
_EVIDENCE = "observability-evidence"
_OUTCOMES = "observability-outcomes"
_CASES = "observability-cases"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Investigations ───────────────────────────────────────────────────────────

def create_investigation(record: dict) -> dict:
    record.setdefault("createdAt", now())
    put_item(_INVESTIGATIONS, record)
    return record


def get_investigation(investigation_id: str) -> dict | None:
    rows = query_items(_INVESTIGATIONS, "investigationId", investigation_id, limit=1)
    return rows[0] if rows else None


def update_investigation(investigation_id: str, updates: dict) -> None:
    rec = get_investigation(investigation_id)
    if not rec:
        log.warning("update_investigation: %s not found", investigation_id)
        return
    update_item(_INVESTIGATIONS,
                {"investigationId": investigation_id, "createdAt": rec["createdAt"]},
                updates)


def list_investigations(project_id: str | None = None, user_id: str | None = None,
                        limit: int = 50) -> list[dict]:
    try:
        if project_id:
            rows = query_items(_INVESTIGATIONS, "projectId", project_id,
                               index_name="projectId-createdAt-index", limit=limit)
        else:
            rows = scan_items(_INVESTIGATIONS, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("list_investigations failed: %s", exc)
        return []
    if user_id:
        rows = [r for r in rows if not r.get("userId") or r.get("userId") == user_id]
    return sorted(rows, key=lambda r: r.get("createdAt", ""), reverse=True)[:limit]


# ── Evidence ─────────────────────────────────────────────────────────────────

def save_evidence_bundle(investigation_id: str, project_id: str,
                         evidence: list[EvidenceRecord]) -> dict:
    """Full (UNMASKED) payloads to S3; light index rows to DynamoDB.

    The bundle is unmasked on purpose — it is our own bucket and the deep links have
    to work. Masking is for external LLM egress, full stop.
    """
    ref: dict[str, str] = {}
    try:
        from src.storage.s3_client import put_json
        key = f"observability/{project_id or 'global'}/{investigation_id}/evidence.json"
        uri = put_json("exports", key, [e.to_dict() for e in evidence])
        ref = {"bucket": "exports", "key": key, "uri": uri}
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not write evidence bundle to S3: %s", exc)

    for e in evidence:
        try:
            put_item(_EVIDENCE, {**e.index_row(), "createdAt": now()})
        except Exception as exc:  # noqa: BLE001
            log.debug("evidence index row failed for %s: %s", e.evidence_id, exc)
    return ref


def list_evidence(investigation_id: str, limit: int = 500) -> list[dict]:
    return query_items(_EVIDENCE, "investigationId", investigation_id, limit=limit)


def get_evidence_detail(investigation_id: str, evidence_id: str,
                        project_id: str = "") -> dict | None:
    """Full payload, read from the S3 bundle — this is the drawer's lazy fetch."""
    rec = get_investigation(investigation_id)
    ref = (rec or {}).get("evidenceRef") or {}
    key = ref.get("key") or \
        f"observability/{project_id or 'global'}/{investigation_id}/evidence.json"
    try:
        from src.storage.s3_client import get_json
        bundle = get_json(ref.get("bucket", "exports"), key) or []
        for item in bundle:
            if item.get("evidence_id") == evidence_id:
                return item
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read evidence bundle: %s", exc)
    for row in list_evidence(investigation_id):
        if row.get("evidenceId") == evidence_id:
            return row
    return None


# ── Outcomes ─────────────────────────────────────────────────────────────────

def save_outcome(outcome: dict) -> None:
    outcome.setdefault("recordedAt", now())
    put_item(_OUTCOMES, outcome)
    update_investigation(outcome["investigationId"], {"outcome": outcome})


def get_outcome(investigation_id: str) -> dict | None:
    rows = query_range(_OUTCOMES, "investigationId", investigation_id, "recordedAt",
                       limit=50)
    return sorted(rows, key=lambda r: r.get("recordedAt", ""))[-1] if rows else None


# ── Cases (learning corpus) ──────────────────────────────────────────────────

def save_case(case: dict) -> None:
    case.setdefault("createdAt", now())
    put_item(_CASES, case)


def list_cases(service: str = "", limit: int = 200) -> list[dict]:
    try:
        if service:
            return query_items(_CASES, "serviceName", service,
                               index_name="serviceName-createdAt-index", limit=limit)
        return scan_items(_CASES, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("list_cases failed: %s", exc)
        return []


def delete_case(case_id: str) -> bool:
    from src.database.dynamo_client import delete_item
    rows = query_items(_CASES, "caseId", case_id, limit=1)
    if not rows:
        return False
    delete_item(_CASES, {"caseId": case_id, "createdAt": rows[0]["createdAt"]})
    return True


def corpus_size(service: str = "") -> int:
    return len(list_cases(service, limit=500))

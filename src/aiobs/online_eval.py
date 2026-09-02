"""Online evaluation — score a sample of production traces.

Offline experiments tell you how a change performs against a fixed dataset.
Online evaluation tells you how it is performing right now, on real traffic that
no dataset anticipated.

Sampling is the whole design. Judging every trace would cost roughly as much as
serving it, so a percentage is scored and the result is an estimate — reported as
one, with its sample size, rather than presented as an exact rate.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from src.aiobs import judges, metrics

log = logging.getLogger(__name__)

CONFIG_TABLE = "graph-config"     # reuses the runtime-config table pattern
_CONFIG_ID = "aiobs-online-eval"

DEFAULT_SAMPLE_RATE = 0.05        # 5%
MAX_PER_RUN = 100                 # hard ceiling on judge calls per sweep


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_config() -> dict:
    from src.database import dynamo_client as db
    try:
        row = db.get_item(CONFIG_TABLE, {"configId": _CONFIG_ID}) or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("online eval config read failed: %s", exc)
        row = {}
    return {
        "enabled": bool(row.get("enabled", False)),
        "sampleRate": float(row.get("sampleRate", DEFAULT_SAMPLE_RATE)),
        "judges": row.get("judges") or ["relevance"],
        "projectId": row.get("projectId", ""),
        "updatedAt": row.get("updatedAt", ""),
        "updatedBy": row.get("updatedBy", ""),
    }


def set_config(enabled: bool, sample_rate: float, judge_names: list[str],
               project_id: str, actor: str) -> dict:
    from src.database import dynamo_client as db
    db.put_item(CONFIG_TABLE, {
        "configId": _CONFIG_ID,
        "enabled": enabled,
        # Clamped: a rate above 1.0 is meaningless and 100% sampling on a busy
        # project is an unbounded bill.
        "sampleRate": max(0.0, min(1.0, float(sample_rate))),
        "judges": judge_names[:5],
        "projectId": project_id,
        "updatedAt": _now(),
        "updatedBy": actor,
    })
    return get_config()


def should_sample(trace_id: str, rate: float) -> bool:
    """Deterministic sampling by trace id.

    Hashing rather than random() means the same trace is always either in or out
    of the sample, so a sweep that reruns does not score some traces twice and
    others never.
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = hashlib.sha256((trace_id or "").encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return bucket < rate


def run_sweep(limit: int = MAX_PER_RUN) -> dict:
    """Score a sample of recent traces. Safe to call repeatedly from the scheduler."""
    config = get_config()
    if not config["enabled"]:
        return {"status": "disabled", "scored": 0}

    from src.aiobs import service
    store = service.get_store()
    project = config["projectId"]
    if not project:
        return {"status": "no-project", "scored": 0}

    candidates = store.list_traces(project, limit=min(limit, MAX_PER_RUN) * 10)
    sampled = [t for t in candidates
               if should_sample(t.get("traceId", ""), config["sampleRate"])
               and not t.get("onlineScoredAt")][:min(limit, MAX_PER_RUN)]

    all_scores: list[metrics.Score] = []
    for trace in sampled:
        scores = [
            judges.run_judge(name,
                             output=trace.get("outputPreview", ""),
                             input_text=trace.get("inputPreview", ""),
                             run_id=f"online-{trace.get('traceId', '')}")
            for name in config["judges"]
        ]
        all_scores.extend(scores)
        _mark_scored(project, trace, scores)

    summary = metrics.aggregate(all_scores)
    return {
        "status": "ok",
        "scored": len(sampled),
        "candidates": len(candidates),
        "sampleRate": config["sampleRate"],
        # Named an estimate on purpose: this is a sample, not a census.
        "estimate": True,
        **summary,
    }


def _mark_scored(project_id: str, trace: dict, scores: list[metrics.Score]) -> None:
    """Write scores back so the list view shows quality inline and a rerun does not
    re-bill the same trace.

    Delegated to the store rather than writing to DynamoDB here. It used to call
    `db.update_item("ai-traces", ...)` directly, which meant that the moment the read
    path moved to Opik this sweep would have kept spending money on judges and
    writing the results into a table nobody was reading — and `onlineScoredAt` would
    never come back, so every sweep would re-score the same traces forever.
    """
    from src.aiobs import service
    store = service.get_store()
    try:
        if not store.record_scores(project_id, trace, list(scores)):
            log.debug("scores not persisted for %s", trace.get("traceId"))
    except Exception as exc:  # noqa: BLE001
        log.debug("could not persist online scores for %s: %s",
                  trace.get("traceId"), exc)

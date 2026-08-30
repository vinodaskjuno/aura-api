"""Experiments: run a dataset through a target and score every item.

An experiment is the unit of comparison — change a prompt or a model, re-run the
same dataset, and diff the aggregates. Per-item scores are kept alongside the
aggregate because a pass rate that moved from 0.8 to 0.7 is only actionable if
you can see which three items regressed.

Test suites are the same machinery with natural-language assertions instead of
metrics, so a suite run gates CI on a pass rate.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from src.aiobs import datasets, judges, metrics
from src.aiobs.metrics import Score

log = logging.getLogger(__name__)

TABLE = "ai-experiments"
META = "__meta__"

# Cap so a runaway config cannot bill an unbounded number of judge calls.
MAX_ITEMS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(name: str, dataset_id: str, project_id: str, actor: str,
           config: dict | None = None) -> dict:
    from src.database import dynamo_client as db
    experiment_id = f"ex-{uuid.uuid4().hex[:12]}"
    meta = {
        "experimentId": experiment_id, "itemKey": META, "name": name,
        "datasetId": dataset_id, "projectId": project_id,
        "status": "created", "createdAt": _now(), "createdBy": actor,
        "config": config or {}, "summary": {},
    }
    db.put_item(TABLE, meta)
    return meta


def get(experiment_id: str) -> dict | None:
    from src.database import dynamo_client as db
    return db.get_item(TABLE, {"experimentId": experiment_id, "itemKey": META})


def results(experiment_id: str, limit: int = 1000) -> list[dict]:
    from src.database import dynamo_client as db
    rows = db.query_items(TABLE, "experimentId", experiment_id, limit=limit) or []
    return sorted((r for r in rows if r.get("itemKey") != META),
                  key=lambda r: r.get("itemKey", ""))


def list_experiments(project_id: str = "", limit: int = 100) -> list[dict]:
    from src.database import dynamo_client as db
    rows = db.scan_items(TABLE, limit=limit * 20) or []
    metas = [r for r in rows if r.get("itemKey") == META]
    if project_id:
        metas = [r for r in metas if r.get("projectId") == project_id]
    return sorted(metas, key=lambda r: r.get("createdAt", ""), reverse=True)[:limit]


def _score_item(item: dict, output: str, latency_ms: int, cost_usd: float,
                config: dict, run_id: str) -> list[Score]:
    """Heuristics first — deterministic and free, so a cheap failure short-circuits
    an expensive judge."""
    scores: list[Score] = []

    for spec in config.get("metrics", []) or []:
        name = spec if isinstance(spec, str) else spec.get("name", "")
        opts = {} if isinstance(spec, str) else {k: v for k, v in spec.items() if k != "name"}
        scores.append(metrics.run_heuristic(
            name, output=output, expected=item.get("expected", ""),
            latency_ms=latency_ms, cost_usd=cost_usd, **opts))

    for spec in config.get("judges", []) or []:
        name = spec if isinstance(spec, str) else spec.get("name", "")
        threshold = 0.7 if isinstance(spec, str) else float(spec.get("threshold", 0.7))
        scores.append(judges.run_judge(
            name, output=output, input_text=item.get("input", ""),
            expected=item.get("expected", ""),
            context=str(item.get("metadata", {}).get("context", "")),
            threshold=threshold, run_id=run_id))

    for spec in config.get("assertions", []) or []:
        text = spec if isinstance(spec, str) else spec.get("text", "")
        scores.append(judges.run_assertion(
            text, output=output, input_text=item.get("input", ""), run_id=run_id))

    return scores


def run(experiment_id: str, target: Callable[[str], dict],
        limit: int = MAX_ITEMS) -> dict:
    """Execute an experiment.

    `target` takes an item's input and returns
    {output, latencyMs, costUsd, traceId} — a plain callable, so the thing under
    test can be an Aura agent, an HTTP endpoint or a client's own function without
    this module knowing which.
    """
    from src.database import dynamo_client as db

    meta = get(experiment_id)
    if not meta:
        return {"error": f"unknown experiment {experiment_id!r}"}

    config = meta.get("config") or {}
    items = datasets.items(meta["datasetId"], limit=limit)[:min(limit, MAX_ITEMS)]
    if not items:
        db.update_item(TABLE, {"experimentId": experiment_id, "itemKey": META},
                       {"status": "empty", "finishedAt": _now()})
        return {"experimentId": experiment_id, "status": "empty", "itemCount": 0}

    db.update_item(TABLE, {"experimentId": experiment_id, "itemKey": META},
                   {"status": "running", "startedAt": _now()})

    all_scores: list[Score] = []
    failed_items = 0

    for item in items:
        try:
            produced = target(item.get("input", "")) or {}
        except Exception as exc:  # noqa: BLE001 — one bad item must not void the run
            log.warning("experiment %s item %s target failed: %s",
                        experiment_id, item.get("itemId"), exc)
            produced = {"output": "", "error": str(exc)[:300]}
            failed_items += 1

        output = str(produced.get("output") or "")
        latency = int(produced.get("latencyMs") or 0)
        cost = float(produced.get("costUsd") or 0.0)
        scores = _score_item(item, output, latency, cost, config, experiment_id)
        all_scores.extend(scores)

        db.put_item(TABLE, {
            "experimentId": experiment_id,
            "itemKey": item["itemId"],
            "input": item.get("input", "")[:10_000],
            "expected": item.get("expected", "")[:10_000],
            "output": output[:10_000],
            "error": produced.get("error", ""),
            "latencyMs": latency,
            "costUsd": cost,
            "traceId": produced.get("traceId", ""),
            "scores": [s.as_item() for s in scores],
            "passed": all(s.passed for s in scores) if scores else False,
            "createdAt": _now(),
        })

    summary = metrics.aggregate(all_scores)
    summary["itemCount"] = len(items)
    summary["failedItems"] = failed_items
    db.update_item(TABLE, {"experimentId": experiment_id, "itemKey": META},
                   {"status": "completed", "finishedAt": _now(), "summary": summary})
    return {"experimentId": experiment_id, "status": "completed", **summary}


def compare(experiment_ids: list[str]) -> dict:
    """Aggregates side by side. The regression view: same dataset, different runs."""
    out = []
    for eid in experiment_ids[:10]:
        meta = get(eid)
        if meta:
            out.append({
                "experimentId": eid, "name": meta.get("name", ""),
                "datasetId": meta.get("datasetId", ""),
                "status": meta.get("status", ""),
                "createdAt": meta.get("createdAt", ""),
                "summary": meta.get("summary") or {},
            })
    return {"experiments": out}

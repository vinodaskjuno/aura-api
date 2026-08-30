"""AI Observability API — traces, threads, datasets, experiments, prompts.

LLM-application observability for a client's own agents. Distinct from
routers/observability.py, which investigates infrastructure incidents; the two
share the word "trace" and little else.

Ingestion is not here — traces arrive over standard OTLP at /otlp/v1/traces so
clients can instrument with any OpenTelemetry SDK.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.aiobs import datasets, experiments, judges, metrics, online_eval, prompts, service
from src.routers.auth import get_current_user, require_permission

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai-observability", tags=["ai-observability"])

_READ = require_permission("dev_workspace")
_WRITE = require_permission("dev_workspace")


# ── Traces ───────────────────────────────────────────────────────────────────

@router.get("/capabilities")
def capabilities(_: dict = Depends(_READ)):
    """What the active store can actually do, so the UI hides filters it cannot
    honour rather than offering one that silently scans."""
    return service.get_store().capabilities()


@router.get("/traces")
def list_traces(projectId: str = Query(...), limit: int = 50,
                status: str = "", threadId: str = "",
                _: dict = Depends(_READ)):
    return {"traces": service.get_store().list_traces(
        projectId, limit=min(limit, 200), status=status, thread_id=threadId)}


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, projectId: str = Query(""), _: dict = Depends(_READ)):
    store = service.get_store()
    trace = store.get_trace(projectId, trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail=f"No trace {trace_id!r}")
    return {"trace": trace, "spans": store.get_spans(trace_id)}


@router.get("/traces/{trace_id}/spans/{span_id}/payload")
def get_span_payload(trace_id: str, span_id: str, which: str = Query("input"),
                     _: dict = Depends(_READ)):
    """Full input/output for one span. Fetched on demand because large payloads
    live in S3 and the list and waterfall views must never pay for them."""
    from src.aiobs.dynamo_store import load_payload
    for span in service.get_store().get_spans(trace_id):
        if span.get("spanId") == span_id:
            ref = span.get("inputRef" if which == "input" else "outputRef")
            preview = span.get("inputPreview" if which == "input" else "outputPreview")
            return {"content": load_payload(ref) if ref else preview,
                    "truncated": bool(ref)}
    raise HTTPException(status_code=404, detail=f"No span {span_id!r}")


@router.get("/threads")
def list_threads(projectId: str = Query(...), limit: int = 50,
                 _: dict = Depends(_READ)):
    return {"threads": service.get_store().list_threads(projectId, limit=min(limit, 200))}


@router.get("/projects")
def list_projects(_: dict = Depends(_READ)):
    """Projects that have actually sent traces. Derived rather than configured, so
    a client appears the moment it starts exporting."""
    from src.database import dynamo_client as db
    try:
        rows = db.scan_items("ai-traces", limit=2000) or []
    except Exception:  # noqa: BLE001
        rows = []
    seen: dict[str, dict] = {}
    for row in rows:
        pid = row.get("projectId", "")
        if not pid:
            continue
        entry = seen.setdefault(pid, {"projectId": pid, "traceCount": 0,
                                      "costUsd": 0.0, "lastSeen": ""})
        entry["traceCount"] += 1
        entry["costUsd"] = round(entry["costUsd"] + float(row.get("costUsd") or 0), 6)
        start = row.get("startTime", "")
        if start > entry["lastSeen"]:
            entry["lastSeen"] = start
    return {"projects": sorted(seen.values(), key=lambda p: p["lastSeen"], reverse=True)}


# ── Datasets ─────────────────────────────────────────────────────────────────

class DatasetRequest(BaseModel):
    name: str
    projectId: str = ""
    description: str = ""


class ItemRequest(BaseModel):
    input: str
    expected: str = ""
    metadata: dict = {}


class SeedRequest(BaseModel):
    projectId: str
    traceIds: list[str]


@router.get("/datasets")
def get_datasets(projectId: str = "", _: dict = Depends(_READ)):
    return {"datasets": datasets.list_datasets(projectId)}


@router.post("/datasets", status_code=201)
def create_dataset(body: DatasetRequest, user: dict = Depends(_WRITE)):
    return datasets.create(body.name, body.projectId, user["username"], body.description)


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, _: dict = Depends(_READ)):
    meta = datasets.get(dataset_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"No dataset {dataset_id!r}")
    return {"dataset": meta, "items": datasets.items(dataset_id)}


@router.post("/datasets/{dataset_id}/items", status_code=201)
def add_dataset_item(dataset_id: str, body: ItemRequest, _: dict = Depends(_WRITE)):
    if not datasets.get(dataset_id):
        raise HTTPException(status_code=404, detail=f"No dataset {dataset_id!r}")
    return datasets.add_item(dataset_id, body.input, body.expected, body.metadata)


@router.delete("/datasets/{dataset_id}/items/{item_id}")
def delete_dataset_item(dataset_id: str, item_id: str, _: dict = Depends(_WRITE)):
    return {"deleted": datasets.delete_item(dataset_id, item_id)}


@router.post("/datasets/{dataset_id}/seed")
def seed_dataset(dataset_id: str, body: SeedRequest, _: dict = Depends(_WRITE)):
    """Build eval items from captured traces — the fastest route to a dataset that
    reflects real traffic rather than imagined traffic."""
    if not datasets.get(dataset_id):
        raise HTTPException(status_code=404, detail=f"No dataset {dataset_id!r}")
    return {"added": datasets.seed_from_traces(dataset_id, body.projectId, body.traceIds)}


# ── Experiments ──────────────────────────────────────────────────────────────

class ExperimentRequest(BaseModel):
    name: str
    datasetId: str
    projectId: str = ""
    # {"metrics": [...], "judges": [...], "assertions": [...]}
    config: dict = {}


class RunRequest(BaseModel):
    # Replay each item against a prompt; omit to score the dataset's own expected
    # values, which is how a suite validates its own baseline.
    promptTemplate: str = ""
    system: str = ""
    limit: int = 100


@router.get("/metrics")
def available_metrics(_: dict = Depends(_READ)):
    """Heuristics are listed first: deterministic, free, and the right default for
    a regression gate."""
    return {
        "heuristics": sorted(metrics.HEURISTICS),
        "judges": [{"name": k, "label": v["label"]} for k, v in judges.JUDGES.items()],
    }


@router.get("/experiments")
def get_experiments(projectId: str = "", _: dict = Depends(_READ)):
    return {"experiments": experiments.list_experiments(projectId)}


@router.post("/experiments", status_code=201)
def create_experiment(body: ExperimentRequest, user: dict = Depends(_WRITE)):
    if not datasets.get(body.datasetId):
        raise HTTPException(status_code=404, detail=f"No dataset {body.datasetId!r}")
    return experiments.create(body.name, body.datasetId, body.projectId,
                              user["username"], body.config)


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: str, _: dict = Depends(_READ)):
    meta = experiments.get(experiment_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"No experiment {experiment_id!r}")
    return {"experiment": meta, "results": experiments.results(experiment_id)}


@router.post("/experiments/{experiment_id}/run")
def run_experiment(experiment_id: str, body: RunRequest, _: dict = Depends(_WRITE)):
    """Run synchronously. Bounded by MAX_ITEMS, and judge calls cost money, so the
    caller is made to wait rather than firing an unbounded background bill."""
    if not experiments.get(experiment_id):
        raise HTTPException(status_code=404, detail=f"No experiment {experiment_id!r}")

    def target(text: str) -> dict:
        if not body.promptTemplate:
            # No prompt: score the dataset's own expected values. Useful for
            # checking that a metric config behaves before spending on a real run.
            return {"output": text, "latencyMs": 0, "costUsd": 0.0}
        result = prompts.run_playground(body.promptTemplate, {"input": text},
                                        body.system, run_id=experiment_id)
        return {"output": result.get("output", ""),
                "latencyMs": result.get("latencyMs", 0),
                "costUsd": result.get("costUsd", 0.0),
                "error": result.get("error") or ""}

    return experiments.run(experiment_id, target, limit=body.limit)


@router.get("/experiments/compare")
def compare_experiments(ids: str = Query(...), _: dict = Depends(_READ)):
    return experiments.compare([i for i in ids.split(",") if i])


# ── Prompts & playground ─────────────────────────────────────────────────────

class PromptRequest(BaseModel):
    promptId: str
    template: str
    projectId: str = ""
    description: str = ""


class PlaygroundRequest(BaseModel):
    template: str
    variables: dict = {}
    system: str = ""


@router.get("/prompts")
def get_prompts(projectId: str = "", _: dict = Depends(_READ)):
    return {"prompts": prompts.list_prompts(projectId)}


@router.post("/prompts", status_code=201)
def save_prompt(body: PromptRequest, user: dict = Depends(_WRITE)):
    return prompts.save(body.promptId, body.template, user["username"],
                        body.projectId, body.description)


@router.get("/prompts/{prompt_id}")
def get_prompt(prompt_id: str, _: dict = Depends(_READ)):
    found = prompts.versions(prompt_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"No prompt {prompt_id!r}")
    return {"promptId": prompt_id, "versions": found, "latest": found[-1]}


@router.post("/playground")
def playground(body: PlaygroundRequest, _: dict = Depends(_WRITE)):
    return prompts.run_playground(body.template, body.variables, body.system)


# ── Online evaluation ────────────────────────────────────────────────────────

class OnlineConfigRequest(BaseModel):
    enabled: bool
    sampleRate: float = 0.05
    judges: list[str] = ["relevance"]
    projectId: str = ""


@router.get("/online-eval")
def get_online_config(_: dict = Depends(_READ)):
    return online_eval.get_config()


@router.put("/online-eval")
def set_online_config(body: OnlineConfigRequest, user: dict = Depends(_WRITE)):
    unknown = [j for j in body.judges if j not in judges.JUDGES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown judge(s): {unknown}")
    return online_eval.set_config(body.enabled, body.sampleRate, body.judges,
                                  body.projectId, user["username"])


@router.post("/online-eval/run")
def run_online_sweep(limit: int = 50, _: dict = Depends(_WRITE)):
    return online_eval.run_sweep(limit=limit)

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


def _tenant_scope(user: dict) -> str:
    """The tenantId a caller's reads are confined to, or "" for unrestricted.

    Traces are written with `tenantId` set to the ingesting credential's userId
    (routers/otlp.py passes user_id into that slot) but until now NOTHING read it
    back, so any holder of `dev_workspace` could read every other user's prompts and
    completions.

    This deployment is one company with multiple teams, so an admin seeing
    everything is correct and expected; what is not correct is an ordinary user
    enumerating a colleague's prompts. Admins are therefore unrestricted and everyone
    else is pinned to their own tenant.
    """
    role = (user or {}).get("role", "")
    if role in ("admin", "super_admin"):
        return ""
    return (user or {}).get("userId") or (user or {}).get("username") or ""


def _store_supports(name: str) -> bool:
    return bool(service.get_store().capabilities().get(name))


def _spans_of(store, trace: dict | None, project_id: str = "",
              trace_id: str = "") -> list:
    """Spans of a trace, tolerating both store signatures.

    The Opik store needs a project (its spans endpoint 400s without one); the
    DynamoDB store partitions spans by trace_id and takes no project at all. Rather
    than force one signature on both, the extra argument is passed when accepted.
    """
    tid = (trace or {}).get("traceId") or trace_id
    if not tid:
        return []
    project = project_id or (trace or {}).get("projectId") or ""
    try:
        return store.get_spans(tid, project_id=project)
    except TypeError:
        # DynamoTraceStore.get_spans(trace_id, limit) — no project parameter.
        return store.get_spans(tid)


# ── Traces ───────────────────────────────────────────────────────────────────

@router.get("/capabilities")
def capabilities(_: dict = Depends(_READ)):
    """What the active store can actually do, so the UI hides filters it cannot
    honour rather than offering one that silently scans.

    Also carries `opikUiUrl`: the address a BROWSER should load the Opik UI from.
    Resolved here rather than in the bundle because the frontend must not read
    addresses from import.meta.env — those are inlined at build time and would pin
    one image to one environment.
    """
    from src.config_settings import get_settings
    s = get_settings()

    # EXPLICIT ONLY. An earlier version derived this as
    # "<request host>:<opik_ui_port>" whenever opik_enabled was true, and that was
    # wrong: opik_enabled means "Aura may WRITE spans to Opik", which says nothing
    # about whether a browser can reach Opik's UI. In the single-instance deployment
    # there is no listener on that port at all, so the SPA got a dead address, pointed
    # an iframe at it, and rendered a blank panel — the least debuggable outcome.
    #
    # Only the infrastructure knows whether a listener exists, so Terraform sets this
    # and sets it EMPTY when it does not. The SPA then shows an explanatory guard
    # instead of a broken frame.
    return {**service.get_store().capabilities(),
            "opikEnabled": bool(s.opik_enabled),
            "opikUiUrl": s.opik_ui_url,
            # Same reasoning as opikUiUrl: only the infrastructure knows whether the
            # demo service was actually deployed, so the UI is told rather than
            # guessing. False hides the trigger button instead of offering one that
            # would 503.
            "demoAgentsEnabled": bool(s.demo_agents_url)}


@router.get("/traces")
def list_traces(projectId: str = Query(...), limit: int = 50,
                status: str = "", threadId: str = "", search: str = "",
                user: dict = Depends(_READ)):
    """Traces in a project, newest first.

    `search` is only forwarded when the active store reports `fullTextSearch`.
    Silently ignoring it would be worse than refusing it: the caller would get a
    complete list back and believe it had been filtered.
    """
    if search and not _store_supports("fullTextSearch"):
        raise HTTPException(
            status_code=400,
            detail=("Free-text search is not available on the active store "
                    f"({service.get_store().capabilities().get('store')}). "
                    "See GET /capabilities."))
    return {"traces": service.get_store().list_traces(
        projectId, limit=min(limit, 200), status=status, thread_id=threadId,
        search=search, tenant_id=_tenant_scope(user))}


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, projectId: str = Query(""), user: dict = Depends(_READ)):
    store = service.get_store()
    trace = store.get_trace(projectId, trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail=f"No trace {trace_id!r}")
    # 404 rather than 403 for another tenant's trace: a 403 confirms the id exists,
    # which is itself a leak when ids are guessable.
    scope = _tenant_scope(user)
    if scope and trace.get("tenantId") not in ("", None, scope):
        raise HTTPException(status_code=404, detail=f"No trace {trace_id!r}")
    # projectId is forwarded because Opik's spans endpoint requires a project; the
    # DynamoDB store ignores the extra argument.
    return {"trace": trace,
            "spans": _spans_of(store, trace, projectId)}


@router.get("/traces/{trace_id}/spans/{span_id}/payload")
def get_span_payload(trace_id: str, span_id: str, which: str = Query("input"),
                     user: dict = Depends(_READ)):
    """Full input/output for one span.

    Fetched on demand because under DynamoDB large payloads live in S3 and the list
    and waterfall views must never pay for them. Under Opik there is no S3 ref —
    Opik holds the payload — so the ref is empty and the "preview" already is the
    full text. Both cases are handled by the same fallback.
    """
    from src.aiobs.dynamo_store import load_payload

    # Scope before reading a payload: this is the endpoint that returns raw prompt
    # text, so it is the one that matters most.
    store = service.get_store()
    scope = _tenant_scope(user)
    # Resolved unconditionally: the tenant check needs it, and so does the Opik
    # store, whose spans endpoint requires the trace's project.
    owner = store.get_trace("", trace_id)
    if scope and owner and owner.get("tenantId") not in ("", None, scope):
        raise HTTPException(status_code=404, detail=f"No span {span_id!r}")

    for span in _spans_of(store, owner, "", trace_id):
        if span.get("spanId") == span_id or span.get("tags", {}).get("auraSpanId") == span_id:
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
def list_projects(user: dict = Depends(_READ)):
    """Projects that have actually sent traces. Derived rather than configured, so a
    client appears the moment it starts exporting.

    Delegated to the store, and tenant-scoped. This used to do
    `scan_items("ai-traces", limit=2000)` inline with no tenant predicate, which
    enumerated every tenant's project names for any caller holding dev_workspace.
    """
    return {"projects": service.get_store().list_projects(
        tenant_id=_tenant_scope(user))}


@router.get("/summary")
def summary(projectId: str = Query(...), limit: int = 500,
            user: dict = Depends(_READ)):
    """KPIs and a daily series for the Overview dashboard.

    Computed from `list_traces` rather than from a store-specific aggregation query,
    so ONE implementation serves both engines. The cost is honesty about accuracy:
    this aggregates the most recent `limit` traces, so on a busy project it is a
    sample of recent activity and not a census.

    `exact` says which it is — it is true only when the whole window fitted inside
    the page. The UI must label a sampled number as such; a dashboard that quietly
    reports a partial sum as a total is worse than one that admits its bound.
    """
    caps = service.get_store().capabilities()
    rows = service.get_store().list_traces(
        projectId, limit=min(max(limit, 1), 1000),
        tenant_id=_tenant_scope(user))

    total = len(rows)
    errors = sum(1 for r in rows if r.get("status") == "error")
    cost = round(sum(float(r.get("costUsd") or 0) for r in rows), 6)
    tokens = sum(int(r.get("totalTokens") or 0) for r in rows)
    latencies = sorted(int(r.get("latencyMs") or 0) for r in rows)

    def pct(p: float) -> int:
        """Nearest-rank percentile. Deliberately not interpolated: with a handful of
        traces an interpolated p95 invents a latency no request ever had."""
        if not latencies:
            return 0
        idx = min(len(latencies) - 1, int(round((p / 100.0) * (len(latencies) - 1))))
        return latencies[idx]

    # Daily buckets, keyed on the ISO date prefix so no timezone maths is needed —
    # every timestamp in the store is already UTC ISO-8601.
    days: dict[str, dict] = {}
    for row in rows:
        day = (row.get("startTime") or "")[:10]
        if not day:
            continue
        bucket = days.setdefault(day, {"day": day, "traces": 0, "errors": 0,
                                       "costUsd": 0.0, "totalTokens": 0})
        bucket["traces"] += 1
        bucket["errors"] += 1 if row.get("status") == "error" else 0
        bucket["costUsd"] = round(bucket["costUsd"] + float(row.get("costUsd") or 0), 6)
        bucket["totalTokens"] += int(row.get("totalTokens") or 0)

    # Judge scores, averaged per metric name across whatever has been scored.
    scores: dict[str, list[float]] = {}
    for row in rows:
        for score in row.get("onlineScores") or []:
            if isinstance(score, dict) and score.get("name"):
                scores.setdefault(score["name"], []).append(float(score.get("value") or 0))

    models: dict[str, int] = {}
    for row in rows:
        for provider in row.get("providers") or []:
            models[provider] = models.get(provider, 0) + 1

    return {
        "projectId": projectId,
        "window": {"traces": total, "limit": limit,
                   # True only if the window was not truncated by the page bound.
                   "exact": total < limit},
        "kpis": {
            "traces": total,
            "errors": errors,
            "errorRate": round(errors / total, 4) if total else 0.0,
            "costUsd": cost,
            "totalTokens": tokens,
            "p50LatencyMs": pct(50),
            "p95LatencyMs": pct(95),
            "avgCostUsd": round(cost / total, 8) if total else 0.0,
        },
        "daily": sorted(days.values(), key=lambda d: d["day"]),
        "scores": [{"name": k, "mean": round(sum(v) / len(v), 4), "count": len(v)}
                   for k, v in sorted(scores.items())],
        "providers": [{"provider": k, "traces": v}
                      for k, v in sorted(models.items(), key=lambda kv: -kv[1])],
        # Surfaced so the UI can show a "degraded" banner instead of an empty
        # dashboard when Opik is unreachable.
        "store": caps.get("store", ""),
        "degraded": bool(caps.get("degraded")),
    }


@router.put("/traces/{trace_id}/feedback")
def set_feedback(trace_id: str, body: dict, projectId: str = Query(...),
                 user: dict = Depends(_WRITE)):
    """Human feedback on a trace — the biggest capability aiobs lacked.

    Goes through `record_scores` so it lands wherever the active store keeps scores:
    native, filterable feedback scores under Opik, or a JSON blob on the row under
    DynamoDB. A thumbs-up and a judge score are therefore the same kind of object,
    which is what makes online eval and human review comparable at all.
    """
    from src.aiobs.metrics import Score

    name = str(body.get("name") or "user_feedback")[:60]
    try:
        value = float(body.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="`value` must be a number") from None
    if not 0.0 <= value <= 1.0:
        raise HTTPException(status_code=400, detail="`value` must be between 0 and 1")

    store = service.get_store()
    trace = store.get_trace(projectId, trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail=f"No trace {trace_id!r}")
    scope = _tenant_scope(user)
    if scope and trace.get("tenantId") not in ("", None, scope):
        raise HTTPException(status_code=404, detail=f"No trace {trace_id!r}")

    reason = f"{user.get('username', 'user')}: {str(body.get('reason') or '')[:400]}"
    ok = store.record_scores(projectId, trace,
                             [Score(name, value, value >= 0.5, reason)])
    if not ok:
        raise HTTPException(status_code=503,
                            detail="Score could not be stored; the trace store rejected it")
    return {"stored": True, "name": name, "value": value}


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


# ─────────────────────────────────────────────────────────────────────────────
# Demo agents
# ─────────────────────────────────────────────────────────────────────────────

DEMO_AGENTS = ["rag", "tools", "chat", "flaky"]


@router.post("/demo/run")
def run_demo_agents(agent: str = Query("all"), count: int = Query(1, ge=1, le=5),
                    _: dict = Depends(_WRITE)):
    """Make the demo agents produce traces now.

    A thin forwarder to the demo service over Service Connect, and thin on purpose:
    it adds NO new nginx route, NO new listener and NO new gate. The demo container is
    reachable only from the backend's security group, so this endpoint — already behind
    Aura's own auth and `dev_workspace` — is the single way in.

    Returns 503 rather than 500 when the service is absent, because "the demo agents
    are not deployed here" is a configuration answer, not a fault.
    """
    from src.config_settings import get_settings
    import httpx

    base = (get_settings().demo_agents_url or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503,
                            detail="Demo agents are not deployed in this environment")
    if agent != "all" and agent not in DEMO_AGENTS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown agent {agent!r}; valid: {DEMO_AGENTS} or 'all'")

    # Generous timeout: a burst of five rounds across four agents is twenty real model
    # calls, and the endpoint deliberately waits for the SDK flush so the caller can
    # refresh immediately and actually see the rows.
    #
    # This only works because ECS Service Connect's per-request timeout is disabled for
    # these services (infra/ecs.tf). Its 15s DEFAULT cut the call off with a 504 from a
    # hop that appears in no application log.
    try:
        response = httpx.post(f"{base}/run/{agent}", params={"count": count}, timeout=280.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        log.warning("demo trigger rejected by the agent service: %s", exc)
        raise HTTPException(status_code=exc.response.status_code,
                            detail=exc.response.text[:300]) from exc
    except Exception as exc:                       # noqa: BLE001
        log.warning("demo trigger failed: %s", exc)
        raise HTTPException(status_code=502,
                            detail=f"Could not reach the demo agents: {exc}") from exc


@router.get("/demo/status")
def demo_agents_status(_: dict = Depends(_READ)):
    """What each demo agent has done, for the pre-demo health check.

    Never raises: this is the endpoint someone opens when they suspect the demo is
    broken, and it answering with a 500 would tell them nothing.
    """
    from src.config_settings import get_settings
    import httpx

    base = (get_settings().demo_agents_url or "").rstrip("/")
    if not base:
        return {"deployed": False, "agents": {}}
    try:
        response = httpx.get(f"{base}/status", timeout=10.0)
        response.raise_for_status()
        return {"deployed": True, **response.json()}
    except Exception as exc:                       # noqa: BLE001
        return {"deployed": True, "reachable": False, "error": str(exc)[:200],
                "agents": {}}

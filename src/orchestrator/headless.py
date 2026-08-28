"""Headless run entry point — the ONE function the API and the eval harness both call.

`run_headless` executes any of the three pipeline kinds without requiring a live
WebSocket, and returns a `RunTrace`: the complete, replayable record of what happened.

The router wraps it with a `ws_emitter`; the eval harness passes `emit=None` and reads
`trace.events`. That is the seam (S1/S2/S6) that makes batch evaluation possible without
re-plumbing every agent.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from src.orchestrator.dag_runtime import (
    Emitter, list_emitter, null_emitter, tee_emitter,
)

log = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = 1


@dataclass
class RunRequest:
    kind: Literal["orchestration", "dag", "investigation"]
    intent: str
    user_id: str
    username: str
    role: str
    session_id: str
    project_id: str | None = None
    extra: dict = field(default_factory=dict)
    command: str | None = None
    agents_override: list[str] | None = None
    capture_prompts: bool = False


@dataclass
class RunTrace:
    trace_schema_version: int
    run_id: str
    kind: str
    started_at: str
    completed_at: str
    status: str
    request: dict
    events: list[dict] = field(default_factory=list)
    results: dict[str, dict] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    summary: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


async def run_headless(req: RunRequest, emit: Emitter | None = None) -> RunTrace:
    """Run a pipeline and capture everything it emitted.

    `emit` is an ADDITIONAL sink (typically a WebSocket). The trace's own sink is
    always attached, so a trace is produced whether or not anyone is watching.
    """
    started = datetime.now(timezone.utc)
    events: list[dict] = []
    sink = list_emitter(events)
    combined: Emitter = tee_emitter(sink, emit) if emit is not None else tee_emitter(sink, null_emitter())

    trace = RunTrace(
        trace_schema_version=TRACE_SCHEMA_VERSION,
        run_id="",
        kind=req.kind,
        started_at=started.isoformat(),
        completed_at="",
        status="running",
        request={
            "kind": req.kind, "intent": req.intent, "project_id": req.project_id,
            "command": req.command, "agents_override": req.agents_override,
            "session_id": req.session_id, "username": req.username,
            "extra_keys": sorted(req.extra.keys()),
        },
        events=events,
    )

    try:
        if req.kind == "dag":
            from src.orchestrator.dag import run_dag
            summary = await _run_dag_headless(req, combined)
        elif req.kind == "investigation":
            from src.orchestrator.investigation_dag import run_investigation_headless
            summary = await run_investigation_headless(req, combined)
        elif req.kind == "orchestration":
            from src.orchestrator.executor import run_orchestration_headless
            summary = await run_orchestration_headless(
                user_message=req.intent, user_id=req.user_id, username=req.username,
                role=req.role, session_id=req.session_id, project_id=req.project_id,
                emit=combined,
            )
        else:
            raise ValueError(f"unknown run kind: {req.kind}")

        trace.run_id = summary.get("run_id", "")
        trace.summary = summary
        trace.results = summary.get("agent_runs", {}) or summary.get("prior_results", {})
        trace.evidence = summary.get("evidence_index", []) or []
        trace.llm_calls = summary.get("llm_calls", []) or []
        trace.cost_usd = float(summary.get("cost_usd", 0.0) or 0.0)
        trace.status = summary.get("status", "success")
    except Exception as exc:  # noqa: BLE001
        log.exception("Headless run failed (%s)", req.kind)
        trace.status = "failed"
        trace.error = str(exc)
        await combined({"type": "error", "message": str(exc)})

    trace.completed_at = datetime.now(timezone.utc).isoformat()
    return trace


async def _run_dag_headless(req: RunRequest, emit: Emitter) -> dict:
    """Run the ontology DAG through the shared runtime with an arbitrary emitter."""
    from src.orchestrator import dag as dag_mod

    # run_dag only accepts a WebSocket; use a tiny shim so an Emitter works too.
    class _EmitShim:
        async def send_json(self, event: dict) -> None:
            await emit(event)

    return await dag_mod.run_dag(
        intent=req.intent, user_id=req.user_id, username=req.username, role=req.role,
        session_id=req.session_id, project_id=req.project_id, extra=req.extra,
        ws=_EmitShim(), command=req.command, agents_override=req.agents_override,
    )


def persist_trace(trace: RunTrace, project_id: str | None = None) -> str | None:
    """Write the trace to S3 (whole doc) and DynamoDB (per-event rows). Best effort."""
    uri: str | None = None
    try:
        from src.storage.s3_client import put_json
        key = f"observability/traces/{trace.run_id or 'unknown'}.json"
        uri = put_json("exports", key, trace.to_dict())
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist trace to S3: %s", exc)

    try:
        from src.database.dynamo_client import put_item
        for i, ev in enumerate(trace.events):
            put_item("observability-traces", {
                "runId": trace.run_id or "unknown",
                "seq": f"{i:06d}",
                "type": ev.get("type", ""),
                "event": ev,
                "projectId": project_id or "",
                "createdAt": trace.started_at,
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist trace events: %s", exc)

    return uri

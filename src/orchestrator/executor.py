"""Orchestrator executor — classify → run agents → persist → done.

Split into a headless core (`run_orchestration_headless`) and a thin WebSocket
wrapper (`run_orchestration`). The wrapper's signature is unchanged: `orchestrator/
router.py` and the VS Code path both call it by keyword.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import WebSocket

from src.agents.base_agent import AgentContext, AgentResult
from src.orchestrator.agent_registry import get_agent
from src.orchestrator.dag_runtime import Emitter, ws_emitter
from src.orchestrator.intent_classifier import classify
from src.database.dynamo_client import put_item

logger = logging.getLogger(__name__)


async def run_orchestration_headless(
    user_message: str,
    user_id: str,
    username: str,
    role: str,
    session_id: str,
    project_id: str | None = None,
    emit: Emitter | None = None,
) -> dict:
    """Full orchestration flow, emitter-driven. Returns a summary dict."""
    from src.orchestrator.dag_runtime import null_emitter
    emit = emit or null_emitter()

    run_id = f"orch-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{session_id[:8]}"
    await emit({"type": "orchestration_start", "runId": run_id})

    # ── Step 1: Classify intent ───────────────────────────────────────────────
    await emit({"type": "status", "message": "Classifying intent..."})
    try:
        classification = await asyncio.get_event_loop().run_in_executor(
            None, classify, user_message
        )
    except Exception as exc:  # noqa: BLE001
        await emit({"type": "error", "message": f"Classification failed: {exc}"})
        return {"run_id": run_id, "status": "failed", "error": str(exc)}

    agent_names: list[str] = classification.get("agents", ["knowledge_graph_agent"])
    intent_summary: str = classification.get("intent_summary", user_message[:80])

    await emit({
        "type": "intent_classified",
        "intentSummary": intent_summary,
        "agents": agent_names,
    })

    # ── Step 2: Build context ─────────────────────────────────────────────────
    context = AgentContext(
        user_id=user_id,
        username=username,
        role=role,
        intent=user_message,
        project_id=project_id,
        session_id=session_id,
        # session_id groups a conversation into one thread in the traces view.
        opik_thread_id=session_id or "",
    )

    # ── Step 3: Run agents sequentially (passing prior results forward) ───────
    all_results: dict[str, AgentResult] = {}

    for agent_name in agent_names:
        agent = get_agent(agent_name)
        if agent is None:
            await emit({"type": "agent_skip", "agent": agent_name, "reason": "not registered"})
            continue

        await emit({"type": "agent_start", "agent": agent_name})
        context.prior_results = dict(all_results)

        try:
            result = await agent.run(context)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent %s raised an exception", agent_name)
            result = AgentResult(agent_name=agent_name, status="failed", error=str(exc))
            result.finish("failed")

        all_results[agent_name] = result

        # The first agent that actually calls an LLM creates the Opik trace; every
        # later agent in this run joins it. Without this a 5-agent run renders as 5
        # unrelated traces and the point of tracing a DAG is lost.
        if not context.opik_trace_id and result.opik_trace_id:
            context.opik_trace_id = result.opik_trace_id

        await emit({
            "type": "agent_done",
            "agent": agent_name,
            "status": result.status,
            "summary": result.activity_log[-1] if result.activity_log else "",
            "artifactCount": len(result.artifacts),
            "kgUpdates": len(result.kg_updates),
            "opikTraceId": result.opik_trace_id or context.opik_trace_id or "",
        })

        # Persist activity log to DynamoDB
        for log_line in result.activity_log:
            try:
                put_item("activity", {
                    "userId": user_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "agent": agent_name,
                    "runId": run_id,
                    "projectId": project_id or "",
                    "message": log_line,
                })
            except Exception:
                pass

    # ── Step 4: Persist agent run audit ──────────────────────────────────────
    try:
        put_item("agents", {
            "agentRunId": run_id,
            "orchestratorId": session_id,
            "userId": user_id,
            "projectId": project_id or "",
            "intent": intent_summary,
            "agentsRun": agent_names,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "status": "completed",
        })
    except Exception:
        pass

    total_kg = sum(len(r.kg_updates) for r in all_results.values())
    total_artifacts = sum(len(r.artifacts) for r in all_results.values())

    await emit({
        "type": "orchestration_done",
        "runId": run_id,
        "agentsRun": agent_names,
        "totalKgUpdates": total_kg,
        "totalArtifacts": total_artifacts,
    })

    return {
        "run_id": run_id,
        "status": "success",
        "agents_run": agent_names,
        "intent_summary": intent_summary,
        "total_kg_updates": total_kg,
        "total_artifacts": total_artifacts,
        "agent_runs": {k: {"status": v.status, "output": v.output,
                           "activity_log": v.activity_log, "error": v.error}
                       for k, v in all_results.items()},
    }


async def run_orchestration(
    ws: WebSocket,
    user_message: str,
    user_id: str,
    username: str,
    role: str,
    session_id: str,
    project_id: str | None = None,
) -> None:
    """WebSocket wrapper — signature unchanged for every existing caller."""
    await run_orchestration_headless(
        user_message=user_message,
        user_id=user_id,
        username=username,
        role=role,
        session_id=session_id,
        project_id=project_id,
        emit=ws_emitter(ws),
    )

"""Orchestrator executor — runs agents sequentially, streams progress via WebSocket."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import WebSocket

from src.agents.base_agent import AgentContext, AgentResult
from src.orchestrator.agent_registry import get_agent
from src.orchestrator.intent_classifier import classify
from src.database.dynamo_client import put_item

logger = logging.getLogger(__name__)


async def _send(ws: WebSocket, event: dict) -> None:
    try:
        await ws.send_json(event)
    except Exception:
        pass


async def run_orchestration(
    ws: WebSocket,
    user_message: str,
    user_id: str,
    username: str,
    role: str,
    session_id: str,
    project_id: str | None = None,
) -> None:
    """
    Full orchestration flow:
      classify → select agents → run each → collect results → persist → done
    """
    run_id = f"orch-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{session_id[:8]}"
    await _send(ws, {"type": "orchestration_start", "runId": run_id})

    # ── Step 1: Classify intent ───────────────────────────────────────────────
    await _send(ws, {"type": "status", "message": "Classifying intent..."})
    try:
        classification = await asyncio.get_event_loop().run_in_executor(
            None, classify, user_message
        )
    except Exception as exc:
        await _send(ws, {"type": "error", "message": f"Classification failed: {exc}"})
        return

    agent_names: list[str] = classification.get("agents", ["knowledge_graph_agent"])
    intent_summary: str = classification.get("intent_summary", user_message[:80])

    await _send(ws, {
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
    )

    # ── Step 3: Run agents sequentially (passing prior results forward) ───────
    all_results: dict[str, AgentResult] = {}

    for agent_name in agent_names:
        agent = get_agent(agent_name)
        if agent is None:
            await _send(ws, {"type": "agent_skip", "agent": agent_name, "reason": "not registered"})
            continue

        await _send(ws, {"type": "agent_start", "agent": agent_name})
        context.prior_results = dict(all_results)

        try:
            result = await agent.run(context)
        except Exception as exc:
            logger.exception("Agent %s raised an exception", agent_name)
            result = AgentResult(agent_name=agent_name, status="failed", error=str(exc))
            result.finish("failed")

        all_results[agent_name] = result

        await _send(ws, {
            "type": "agent_done",
            "agent": agent_name,
            "status": result.status,
            "summary": result.activity_log[-1] if result.activity_log else "",
            "artifactCount": len(result.artifacts),
            "kgUpdates": len(result.kg_updates),
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

    await _send(ws, {
        "type": "orchestration_done",
        "runId": run_id,
        "agentsRun": agent_names,
        "totalKgUpdates": sum(len(r.kg_updates) for r in all_results.values()),
        "totalArtifacts": sum(len(r.artifacts) for r in all_results.values()),
    })

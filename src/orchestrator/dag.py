"""
DAG Executor — parallel/sequential multi-agent pipeline for AURA.

Pipeline stages (each stage may run agents in parallel):
  Stage 1 (parallel):  project_scanner | knowledge_analyzer | infrastructure_analyzer
  Stage 2 (parallel):  file_analyzer | api_analyzer | database_analyzer | domain_analyzer
  Stage 3 (parallel):  architecture_analyzer | business_logic_analyzer |
                       data_flow_analyzer | dependency_analyzer | article_analyzer
  Stage 4 (seq):       graph_builder (waits for all Stage 1-3 results)
  Stage 5 (seq):       graph_reviewer → knowledge_validator
  Stage 6 (parallel):  tour_builder | teacher_agent

WebSocket events emitted:
  {type: "dag_start",   runId, total_stages, total_agents}
  {type: "stage_start", stage, title, agents: [name]}
  {type: "agent_start", agent, stage}
  {type: "agent_done",  agent, stage, status, nodes_added, rels_added}
  {type: "stage_done",  stage, elapsed_ms}
  {type: "dag_done",    runId, total_nodes_added, total_rels_added, version_id}
  {type: "error",       message}
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from src.agents.base_agent import AgentContext, AgentResult
from src.services.ontology_version_service import create_version_record

log = logging.getLogger(__name__)

# Stage definitions: list of (stage_number, title, [agent_names], sequential)
_STAGES: list[tuple[int, str, list[str], bool]] = [
    (1, "Scan & Discover",
     ["project_scanner", "knowledge_analyzer", "infrastructure_analyzer"],
     False),
    (2, "Extract",
     ["file_analyzer", "api_analyzer", "database_analyzer", "domain_analyzer"],
     False),
    (3, "Analyze",
     ["architecture_analyzer", "business_logic_analyzer",
      "data_flow_analyzer", "dependency_analyzer", "article_analyzer"],
     False),
    (4, "Build Graph",
     ["graph_builder"],
     True),
    (5, "Validate",
     ["graph_reviewer", "knowledge_validator"],
     True),
    (6, "Learn & Teach",
     ["tour_builder", "teacher_agent"],
     False),
]


async def _send(ws: WebSocket | None, event: dict) -> None:
    if ws is None:
        return
    try:
        await ws.send_json(event)
    except Exception:
        pass


def _make_context(
    intent: str,
    user_id: str,
    username: str,
    role: str,
    project_id: str | None,
    session_id: str,
    version_id: str | None,
    extra: dict[str, Any],
    prior_results: dict[str, AgentResult],
) -> AgentContext:
    return AgentContext(
        user_id=user_id,
        username=username,
        role=role,
        intent=intent,
        project_id=project_id,
        session_id=session_id,
        prior_results=prior_results,
        extra={**extra, "version_id": version_id},
    )


async def _run_agent(
    agent_name: str,
    context: AgentContext,
    ws: WebSocket | None,
    stage: int,
) -> AgentResult | None:
    from src.orchestrator.agent_registry import get_agent  # lazy to avoid circular import

    await _send(ws, {"type": "agent_start", "agent": agent_name, "stage": stage})
    try:
        agent = get_agent(agent_name)
        result = await agent.run(context)
        await _send(ws, {
            "type": "agent_done",
            "agent": agent_name,
            "stage": stage,
            "status": result.status,
            "nodes_added": result.output.get("nodes_added", 0),
            "rels_added": result.output.get("rels_added", 0),
        })
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("Agent %s failed", agent_name)
        await _send(ws, {"type": "agent_done", "agent": agent_name, "stage": stage,
                         "status": "failed", "error": str(exc)})
        return None


async def run_dag(
    intent: str,
    user_id: str,
    username: str,
    role: str,
    session_id: str,
    project_id: str | None = None,
    extra: dict[str, Any] | None = None,
    ws: WebSocket | None = None,
    command: str | None = None,
    agents_override: list[str] | None = None,
) -> dict[str, Any]:
    """
    Execute the full AURA understanding DAG pipeline.

    Args:
        agents_override: If provided, only these agents are executed (skips stages
                         that have no overlap). Used by domain/infra/etc. commands.
    """
    extra = extra or {}
    run_id = f"dag-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{session_id[:8]}"
    log.info("DAG run %s starting — intent: %s", run_id, intent[:60])

    # Create a version record for this run
    version_id: str | None = None
    try:
        version = create_version_record(
            load_method=command or "understand",
            actor=username,
            sources=extra.get("sources", []),
            notes=f"DAG run {run_id}: {intent[:120]}",
        )
        version_id = version["versionId"]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not create version record: %s", exc)

    total_stages = len(_STAGES)
    total_agents = sum(len(s[2]) for s in _STAGES)
    await _send(ws, {"type": "dag_start", "runId": run_id,
                     "total_stages": total_stages, "total_agents": total_agents,
                     "version_id": version_id})

    prior_results: dict[str, AgentResult] = {}
    total_nodes = 0
    total_rels = 0

    for stage_num, title, agent_names, sequential in _STAGES:
        # Filter agents if override specified
        if agents_override is not None:
            active_agents = [a for a in agent_names if a in agents_override]
            if not active_agents:
                log.debug("Stage %d skipped — no overlap with agent override", stage_num)
                continue
        else:
            active_agents = agent_names

        t0 = time.monotonic()
        await _send(ws, {"type": "stage_start", "stage": stage_num,
                         "title": title, "agents": active_agents})
        log.info("Stage %d (%s): %s", stage_num, title, active_agents)

        if sequential:
            for agent_name in active_agents:
                ctx = _make_context(intent, user_id, username, role, project_id,
                                    session_id, version_id, extra, prior_results)
                result = await _run_agent(agent_name, ctx, ws, stage_num)
                if result:
                    prior_results[agent_name] = result
                    total_nodes += result.output.get("nodes_added", 0)
                    total_rels += result.output.get("rels_added", 0)
        else:
            # Run all agents in this stage concurrently
            ctx = _make_context(intent, user_id, username, role, project_id,
                                session_id, version_id, extra, prior_results)
            tasks = [_run_agent(name, ctx, ws, stage_num) for name in active_agents]
            results = await asyncio.gather(*tasks, return_exceptions=False)
            for name, result in zip(active_agents, results):
                if result and not isinstance(result, Exception):
                    prior_results[name] = result
                    total_nodes += result.output.get("nodes_added", 0)
                    total_rels += result.output.get("rels_added", 0)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        await _send(ws, {"type": "stage_done", "stage": stage_num,
                         "title": title, "elapsed_ms": elapsed_ms})

    summary = {
        "run_id": run_id,
        "version_id": version_id,
        "total_nodes_added": total_nodes,
        "total_rels_added": total_rels,
        "stages_run": total_stages,
        "prior_results": {k: {"status": v.status, "output": v.output}
                          for k, v in prior_results.items()},
    }
    await _send(ws, {"type": "dag_done", "runId": run_id,
                     "total_nodes_added": total_nodes, "total_rels_added": total_rels,
                     "version_id": version_id})
    log.info("DAG run %s complete: %d nodes, %d rels", run_id, total_nodes, total_rels)
    return summary


# Agent subsets for named commands
COMMAND_AGENTS: dict[str, list[str]] = {
    "understand": [],  # empty = all agents
    "understand-domain": [
        "knowledge_analyzer", "article_analyzer",
        "domain_analyzer", "business_logic_analyzer", "graph_builder",
        "graph_reviewer", "knowledge_validator",
    ],
    "understand-knowledge": [
        "knowledge_analyzer", "article_analyzer", "graph_builder", "knowledge_validator",
    ],
    "understand-infrastructure": [
        "infrastructure_analyzer", "graph_builder", "graph_reviewer",
    ],
    "understand-data": [
        "file_analyzer", "database_analyzer", "data_flow_analyzer",
        "graph_builder", "graph_reviewer",
    ],
    "teach": [
        "tour_builder", "teacher_agent",
    ],
}

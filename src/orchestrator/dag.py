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

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from src.agents.base_agent import AgentContext, AgentResult
from src.orchestrator.dag_runtime import (
    Emitter, Stage, make_context, run_stages, ws_emitter,
)
from src.services.ontology_version_service import create_version_record

log = logging.getLogger(__name__)

# Stage definitions: list of (stage_number, title, [agent_names], sequential)
_STAGES: list[Stage] = [
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
    emit: Emitter = ws_emitter(ws)
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
    await emit({"type": "dag_start", "runId": run_id,
                "total_stages": total_stages, "total_agents": total_agents,
                "version_id": version_id})

    def _ctx(prior: dict[str, AgentResult]) -> AgentContext:
        return make_context(intent, user_id, username, role, project_id,
                            session_id, version_id, extra, prior)

    outcome = await run_stages(_STAGES, _ctx, emit, agents_override)

    summary = {
        "run_id": run_id,
        "version_id": version_id,
        "total_nodes_added": outcome.total_nodes,
        "total_rels_added": outcome.total_rels,
        "stages_run": total_stages,
        "prior_results": {k: {"status": v.status, "output": v.output}
                          for k, v in outcome.prior_results.items()},
    }
    await emit({"type": "dag_done", "runId": run_id,
                "total_nodes_added": outcome.total_nodes,
                "total_rels_added": outcome.total_rels,
                "version_id": version_id})
    log.info("DAG run %s complete: %d nodes, %d rels",
             run_id, outcome.total_nodes, outcome.total_rels)
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

"""
Named command endpoints — /understand, /understand-domain, etc.
Each command triggers a specific subset of the AURA DAG pipeline.

POST /api/commands/{command}
  Body: {intent: string, project_id?: string, extra?: object}
  Auth: Bearer JWT — requires at minimum 'authenticated' role

WebSocket progress is NOT available on these REST endpoints; use the
WebSocket chat endpoint for streaming. These return a run summary JSON.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from src.routers.auth import get_current_user
from src.orchestrator.dag import COMMAND_AGENTS, run_dag

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/commands", tags=["commands"])

_VALID_COMMANDS = set(COMMAND_AGENTS.keys())


class CommandRequest(BaseModel):
    intent: str = Field(..., min_length=3, max_length=2000)
    project_id: str | None = None
    extra: dict = Field(default_factory=dict)
    background: bool = Field(
        default=True,
        description="Run pipeline in background (returns run_id immediately) or wait for result.",
    )


class CommandResponse(BaseModel):
    run_id: str | None = None
    command: str
    agents: list[str]
    status: str
    message: str
    result: dict | None = None


_active_runs: dict[str, dict] = {}


@router.post("/{command}", response_model=CommandResponse)
async def execute_command(
    command: str,
    request: CommandRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
) -> CommandResponse:
    """
    Execute a named AURA understanding command.

    Available commands:
    - `understand` — full pipeline (all 19 agents, 6 stages)
    - `understand-domain` — domain + business logic + knowledge pipeline
    - `understand-knowledge` — wiki/doc/policy ingestion + graph update
    - `understand-infrastructure` — IaC scan + graph update
    - `understand-data` — database + data flow analysis
    - `teach` — generate learning tours for the current graph state
    """
    if command not in _VALID_COMMANDS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown command '{command}'. Valid: {sorted(_VALID_COMMANDS)}",
        )

    agents_override = COMMAND_AGENTS[command] or None  # None = all agents
    user_id = current_user.get("userId", "unknown")
    username = current_user.get("username", "unknown")
    role = current_user.get("role", "viewer")
    session_id = current_user.get("sessionId", "cmd")

    log.info("Command /%s by %s: %s", command, username, request.intent[:80])

    if request.background:
        background_tasks.add_task(
            _run_and_store,
            command=command,
            intent=request.intent,
            user_id=user_id,
            username=username,
            role=role,
            session_id=session_id,
            project_id=request.project_id,
            extra=request.extra,
            agents_override=agents_override,
        )
        return CommandResponse(
            command=command,
            agents=COMMAND_AGENTS[command] or ["(all)"],
            status="started",
            message=f"Command /{command} started in background. "
                    f"Poll /api/commands/{command}/status for results.",
        )

    # Foreground (blocking) — suitable only for quick commands like /teach
    result = await run_dag(
        intent=request.intent,
        user_id=user_id,
        username=username,
        role=role,
        session_id=session_id,
        project_id=request.project_id,
        extra=request.extra,
        ws=None,
        command=command,
        agents_override=agents_override,
    )
    return CommandResponse(
        run_id=result.get("run_id"),
        command=command,
        agents=COMMAND_AGENTS[command] or ["(all)"],
        status="complete",
        message=f"Completed: {result.get('total_nodes_added', 0)} nodes, "
                f"{result.get('total_rels_added', 0)} relationships added.",
        result=result,
    )


async def _run_and_store(
    command: str,
    intent: str,
    user_id: str,
    username: str,
    role: str,
    session_id: str,
    project_id: str | None,
    extra: dict,
    agents_override: list[str] | None,
) -> None:
    try:
        result = await run_dag(
            intent=intent,
            user_id=user_id,
            username=username,
            role=role,
            session_id=session_id,
            project_id=project_id,
            extra=extra,
            ws=None,
            command=command,
            agents_override=agents_override,
        )
        run_id = result.get("run_id", "unknown")
        _active_runs[run_id] = {"status": "complete", **result}
        log.info("Background command /%s run %s complete", command, run_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Background command /%s failed", command)

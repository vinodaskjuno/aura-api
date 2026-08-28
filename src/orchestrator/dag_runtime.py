"""Shared DAG runtime — the parts `dag.py` and `investigation_dag.py` both need.

Extracted so that agent pipelines can run **headless** (no WebSocket), which is what
the observability investigation runner and the future eval harness both require.

The one signature change versus the old `dag._run_agent` is that a WebSocket is no
longer passed around; an `Emitter` is. `ws_emitter(ws)` reproduces the old behaviour
exactly, so `run_dag(ws=...)` keeps its public signature and every existing caller is
untouched.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from src.agents.base_agent import AgentContext, AgentResult

log = logging.getLogger(__name__)

# An emitter takes a JSON-serialisable event and delivers it somewhere.
Emitter = Callable[[dict], Awaitable[None]]

# (stage_number, title, [agent_names], sequential)
Stage = tuple[int, str, list[str], bool]


# ── Emitters ─────────────────────────────────────────────────────────────────

def ws_emitter(ws: Any | None) -> Emitter:
    """Emit to a FastAPI WebSocket. Swallows send errors exactly as `_send` did."""
    async def _emit(event: dict) -> None:
        if ws is None:
            return
        try:
            await ws.send_json(event)
        except Exception:  # noqa: BLE001 — a dead socket must never fail a run
            pass
    return _emit


def list_emitter(sink: list[dict]) -> Emitter:
    """Append every event to a list. Used by headless runs and the eval harness."""
    async def _emit(event: dict) -> None:
        sink.append(event)
    return _emit


def tee_emitter(*emitters: Emitter) -> Emitter:
    """Fan one event out to several emitters (e.g. a socket *and* a trace sink)."""
    async def _emit(event: dict) -> None:
        for em in emitters:
            await em(event)
    return _emit


def null_emitter() -> Emitter:
    async def _emit(event: dict) -> None:
        return None
    return _emit


# ── Sequence stamping ────────────────────────────────────────────────────────

class SeqEmitter:
    """Wraps an emitter and stamps a monotonic `seq` (plus optional static fields).

    The SPA reconnect protocol replays `seq > sinceSeq`, so every event on an
    investigation socket must carry one.
    """

    def __init__(self, inner: Emitter, **static: Any) -> None:
        self._inner = inner
        self._static = static
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    async def __call__(self, event: dict) -> None:
        self._seq += 1
        await self._inner({**self._static, **event, "seq": self._seq})


# ── Context ──────────────────────────────────────────────────────────────────

def make_context(
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


# ── Agent execution ──────────────────────────────────────────────────────────

@dataclass
class AgentRun:
    """Per-agent timing and outcome, captured for traces (seam S6)."""
    agent: str
    stage: int
    status: str
    started_at: str
    completed_at: str
    elapsed_ms: int
    result: AgentResult | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "stage": self.stage,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": self.elapsed_ms,
            "output": self.result.output if self.result else {},
            "activity_log": self.result.activity_log if self.result else [],
            "error": self.error,
        }


async def run_agent(
    agent_name: str,
    context: AgentContext,
    emit: Emitter,
    stage: int,
) -> AgentRun:
    """Run one agent, emitting agent_start / agent_done. Never raises."""
    from src.orchestrator.agent_registry import get_agent  # lazy — circular import

    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    await emit({"type": "agent_start", "agent": agent_name, "stage": stage})

    result: AgentResult | None = None
    error: str | None = None
    status = "failed"

    try:
        agent = get_agent(agent_name)
        if agent is None:
            raise LookupError(f"agent '{agent_name}' is not registered")
        result = await agent.run(context)
        status = result.status
    except Exception as exc:  # noqa: BLE001
        log.exception("Agent %s failed", agent_name)
        error = str(exc)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    completed = datetime.now(timezone.utc)

    done: dict[str, Any] = {
        "type": "agent_done",
        "agent": agent_name,
        "stage": stage,
        "status": status,
        "elapsed_ms": elapsed_ms,
    }
    if result is not None:
        out = result.output
        done["nodes_added"] = out.get("nodes_added", 0)
        done["rels_added"] = out.get("rels_added", 0)
        # Observability additions — harmless zeros for ontology agents.
        done["evidence_added"] = out.get("evidence_added", 0)
        done["costDelta"] = out.get("cost_usd", 0.0)
    if error:
        done["error"] = error
    await emit(done)

    return AgentRun(
        agent=agent_name,
        stage=stage,
        status=status,
        started_at=started.isoformat(),
        completed_at=completed.isoformat(),
        elapsed_ms=elapsed_ms,
        result=result,
        error=error,
    )


# ── Stage execution ──────────────────────────────────────────────────────────

@dataclass
class StagesOutcome:
    prior_results: dict[str, AgentResult] = field(default_factory=dict)
    runs: list[AgentRun] = field(default_factory=list)
    stages_run: int = 0
    total_nodes: int = 0
    total_rels: int = 0


async def run_stages(
    stages: list[Stage],
    ctx_factory: Callable[[dict[str, AgentResult]], AgentContext],
    emit: Emitter,
    agents_override: list[str] | None = None,
    prior: dict[str, AgentResult] | None = None,
) -> StagesOutcome:
    """Execute a stage list, honouring per-stage sequential/parallel semantics.

    `agents_override` filters *within* stages; a stage with no overlap is skipped.
    """
    outcome = StagesOutcome(prior_results=dict(prior or {}))

    for stage_num, title, agent_names, sequential in stages:
        if agents_override is not None:
            active = [a for a in agent_names if a in agents_override]
            if not active:
                log.debug("Stage %d skipped — no overlap with agent override", stage_num)
                continue
        else:
            active = list(agent_names)

        t0 = time.monotonic()
        await emit({"type": "stage_start", "stage": stage_num,
                    "title": title, "agents": active})
        log.info("Stage %d (%s): %s", stage_num, title, active)

        if sequential:
            for name in active:
                ctx = ctx_factory(outcome.prior_results)
                run = await run_agent(name, ctx, emit, stage_num)
                _absorb(outcome, name, run)
        else:
            ctx = ctx_factory(outcome.prior_results)
            runs = await asyncio.gather(
                *[run_agent(name, ctx, emit, stage_num) for name in active]
            )
            for name, run in zip(active, runs):
                _absorb(outcome, name, run)

        outcome.stages_run += 1
        await emit({"type": "stage_done", "stage": stage_num, "title": title,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000)})

    return outcome


def _absorb(outcome: StagesOutcome, name: str, run: AgentRun) -> None:
    outcome.runs.append(run)
    if run.result is not None:
        outcome.prior_results[name] = run.result
        outcome.total_nodes += run.result.output.get("nodes_added", 0)
        outcome.total_rels += run.result.output.get("rels_added", 0)

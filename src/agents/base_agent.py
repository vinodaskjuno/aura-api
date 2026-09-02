"""Base contracts for all AURA agents."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal
import uuid
from datetime import datetime, timezone


@dataclass
class S3Ref:
    bucket: str
    key: str
    uri: str  # s3://bucket/key


@dataclass
class Triple:
    subject: str
    predicate: str
    obj: str


@dataclass
class AgentContext:
    user_id: str
    username: str
    role: str
    intent: str
    project_id: str | None = None
    session_id: str | None = None
    prior_results: dict[str, "AgentResult"] = field(default_factory=dict)
    kg_snapshot: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    # Distributed-trace context, threaded down the orchestrator DAG so a multi-agent
    # run renders as ONE trace instead of N unrelated ones. Empty when Opik is off.
    # These are Opik ids (UUIDv7), not OTel ids — Opik mints its own and does not
    # preserve a 128-bit OTel trace id on ingest.
    opik_trace_id: str = ""
    opik_parent_span_id: str = ""
    # Groups related runs in the traces view. Aura maps session_id onto it so a whole
    # chat conversation reads as one thread.
    opik_thread_id: str = ""


@dataclass
class AgentResult:
    agent_name: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: Literal["success", "partial", "failed"] = "success"
    output: dict[str, Any] = field(default_factory=dict)
    kg_updates: list[Triple] = field(default_factory=list)
    artifacts: list[S3Ref] = field(default_factory=list)
    activity_log: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None
    # The trace this agent's work landed in, so the orchestrator can pass it to the
    # next agent and the UI can deep-link a DAG node straight to its spans.
    opik_trace_id: str = ""
    opik_span_id: str = ""

    def finish(self, status: Literal["success", "partial", "failed"] = "success") -> "AgentResult":
        self.status = status
        self.completed_at = datetime.now(timezone.utc).isoformat()
        return self

    def log(self, msg: str) -> None:
        self.activity_log.append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}")


class BaseAgent(ABC):
    name: str = "base"
    description: str = "Base agent"

    @abstractmethod
    async def run(self, context: AgentContext) -> AgentResult:
        """Execute the agent. Must return an AgentResult."""
        ...

    def _result(self, context: AgentContext) -> AgentResult:
        # Carries the incoming trace id forward by default, so an agent that never
        # touches Opik still reports which trace it belonged to.
        return AgentResult(agent_name=self.name,
                           opik_trace_id=context.opik_trace_id)

    def llm_kwargs(self, context: AgentContext) -> dict[str, str]:
        """Trace-context kwargs to splat into `observability.llm.invoke_masked`.

        Exists so the 45 agents do not each hand-copy four field names:

            text, rec = await invoke_masked(system=..., user=...,
                                            investigation_id=..., agent=self.name,
                                            **self.llm_kwargs(context))
        """
        return {"opik_trace_id": context.opik_trace_id,
                "opik_parent_span_id": context.opik_parent_span_id,
                "opik_thread_id": context.opik_thread_id or (context.session_id or "")}

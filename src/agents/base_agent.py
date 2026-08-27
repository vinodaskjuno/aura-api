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
        return AgentResult(agent_name=self.name)

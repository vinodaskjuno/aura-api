"""Pydantic models for the observability surface.

`SOPStep` is imported from models/sop.py rather than redefined so the SPA can reuse
the existing SOP rendering components. `SOPDocument.stage` is deliberately NOT
widened — it is a closed Literal keyed by STAGE_META and routers/sop.py::_can_approve,
and adding a value ripples into approval logic.
"""
from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

from src.models.sop import SOPChecklistItem, SOPStep  # noqa: F401  (re-exported)

Verdict = Literal["confirmed", "wrong", "partial", "unknown"]
FindingStatus = Literal["root_cause", "supported", "refuted", "unsupported"]
RunbookOrigin = Literal["human", "confluence", "git", "synthesized", "template"]
RunbookStatus = Literal["active", "candidate", "archived"]


class StartInvestigationRequest(BaseModel):
    title: str = ""
    services: List[str] = Field(default_factory=list)
    symptom: str = ""
    start: str = ""
    end: str = ""
    window_minutes: int = 60
    incident_id: str = ""
    severity: str = "high"
    provider_ids: List[str] = Field(default_factory=list)
    runbook_id: str = ""
    project_id: str = ""
    filter: str = ""
    masking_enabled: bool = True
    background: bool = True


class Finding(BaseModel):
    findingId: str
    rank: int = 0
    claim: str
    confidence: float = 0.0
    status: FindingStatus = "supported"
    evidenceIds: List[str] = Field(default_factory=list)
    agent: str = ""
    category: str = ""
    runbookStepId: str = ""
    caseIds: List[str] = Field(default_factory=list)   # priors, NEVER citations
    createdAt: str = ""


class OutcomeRequest(BaseModel):
    verdict: Verdict
    actual_cause: str = ""
    actual_category: str = ""
    note: str = ""


class RunbookDoc(BaseModel):
    runbookId: str
    title: str
    origin: RunbookOrigin = "human"
    status: RunbookStatus = "active"
    sourceType: str = ""
    sourceUrl: str = ""
    s3Uri: str = ""
    bodySnippet: str = ""
    tags: List[str] = Field(default_factory=list)
    services: List[str] = Field(default_factory=list)
    alertSignatures: List[str] = Field(default_factory=list)
    severity: str = ""
    steps: List[SOPStep] = Field(default_factory=list)
    confirmedCount: int = 0
    lastConfirmedAt: str = ""
    sourceInvestigations: List[str] = Field(default_factory=list)
    createdAt: str = ""


class RunbookCreate(BaseModel):
    title: str
    body: str = ""
    services: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    alertSignatures: List[str] = Field(default_factory=list)
    severity: str = "high"
    steps: List[dict] = Field(default_factory=list)
    sourceUrl: str = ""


class RunbookMatch(BaseModel):
    runbookId: str
    title: str
    score: float
    origin: RunbookOrigin = "human"
    matchedOn: List[str] = Field(default_factory=list)
    sourceUrl: str = ""


class InvestigationRecord(BaseModel):
    investigationId: str
    createdAt: str
    projectId: str = ""
    serviceName: str = ""
    title: str = ""
    status: Literal["queued", "running", "complete", "failed"] = "queued"
    severity: str = "high"
    services: List[str] = Field(default_factory=list)
    incidentId: str = ""
    runId: str = ""
    window: dict = Field(default_factory=dict)
    findings: List[dict] = Field(default_factory=list)
    rootCause: Optional[dict] = None
    evidenceCount: int = 0
    citationCoverage: float = 0.0
    cost: dict = Field(default_factory=dict)
    masking: dict = Field(default_factory=dict)
    outcome: Optional[dict] = None
    runbookId: str = ""
    caseCount: int = 0
    startedAt: str = ""
    completedAt: str = ""
    userId: str = ""


class NotificationTestRequest(BaseModel):
    channel: str = "slack"
    message: str = "Aura observability test notification"

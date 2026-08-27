"""Canonical AURA Enterprise Technology Ontology — node labels and relationship types.

Import this module as the single source of truth for all label/relationship names
used across Neo4j DDL, agent writers, graph queries, and frontend type maps.
"""
from __future__ import annotations

# ── Node label categories ────────────────────────────────────────────────────

ENTERPRISE_LABELS = [
    "Enterprise", "Organization", "BusinessUnit", "BusinessDomain", "Product",
]

BUSINESS_LABELS = [
    "BusinessProcess", "BusinessRule", "BusinessApplication",
    "Requirement", "Policy", "SOP",
]

DOCUMENT_LABELS = [
    "Document", "WikiArticle", "ADR", "TechnicalSpec", "Runbook",
]

TICKET_LABELS = ["Ticket"]

APPLICATION_LABELS = [
    "Application", "Service", "API", "Module", "Class", "Function", "Feature",
]

CODE_LABELS = [
    "Repository", "CodeFile", "Dependency", "Configuration", "FeatureFlag",
]

PIPELINE_LABELS = [
    "BuildPipeline", "BuildArtifact", "Deployment", "DeploymentEnvironment",
]

DATA_LABELS = [
    "Database", "Table", "Column", "DataElement", "DataFlow",
]

INFRASTRUCTURE_LABELS = [
    "Server", "VM", "Container", "KubernetesCluster", "CloudResource", "Network",
]

SECURITY_LABELS = [
    "SecurityFinding", "Vulnerability", "AttackPath", "IAMRole", "IAMPolicy",
]

IDENTITY_LABELS = ["User", "Team", "Role", "ServiceAccount"]

OPERATIONS_LABELS = ["Incident", "Alert", "ChangeRequest"]

AI_LABELS = [
    "AIModel", "PromptRepository", "RAGKnowledgeBase",
    "VectorDatabase", "AgentDefinition", "MCPServer",
]

# Existing labels — kept for backward compat
LEGACY_LABELS = ["Project", "Infrastructure", "AuditLog"]

ALL_LABELS: list[str] = (
    ENTERPRISE_LABELS
    + BUSINESS_LABELS
    + DOCUMENT_LABELS
    + TICKET_LABELS
    + APPLICATION_LABELS
    + CODE_LABELS
    + PIPELINE_LABELS
    + DATA_LABELS
    + INFRASTRUCTURE_LABELS
    + SECURITY_LABELS
    + IDENTITY_LABELS
    + OPERATIONS_LABELS
    + AI_LABELS
    + LEGACY_LABELS
)

# ── Relationship types ───────────────────────────────────────────────────────

RELATIONSHIP_TYPES: list[str] = [
    # Code → Repository
    "COMMITTED_TO",
    # Repository → BuildPipeline
    "BUILT_BY",
    # BuildPipeline → BuildArtifact
    "PRODUCES",
    # BuildArtifact → DeploymentEnvironment
    "DEPLOYED_TO",
    # Service/Container → CloudResource/VM/Container
    "RUNS_ON",
    # Service/API → Network
    "EXPOSED_VIA",
    # Service → IAMRole/ServiceAccount
    "ACCESSES_AS",
    # Service/Infra → SecurityFinding/Vulnerability
    "HAS_FINDING",
    # Service → Database/DataFlow
    "CONNECTS_TO",
    # Service/Application → BusinessApplication
    "IMPLEMENTS",
    # Service/Application/Repo → Team/User
    "OWNED_BY",
    # Service/Infra → Incident
    "HAS_INCIDENT",
    # Incident/SecurityFinding → ChangeRequest/CodeFile
    "REMEDIATED_BY",
    # Generic containment
    "BELONGS_TO", "PART_OF", "CONTAINS",
    # Dependency
    "DEPENDS_ON", "CALLS", "IMPORTS", "EXTENDS",
    # Business
    "SUPPORTS_PROCESS", "IMPLEMENTS_BUSINESS_RULE", "GOVERNED_BY",
    # Documents
    "DOCUMENTS", "REFERENCED_BY",
    # Infra
    "HOSTS", "ROUTES_TO",
    # Audit
    "AUDITED_BY",
]

# ── Provenance property schema (applied to every relationship) ───────────────

PROVENANCE_PROPS = {
    "source": str,           # git | servicenow | wiz | confluence | manual | ai_inferred
    "sourceRecordId": str,   # external record id in source system
    "discoveredBy": str,     # agent name that created this relationship
    "confidence": float,     # 0.0–1.0
    "firstSeen": str,        # ISO datetime
    "lastSeen": str,         # ISO datetime
    "evidence": list,        # ["file:line", "url", "audit_id"]
    "factType": str,         # "known" | "inferred" | "hypothesis"
}

# ── Source systems ───────────────────────────────────────────────────────────

KNOWN_SOURCES = [
    "git", "servicenow", "wiz", "confluence", "jira",
    "terraform", "aws", "azure", "gcp", "kubernetes",
    "snyk", "crowdstrike", "okta", "entra_id", "cyberark",
    "datadog", "pagerduty", "splunk", "manual", "ai_inferred",
    "file_upload", "mcp_connector", "api_ingest", "scheduler",
]

FACT_TYPES = ["known", "inferred", "hypothesis"]

# ── Node tier for App↔Infra view (higher = higher in visual hierarchy) ────────

NODE_TIER: dict[str, int] = {
    "BusinessApplication": 0,
    "BusinessDomain": 0,
    "BusinessProcess": 0,
    "Application": 1,
    "Service": 1,
    "API": 2,
    "Module": 2,
    "Feature": 2,
    "Container": 3,
    "VM": 3,
    "DeploymentEnvironment": 3,
    "KubernetesCluster": 3,
    "CloudResource": 4,
    "Network": 4,
    "IAMRole": 4,
    "IAMPolicy": 4,
    "Database": 5,
    "DataFlow": 5,
    "SecurityFinding": 5,
    "Vulnerability": 5,
}

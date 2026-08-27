"""SOP (Standard Operating Procedure) data models."""
from __future__ import annotations
from typing import Literal, List
from pydantic import BaseModel


class SOPChecklistItem(BaseModel):
    id: str
    text: str
    completed: bool = False


class SOPStep(BaseModel):
    id: str
    order: int
    title: str
    description: str
    checklist: List[SOPChecklistItem] = []
    status: Literal["pending", "in_progress", "completed", "skipped"] = "pending"
    notes: str = ""
    autoDetected: bool = False   # True when populated from KG / agent data


class SOPDocument(BaseModel):
    sopId: str
    projectId: str
    stage: Literal["dev", "qa", "aiops", "reverse_engineering"]
    title: str
    summary: str
    steps: List[SOPStep] = []
    status: Literal["draft", "in_review", "approved"] = "draft"
    approvedBy: str = ""
    approvedAt: str = ""
    generatedAt: str = ""
    exportedAt: str = ""


# ── Stage meta — labels, icons, required roles for approval ──────────────────

STAGE_META: dict[str, dict] = {
    "dev": {
        "label":       "Dev Workspace",
        "icon":        "Code2",
        "approveRoles":["admin", "super_admin"],
        "color":       "#4f8ef7",
    },
    "qa": {
        "label":       "QA / Testing",
        "icon":        "TestTube2",
        "approveRoles":["user_qa", "admin", "super_admin"],
        "color":       "#10b981",
    },
    "aiops": {
        "label":       "AI Ops",
        "icon":        "Activity",
        "approveRoles":["user_ops", "admin", "super_admin"],
        "color":       "#f59e0b",
    },
    "reverse_engineering": {
        "label":       "Reverse Engineering",
        "icon":        "GitGraph",
        "approveRoles":["admin", "super_admin"],
        "color":       "#8b5cf6",
    },
}


# ── Template steps (used when Bedrock unavailable) ────────────────────────────

TEMPLATE_STEPS: dict[str, list[dict]] = {
    "dev": [
        {"title": "Repository Setup",
         "description": "Connect all repositories and verify paths are accessible.",
         "checklist": ["Repos connected", "Local paths verified", "Tokens configured"]},
        {"title": "Code Analysis",
         "description": "Run CodeAnalysisAgent to detect tech stack, services, and languages.",
         "checklist": ["Analysis completed", "Tech stack reviewed", "Service list confirmed"]},
        {"title": "Knowledge Graph Review",
         "description": "Review the Ontology Graph and verify service correlations.",
         "checklist": ["Ontology graph reviewed", "Correlations validated", "Mind map checked"]},
        {"title": "Security Assessment",
         "description": "Run SecurityAgent and review CVE findings.",
         "checklist": ["SecurityAgent executed", "CVE scan reviewed", "OWASP Top 10 checked"]},
        {"title": "Architecture Review",
         "description": "Review Knowledge Diagram and confirm dependency map.",
         "checklist": ["Architecture diagram reviewed", "Dependencies confirmed", "No circular deps"]},
        {"title": "Technical Sign-off",
         "description": "Lead developer or architect approves the analysis.",
         "checklist": ["Reviewed by architect", "Sign-off obtained", "Documentation updated"]},
    ],
    "qa": [
        {"title": "Dev Analysis Prerequisite",
         "description": "Verify that Dev SOP is approved before QA begins.",
         "checklist": ["Dev SOP status = Approved", "Knowledge graph available"]},
        {"title": "Test Plan",
         "description": "Select test types: Playwright, API, Integration, Regression, Negative, Boundary.",
         "checklist": ["Test types selected", "Coverage target set", "Focus areas defined"]},
        {"title": "Test Generation",
         "description": "Run TestGenerationAgent and save artifacts to S3.",
         "checklist": ["Tests generated", "Artifacts saved to S3", "Test files reviewed"]},
        {"title": "Test Execution",
         "description": "Execute generated suites and store results in DynamoDB.",
         "checklist": ["All suites executed", "Results stored", "Execution log reviewed"]},
        {"title": "Coverage Review",
         "description": "Review per-class coverage and confirm it meets the target.",
         "checklist": ["Coverage ≥ target %", "Low-coverage files identified", "Gap tests created"]},
        {"title": "Defect Triage",
         "description": "Log and assign all failed tests.",
         "checklist": ["Failures logged in Jira", "Priority assigned", "Owners notified"]},
        {"title": "QA Sign-off",
         "description": "QA lead approves the test run.",
         "checklist": ["QA lead review complete", "Sign-off obtained", "Test report exported"]},
    ],
    "aiops": [
        {"title": "Alert Acknowledgement",
         "description": "Acknowledge the alert and classify its severity.",
         "checklist": ["Alert acknowledged", "Severity classified", "On-call notified"]},
        {"title": "Initial Diagnosis",
         "description": "Review CloudWatch metrics and recent logs.",
         "checklist": ["CloudWatch dashboard checked", "Logs reviewed", "Baseline compared"]},
        {"title": "Root Cause Analysis",
         "description": "Run RCAAgent to identify the root cause and affected services.",
         "checklist": ["RCAAgent executed", "Root cause identified", "Affected services listed"]},
        {"title": "Remediation Plan",
         "description": "Document the fix steps and assign owners.",
         "checklist": ["Fix steps documented", "Owners assigned", "ETA confirmed"]},
        {"title": "Fix Implementation",
         "description": "Apply the code change, config update, or scaling action.",
         "checklist": ["Fix applied", "PR reviewed", "Change deployed"]},
        {"title": "Verification",
         "description": "Confirm the alert is resolved and metrics are normal.",
         "checklist": ["Alert resolved", "Metrics normalized", "No downstream impact"]},
        {"title": "Post-Incident Review",
         "description": "Document timeline and create action items.",
         "checklist": ["Timeline documented", "Action items in Jira", "Post-mortem scheduled"]},
    ],
    "reverse_engineering": [
        {"title": "Source Acquisition",
         "description": "Connect legacy repos (COBOL, Java, Mule, mainframe).",
         "checklist": ["Repos connected", "Source accessible", "Version confirmed"]},
        {"title": "Initial Scan",
         "description": "Run ReverseEngineeringAgent to extract structure.",
         "checklist": ["Scan completed", "Classes extracted", "Dependencies mapped"]},
        {"title": "Dependency Mapping",
         "description": "Review correlations and confirm N-tier architecture.",
         "checklist": ["Correlations reviewed", "N-tier confirmed", "External deps listed"]},
        {"title": "Legacy Pattern Identification",
         "description": "Identify anti-patterns, technical debt, and upgrade blockers.",
         "checklist": ["Anti-patterns documented", "Debt items logged", "Blockers identified"]},
        {"title": "Knowledge Graph Validation",
         "description": "Validate entities and relationships in Neptune.",
         "checklist": ["All entities present", "Relationships verified", "Graph exported"]},
        {"title": "Modernization Roadmap",
         "description": "Assign 6R dispositions (Rehost, Replatform, Refactor, etc.) per service.",
         "checklist": ["6R assigned", "Priority order set", "Effort estimated"]},
        {"title": "Architecture Sign-off",
         "description": "Architect approves the reverse-engineered blueprint.",
         "checklist": ["Architect review complete", "Blueprint approved", "Handed to Dev team"]},
    ],
}

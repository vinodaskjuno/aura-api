"""Projects API — DynamoDB-backed project management for Dev Workspace."""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.routers.auth import get_current_user, require_permission
from src.database.dynamo_client import put_item, get_item, get_item_by_pk, query_items, update_item, delete_item, scan_items
from src.storage.s3_client import get_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Models ────────────────────────────────────────────────────────────────────

class RepoConfig(BaseModel):
    repoType: str           # ui | backend | mule | infra | config | other
    sourceType: str = "git" # git | local
    repoUrl: str = ""       # used when sourceType == "git"
    localPath: str = ""     # used when sourceType == "local"
    token: str = ""
    branch: str = "main"


class ConnectorConfig(BaseModel):
    connectorType: str  # git | mcp | sql | rest
    name: str
    repoUrl: str = ""
    endpoint: str = ""
    token: str = ""
    repoType: str = ""


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""
    environment: str = "development"
    repos: list[RepoConfig] = []
    mcpEndpoints: list[str] = []


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    environment: str | None = None
    # Valid status values: pending | analyzing | analyzed | error | generating | ready
    status: str | None = None
    # Git/PR workflow fields
    prUrl: str | None = None
    clonedPath: str | None = None
    codeChangesSummary: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_projects(user: dict = Depends(require_permission("dev_workspace"))):
    try:
        all_projects = scan_items("projects", limit=1000)
        user_projects = [p for p in all_projects if p.get("userId") == user["userId"]]

        if user_projects:
            project_ids = {p["projectId"] for p in user_projects}
            repos_by_project: dict = {}

            # 1. Repos from connectors table (created via project creation form)
            for c in scan_items("connectors", limit=500):
                pid = c.get("projectId")
                if pid in project_ids and c.get("repoUrl", "").strip():
                    repos_by_project.setdefault(pid, []).append({
                        "repoUrl":     c.get("repoUrl", ""),
                        "branch":      c.get("branch", "main"),
                        "repoType":    c.get("repoType", ""),
                        "sourceType":  c.get("sourceType", "git"),
                        "localPath":   c.get("localPath", ""),
                        "name":        c.get("repoType", "") or "repo",
                        "serviceName": "",
                    })

            # 2. Repos from services table (created via Data Loader)
            for svc in scan_items("services", limit=1000):
                pid = svc.get("projectId")
                if pid in project_ids:
                    for repo in svc.get("repos", []):
                        if repo.get("repoUrl", "").strip():
                            repos_by_project.setdefault(pid, []).append({
                                "repoUrl":     repo.get("repoUrl", ""),
                                "branch":      repo.get("branch", "main"),
                                "repoType":    repo.get("repoType", ""),
                                "sourceType":  "git",
                                "name":        repo.get("name", "") or repo.get("repoUrl", "").split("/")[-1],
                                "serviceName": svc.get("name", ""),
                            })

            for p in user_projects:
                p["repos"] = repos_by_project.get(p["projectId"], [])

        return user_projects
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
def create_project(req: CreateProjectRequest, user: dict = Depends(require_permission("dev_workspace"))):
    project_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    project = {
        "projectId": project_id,
        "userId": user["userId"],
        "username": user["username"],
        "name": req.name,
        "description": req.description,
        "environment": req.environment,
        "status": "pending",
        "createdAt": now,
        "updatedAt": now,
        "repoCount": len(req.repos),
        "mcpEndpoints": req.mcpEndpoints,
    }
    put_item("projects", project)

    # Store each repo as a connector record
    for repo in req.repos:
        connector_id = str(uuid.uuid4())
        put_item("connectors", {
            "connectorId": connector_id,
            "projectId": project_id,
            "connectorType": "git",
            "sourceType": repo.sourceType,
            "repoType": repo.repoType,
            "repoUrl": repo.repoUrl,
            "branch": repo.branch,
            "localPath": repo.localPath,
            "hasToken": bool(repo.token),
            "createdAt": now,
        })

    return project


@router.get("/{project_id}")
def get_project(project_id: str, user: dict = Depends(require_permission("dev_workspace"))):
    project = get_item_by_pk("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.put("/{project_id}")
def update_project(project_id: str, req: UpdateProjectRequest,
                   user: dict = Depends(require_permission("dev_workspace"))):
    project = get_item_by_pk("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    updates = req.model_dump(exclude_none=True)
    updates["updatedAt"] = datetime.now(timezone.utc).isoformat()
    result = update_item("projects", {"projectId": project_id, "userId": project["userId"]}, updates)
    return result or project


@router.delete("/{project_id}")
def delete_project(project_id: str, user: dict = Depends(require_permission("dev_workspace"))):
    # Need full composite key (projectId + userId) to delete
    project = get_item_by_pk("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    delete_item("projects", {"projectId": project_id, "userId": project["userId"]})
    return {"ok": True}


@router.get("/{project_id}/connectors")
def get_project_connectors(project_id: str, user: dict = Depends(require_permission("dev_workspace"))):
    try:
        all_connectors = scan_items("connectors", limit=500)
        return [c for c in all_connectors if c.get("projectId") == project_id]
    except Exception:
        return []


@router.post("/{project_id}/connectors")
def add_project_connector(project_id: str, repo: RepoConfig,
                          user: dict = Depends(require_permission("dev_workspace"))):
    """Add a git connector (repo URL) to an existing project."""
    project = get_item_by_pk("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    now = datetime.now(timezone.utc).isoformat()
    connector_id = str(uuid.uuid4())
    put_item("connectors", {
        "connectorId": connector_id,
        "projectId": project_id,
        "connectorType": "git",
        "sourceType": repo.sourceType,
        "repoType": repo.repoType,
        "repoUrl": repo.repoUrl,
        "branch": repo.branch,
        "localPath": repo.localPath,
        "hasToken": bool(repo.token),
        "createdAt": now,
    })
    # Update repoCount on the project
    composite_key = {"projectId": project_id, "userId": project["userId"]}
    existing = scan_items("connectors", limit=500)
    count = len([c for c in existing if c.get("projectId") == project_id])
    update_item("projects", composite_key, {"repoCount": count, "updatedAt": now})
    return {"connectorId": connector_id, "ok": True}


@router.get("/{project_id}/knowledge-graph")
def get_knowledge_graph(project_id: str, user: dict = Depends(require_permission("dev_workspace"))):
    """Return knowledge graph JSON.
    Priority: S3 → DynamoDB knowledgeGraph field → DynamoDB techStack/services fields → empty.
    """
    import json as _json

    # 1. Try S3
    try:
        data = get_json("analysis", f"{project_id}/knowledge_graph.json")
        if data:
            return data
    except Exception:
        pass

    # 2. DynamoDB embedded KG (set after analysis or for sample projects)
    project = get_item_by_pk("projects", project_id)
    if project:
        if project.get("knowledgeGraph"):
            try:
                kg = _json.loads(project["knowledgeGraph"])
                # Backfill code section from flat fields if missing
                if not kg.get("code") or not kg["code"].get("tech_stack"):
                    kg.setdefault("code", {})
                    kg["code"].setdefault("tech_stack", project.get("techStack", []))
                    kg["code"].setdefault("services",   project.get("services",  []))
                    kg["code"].setdefault("languages",  project.get("languages", []))
                return kg
            except Exception:
                pass

        # 3. Build minimal KG from flat fields written by analyse_project
        tech_stack = project.get("techStack", [])
        services   = project.get("services",  [])
        languages  = project.get("languages", [])
        if tech_stack or services or languages:
            return {
                "project_id": project_id,
                "code": {
                    "tech_stack": tech_stack,
                    "services":   services,
                    "languages":  languages if isinstance(languages, list) else list(languages),
                },
                "infra":        [],
                "db_servers":   [],
                "correlations": [],
            }

    return {"project_id": project_id, "code": {"tech_stack": [], "services": [], "languages": []},
            "infra": [], "db_servers": [], "correlations": []}


@router.get("/{project_id}/activity")
def get_project_activity(project_id: str, user: dict = Depends(require_permission("dev_workspace"))):
    """Return activity history for this project from DynamoDB."""
    try:
        all_activity = scan_items("activity", limit=500)
        project_activity = [
            a for a in all_activity
            if a.get("projectId") == project_id or a.get("userId") == user["userId"]
        ]
        project_activity.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return project_activity[:100]
    except Exception:
        return []


@router.post("/{project_id}/analyse")
async def analyse_project(project_id: str, user: dict = Depends(require_permission("dev_workspace"))):
    """Trigger CodeAnalysisAgent + ReverseEngineeringAgent + KnowledgeGraphAgent."""
    from src.agents.base_agent import AgentContext
    from src.orchestrator.agent_registry import get_agent
    from src.graph import provenance

    # Fetch project to get full composite key
    project = get_item_by_pk("projects", project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    composite_key = {"projectId": project_id, "userId": project["userId"]}
    update_item("projects", composite_key, {
        "status": "analyzing",
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    })

    context = AgentContext(
        user_id=user["userId"],
        username=user["username"],
        role=user["role"],
        intent=f"Analyse project {project_id}",
        project_id=project_id,
        session_id=str(uuid.uuid4()),
    )

    # One run covers the whole analysis — the three agents and the graph
    # projection alike. They are one user action and one answer to "where did this
    # come from", so splitting them into separate runs would fragment the trace of
    # a single click into four unrelated entries.
    with provenance.trace_run(
        provenance.PIPELINE_DEV_MATE,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"],
        actorId=user["userId"],
        source="code-analysis",
        sourceDetail=project.get("name") or project_id,
        projectId=project_id,
        sessionId=context.session_id,
        writtenBy="projects.analyse_project",
        notes="Project analysis: code + reverse engineering + knowledge graph",
    ):
        results = {}
        for agent_name in ["code_analysis_agent", "reverse_engineering_agent", "knowledge_graph_agent"]:
            agent = get_agent(agent_name)
            if agent:
                try:
                    res = await agent.run(context)
                    results[agent_name] = {"status": res.status, "log": res.activity_log[-3:]}
                    context.prior_results[agent_name] = res
                except Exception as exc:
                    results[agent_name] = {"status": "failed", "error": str(exc)}

        # ── Persist analysis results back to DynamoDB so Overview populates ──────
        import json as _json

        # Load existing project data — we only OVERWRITE if new analysis has MORE data
        existing_services  = project.get("services", [])
        existing_tech      = project.get("techStack", [])
        existing_languages = project.get("languages", [])
        existing_kg_raw    = project.get("knowledgeGraph", "")

        final_updates: dict = {
            "status": "analyzed",
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }

        # Code analysis → tech stack, services, languages
        # Only overwrite if new results are non-empty (prevent wiping good data)
        code_res = context.prior_results.get("code_analysis_agent")
        new_services  = []
        new_tech      = []
        new_languages = []
        if code_res and code_res.output:
            out = code_res.output
            new_tech      = list(set(out.get("tech_stack", [])))
            new_services  = list(set(out.get("services", [])))
            new_languages = list(out.get("languages", {}).keys()) if isinstance(out.get("languages"), dict) else list(out.get("languages", []))

        # Merge: use new if non-empty, otherwise keep existing
        final_updates["techStack"] = new_tech      if new_tech      else existing_tech
        final_updates["services"]  = new_services  if new_services  else existing_services
        final_updates["languages"] = new_languages if new_languages else existing_languages

        # Reverse engineering → knowledge graph
        re_res = context.prior_results.get("reverse_engineering_agent")
        if re_res and re_res.output:
            kg = re_res.output

            # Ensure services in KG come from code analysis, not fake Bedrock items
            all_known_services = set(final_updates["services"])
            if kg.get("correlations"):
                # Filter out correlations where from/to are language or tech items, not real services
                all_langs_and_tech = set(
                    [l.lower() for l in final_updates["languages"]] +
                    [t.lower() for t in final_updates["techStack"]] +
                    ["yaml", "typescript", "python", "mule", "javascript", "java", "node.js", "node"]
                )
                clean_correlations = [
                    c for c in kg["correlations"]
                    if c.get("from") and c.get("to")
                    and c.get("from", "").lower() not in all_langs_and_tech
                    and c.get("to", "").lower() not in all_langs_and_tech
                    and "|" not in c.get("relationship", "")
                    and len(c.get("relationship", "")) < 50
                ]
                kg["correlations"] = clean_correlations

            # Inject confirmed services and code data into KG
            kg.setdefault("code", {})
            kg["code"]["tech_stack"] = final_updates["techStack"]
            kg["code"]["services"]   = final_updates["services"]
            kg["code"]["languages"]  = final_updates["languages"]

            # Only save KG if it has real content (services or correlations)
            has_real_content = (
                len(final_updates["services"]) > 0
                or len(kg.get("correlations", [])) > 0
                or len(kg.get("infra", [])) > 0
            )
            if has_real_content:
                final_updates["knowledgeGraph"] = _json.dumps(kg)
            elif existing_kg_raw:
                # Keep existing KG if new one has no content
                pass
        elif existing_kg_raw and final_updates["services"]:
            # No RE result but we have services — update the existing KG's code section
            try:
                existing_kg = _json.loads(existing_kg_raw)
                existing_kg.setdefault("code", {})
                existing_kg["code"]["tech_stack"] = final_updates["techStack"]
                existing_kg["code"]["services"]   = final_updates["services"]
                existing_kg["code"]["languages"]  = final_updates["languages"]
                final_updates["knowledgeGraph"] = _json.dumps(existing_kg)
            except Exception:
                pass

        update_item("projects", composite_key, final_updates)

        # ── Project the analysed code into Neo4j ────────────────────────────────
        # Deliberately after the DynamoDB write and wrapped: the graph is a projection
        # of the analysis, so Neo4j being unreachable must leave the analysis itself
        # intact rather than failing the request.
        graph_report: dict = {}
        try:
            from src.graph import code_graph
            connectors = [c for c in scan_items("connectors", limit=500)
                          if c.get("projectId") == project_id]
            connectors += [c for c in scan_items("user-connectors", limit=500)
                           if c.get("projectId") == project_id]
            graph_report = code_graph.sync_project(
                {**project, **final_updates, "projectId": project_id},
                connectors,
                actor=user["username"],
            ).as_dict()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j sync failed for %s: %s", project_id, exc)
            graph_report = {"errors": [str(exc)]}

        return {"project_id": project_id, "results": results,
                "graph": graph_report,
                "summary": {
                    "services":     len(final_updates["services"]),
                    "techStack":    len(final_updates["techStack"]),
                    "languages":    len(final_updates["languages"]),
                    "correlations": len(_json.loads(final_updates.get("knowledgeGraph","{}") or "{}").get("correlations", [])),
                }}


# ── Sample project seeder ─────────────────────────────────────────────────────

SAMPLE_PROJECTS = [
    {
        "name": "Payments Service",
        "description": "Core payment processing microservice — Java Spring Boot + React frontend + AWS infrastructure",
        "environment": "production",
        "repos": [
            {"repoType": "ui",      "sourceType": "git", "repoUrl": "https://github.com/aura-demo/payments-ui",      "branch": "main"},
            {"repoType": "backend", "sourceType": "git", "repoUrl": "https://github.com/aura-demo/payments-api",     "branch": "main"},
            {"repoType": "mule",    "sourceType": "git", "repoUrl": "https://github.com/aura-demo/payments-mule",    "branch": "develop"},
            {"repoType": "infra",   "sourceType": "git", "repoUrl": "https://github.com/aura-demo/payments-infra",   "branch": "main"},
            {"repoType": "config",  "sourceType": "git", "repoUrl": "https://github.com/aura-demo/payments-config",  "branch": "main"},
        ],
        "knowledge_graph": {
            "code": {
                "languages": ["Java", "TypeScript", "XML"],
                "services": ["PaymentService", "UserService", "NotificationService", "AuditService", "OrderService"],
                "tech_stack": ["Java 17", "Spring Boot 3", "React 18", "Mule 4", "PostgreSQL", "DynamoDB", "AWS Lambda", "API Gateway", "EKS", "S3"],
            },
            "infra": [
                {"name": "EKS-prod-cluster", "type": "Kubernetes"},
                {"name": "payment-rds", "type": "RDS PostgreSQL"},
                {"name": "payments-nlb", "type": "Network Load Balancer"},
                {"name": "us-east-1a", "type": "AWS Region"},
            ],
            "db_servers": [
                {"name": "payment_db", "type": "PostgreSQL"},
                {"name": "payments-orders", "type": "DynamoDB"},
                {"name": "audit-trail", "type": "DynamoDB"},
            ],
            "correlations": [
                {"from": "PaymentService", "to": "payment_db",       "relationship": "depends_on"},
                {"from": "PaymentService", "to": "UserService",       "relationship": "calls"},
                {"from": "PaymentService", "to": "AuditService",      "relationship": "calls"},
                {"from": "PaymentService", "to": "EKS-prod-cluster",  "relationship": "deployed_on"},
                {"from": "PaymentService", "to": "AWS Lambda",        "relationship": "uses"},
                {"from": "UserService",    "to": "payment_db",        "relationship": "depends_on"},
                {"from": "OrderService",   "to": "PaymentService",    "relationship": "depends_on"},
                {"from": "OrderService",   "to": "payments-orders",    "relationship": "depends_on"},
                {"from": "NotificationService", "to": "AWS Lambda",   "relationship": "deployed_on"},
            ],
        },
    },
    {
        "name": "Data Lake Pipeline",
        "description": "AWS Glue + S3 data ingestion pipeline — Python ETL with Spark and PySpark transformations",
        "environment": "staging",
        "repos": [
            {"repoType": "backend", "sourceType": "git", "repoUrl": "https://github.com/aura-demo/datalake-etl",    "branch": "main"},
            {"repoType": "infra",   "sourceType": "git", "repoUrl": "https://github.com/aura-demo/datalake-infra",  "branch": "main"},
        ],
        "knowledge_graph": {
            "code": {
                "languages": ["Python", "SQL", "YAML"],
                "services": ["GlueJobIngestor", "S3Transformer", "AthenaQueryEngine", "DataCatalog"],
                "tech_stack": ["Python 3.11", "AWS Glue", "Apache Spark", "S3", "Athena", "DynamoDB", "CloudWatch", "Terraform"],
            },
            "infra": [
                {"name": "aura-datalake-raw", "type": "S3 Bucket"},
                {"name": "aura-datalake-curated", "type": "S3 Bucket"},
                {"name": "glue-etl-cluster", "type": "AWS Glue"},
                {"name": "athena-workgroup", "type": "AWS Athena"},
            ],
            "db_servers": [
                {"name": "aura-data-catalog", "type": "AWS Glue Data Catalog"},
                {"name": "pipeline-state", "type": "DynamoDB"},
            ],
            "correlations": [
                {"from": "GlueJobIngestor",   "to": "aura-datalake-raw",    "relationship": "reads_from"},
                {"from": "S3Transformer",     "to": "aura-datalake-curated","relationship": "writes_to"},
                {"from": "GlueJobIngestor",   "to": "glue-etl-cluster",    "relationship": "runs_on"},
                {"from": "AthenaQueryEngine", "to": "aura-data-catalog",    "relationship": "queries"},
                {"from": "DataCatalog",       "to": "S3Transformer",       "relationship": "tracks"},
            ],
        },
    },
    {
        "name": "Claims API (Legacy COBOL)",
        "description": "Legacy COBOL claims processing system being modernised — Reverse engineering to knowledge graph",
        "environment": "production",
        "repos": [
            {"repoType": "backend", "sourceType": "local", "localPath": "C:/projects/claims-cobol/src", "repoUrl": ""},
            {"repoType": "config",  "sourceType": "git",   "repoUrl": "https://github.com/aura-demo/claims-config", "branch": "main"},
        ],
        "knowledge_graph": {
            "code": {
                "languages": ["COBOL", "JCL", "SQL"],
                "services": ["ClaimsProcessor", "PolicyValidator", "BenefitsCalculator", "ReportGenerator"],
                "tech_stack": ["COBOL", "IBM DB2", "JCL", "VSAM", "Mainframe z/OS"],
            },
            "infra": [
                {"name": "mainframe-prod", "type": "IBM Mainframe"},
                {"name": "db2-claims", "type": "IBM DB2"},
                {"name": "vsam-store", "type": "VSAM File Store"},
            ],
            "db_servers": [
                {"name": "CLAIMS_DB2", "type": "IBM DB2"},
                {"name": "POLICY_VSAM", "type": "VSAM"},
            ],
            "correlations": [
                {"from": "ClaimsProcessor",    "to": "CLAIMS_DB2",        "relationship": "depends_on"},
                {"from": "PolicyValidator",    "to": "POLICY_VSAM",       "relationship": "depends_on"},
                {"from": "BenefitsCalculator", "to": "ClaimsProcessor",   "relationship": "calls"},
                {"from": "ReportGenerator",    "to": "CLAIMS_DB2",        "relationship": "reads_from"},
                {"from": "ClaimsProcessor",    "to": "mainframe-prod",    "relationship": "runs_on"},
            ],
        },
    },
    {
        "name": "Customer Portal (Angular + Mule)",
        "description": "Customer self-service portal — Angular frontend with Mule ESB integration layer",
        "environment": "development",
        "repos": [
            {"repoType": "ui",      "sourceType": "git",   "repoUrl": "https://github.com/aura-demo/portal-angular", "branch": "develop"},
            {"repoType": "mule",    "sourceType": "git",   "repoUrl": "https://github.com/aura-demo/portal-mule",    "branch": "develop"},
            {"repoType": "backend", "sourceType": "local", "localPath": "C:/projects/portal-api", "repoUrl": ""},
        ],
        "knowledge_graph": {
            "code": {
                "languages": ["TypeScript", "XML", "Java"],
                "services": ["CustomerPortalUI", "AuthGateway", "ProfileService", "MuleIntegrationLayer", "SAMLProvider"],
                "tech_stack": ["Angular 17", "Mule 4", "Java 11", "Spring Security", "OAuth2", "SAML", "PostgreSQL", "Redis"],
            },
            "infra": [
                {"name": "portal-eks",       "type": "Kubernetes"},
                {"name": "portal-alb",       "type": "Application Load Balancer"},
                {"name": "portal-redis",     "type": "ElastiCache Redis"},
                {"name": "cognito-pool",     "type": "AWS Cognito"},
            ],
            "db_servers": [
                {"name": "customer_db", "type": "PostgreSQL"},
                {"name": "session-cache", "type": "Redis"},
            ],
            "correlations": [
                {"from": "CustomerPortalUI",      "to": "AuthGateway",           "relationship": "calls"},
                {"from": "AuthGateway",           "to": "SAMLProvider",          "relationship": "depends_on"},
                {"from": "AuthGateway",           "to": "cognito-pool",          "relationship": "uses"},
                {"from": "MuleIntegrationLayer",  "to": "ProfileService",        "relationship": "routes_to"},
                {"from": "ProfileService",        "to": "customer_db",           "relationship": "depends_on"},
                {"from": "AuthGateway",           "to": "session-cache",         "relationship": "uses"},
                {"from": "CustomerPortalUI",      "to": "portal-eks",            "relationship": "deployed_on"},
            ],
        },
    },
]


@router.post("/seed/samples")
async def seed_sample_projects(user: dict = Depends(require_permission("dev_workspace"))):
    """Create 4 sample projects with pre-populated knowledge graphs for testing."""
    from src.storage.s3_client import put_json
    created = []

    for sample in SAMPLE_PROJECTS:
        project_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        project = {
            "projectId": project_id,
            "userId": user["userId"],
            "username": user["username"],
            "name": sample["name"],
            "description": sample["description"],
            "environment": sample["environment"],
            "status": "analyzed",
            "createdAt": now,
            "updatedAt": now,
            "repoCount": len(sample["repos"]),
            "mcpEndpoints": [],
            "isSample": True,
        }
        put_item("projects", project)

        # Store connectors
        for repo in sample["repos"]:
            put_item("connectors", {
                "connectorId": str(uuid.uuid4()),
                "projectId": project_id,
                "connectorType": "git",
                "sourceType": repo.get("sourceType", "git"),
                "repoType": repo["repoType"],
                "repoUrl": repo.get("repoUrl", ""),
                "localPath": repo.get("localPath", ""),
                "branch": repo.get("branch", "main"),
                "hasToken": False,
                "createdAt": now,
            })

        # Store pre-built knowledge graph to S3
        kg = sample["knowledge_graph"]
        kg["project_id"] = project_id
        try:
            put_json("analysis", f"{project_id}/knowledge_graph.json", kg)
            put_json("analysis", f"{project_id}/code_analysis.json", {
                "project_id": project_id,
                "tech_stack": kg["code"]["tech_stack"],
                "services": kg["code"]["services"],
                "languages": {l: 100 for l in kg["code"]["languages"]},
                "correlations": kg["correlations"],
                "db_references": [d["name"] for d in kg.get("db_servers", [])],
                "cloud_resources": [i["name"] for i in kg.get("infra", [])],
                "total_files": 0,
                "repos_analyzed": len(sample["repos"]),
            })
        except Exception:
            pass  # S3 not configured — graphs show demo data

        created.append({"projectId": project_id, "name": sample["name"], "status": "analyzed"})

    return {"created": len(created), "projects": created}

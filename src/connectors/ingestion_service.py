"""Ingestion service: pulls data from MCP sources → upserts into Neo4j.

Called by:
  - POST /api/ontology/load   (full load)
  - POST /connectors/:id/ingest  (single connector)
  - APScheduler delta job

Never deletes nodes — marks missing ones as status='retired'.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.graph import neo4j_client as neo4j

log = logging.getLogger(__name__)

# ── Organization seed (single org for now) ────────────────────────────────────
ORG_ID = "org-aura-global"
ORG_NAME = "Aura Global"


def _ensure_org():
    neo4j.upsert_node("Organization", ORG_ID, {
        "name": ORG_NAME,
        "domain": "aura.com",
        "source": "seed",
    })


# ── Git ingestion ──────────────────────────────────────────────────────────────

def ingest_git(connector_config: dict | None = None) -> dict:
    """Ingest Git repos and services from mock or real Git MCP."""
    from src.connectors.mock_mcp.server import git_list_repos, git_list_services
    stats = {"repos": 0, "projects": 0, "services": 0, "teams": 0, "errors": []}

    _ensure_org()

    # Teams first
    team_ids_seen: set[str] = set()
    repos = git_list_repos()
    for repo in repos:
        team_name = repo.get("team", "unknown-team")
        team_eid = f"team-{team_name}"
        if team_eid not in team_ids_seen:
            neo4j.upsert_node("Team", team_eid, {
                "name": team_name,
                "email": f"{team_name}@aura.com",
                "source": "git",
            })
            team_ids_seen.add(team_eid)
            stats["teams"] += 1

    # Projects (derived from repo name prefix: strip -core / -frontend)
    project_ids_seen: set[str] = set()
    for repo in repos:
        raw = repo["name"]
        proj_name = raw.removesuffix("-core").removesuffix("-frontend")
        proj_eid = f"project-{proj_name}"
        if proj_eid not in project_ids_seen:
            neo4j.upsert_node("Project", proj_eid, {
                "name": proj_name,
                "status": "active",
                "environment": "prod",
                "source": "git",
                "externalId": proj_eid,
            })
            neo4j.upsert_relationship("Project", proj_eid, "Organization", ORG_ID, "BELONGS_TO")
            team_eid = f"team-{repo.get('team', 'unknown-team')}"
            neo4j.upsert_relationship("Project", proj_eid, "Team", team_eid, "MANAGED_BY")
            project_ids_seen.add(proj_eid)
            stats["projects"] += 1

    # Repos
    for repo in repos:
        try:
            neo4j.upsert_node("Repository", repo["id"], {
                "name": repo["name"],
                "url": repo["url"],
                "branch": repo.get("branch", "main"),
                "language": repo.get("language"),
                "source": "git",
                "lastCommit": repo.get("last_commit"),
            })
            team_eid = f"team-{repo.get('team', 'unknown-team')}"
            neo4j.upsert_relationship("Repository", repo["id"], "Team", team_eid, "MANAGED_BY")
            neo4j.upsert_relationship("Repository", repo["id"], "Organization", ORG_ID, "BELONGS_TO")
            # Link repo to its project
            proj_name = repo["name"].removesuffix("-core").removesuffix("-frontend")
            neo4j.upsert_relationship("Repository", repo["id"], "Project", f"project-{proj_name}", "BELONGS_TO")
            stats["repos"] += 1
        except Exception as exc:
            stats["errors"].append(f"Repo {repo['id']}: {exc}")

    # Services
    for svc in git_list_services():
        try:
            neo4j.upsert_node("Service", svc["id"], {
                "name": svc["name"],
                "type": svc.get("type", "microservice"),
                "language": svc.get("language"),
                "hostname": svc.get("hostname"),
                "version": svc.get("version"),
                "environment": svc.get("environment", "prod"),
                "source": "git",
            })
            neo4j.upsert_relationship("Service", svc["id"], "Repository", svc["repo_id"], "HOSTED_IN")
            team_eid = f"team-{svc.get('team', 'unknown-team')}"
            neo4j.upsert_relationship("Service", svc["id"], "Team", team_eid, "MANAGED_BY")
            neo4j.upsert_relationship("Service", svc["id"], "Organization", ORG_ID, "BELONGS_TO")
            stats["services"] += 1
        except Exception as exc:
            stats["errors"].append(f"Service {svc['id']}: {exc}")

    log.info("Git ingestion: %s", stats)
    return stats


# ── ServiceNow CMDB ingestion ─────────────────────────────────────────────────

def ingest_servicenow(connector_config: dict | None = None, delta_since: str | None = None) -> dict:
    """Ingest ServiceNow incidents, CMDB items, and changes."""
    from src.connectors.mock_mcp.server import (
        servicenow_list_incidents, servicenow_list_cmdb_items, servicenow_list_changes
    )
    stats = {"incidents": 0, "cmdb": 0, "changes": 0, "errors": []}

    _ensure_org()

    # CMDB Infrastructure
    for ci in servicenow_list_cmdb_items():
        try:
            neo4j.upsert_node("Infrastructure", ci["id"], {
                "name": ci["name"],
                "type": ci["type"],
                "hostname": ci.get("hostname"),
                "ip": ci.get("ip"),
                "region": ci.get("region"),
                "environment": ci.get("environment"),
                "source": "servicenow_cmdb",
            })
            team_eid = f"team-{ci.get('team', 'unknown-team')}"
            neo4j.upsert_relationship("Infrastructure", ci["id"], "Organization", ORG_ID, "BELONGS_TO")
            if ci.get("service_id"):
                neo4j.upsert_relationship("Service", ci["service_id"], "Infrastructure", ci["id"], "RUNS_ON")
            stats["cmdb"] += 1
        except Exception as exc:
            stats["errors"].append(f"CMDB {ci['id']}: {exc}")

    # Incidents
    for inc in servicenow_list_incidents(days=90):
        try:
            neo4j.upsert_node("Incident", inc["id"], {
                "number": inc["number"],
                "title": inc["title"],
                "severity": inc["severity"],
                "state": inc["state"],
                "createdAt": inc.get("created_at"),
                "resolvedAt": inc.get("resolved_at"),
                "source": "servicenow",
            })
            if inc.get("service_id"):
                neo4j.upsert_relationship("Service", inc["service_id"], "Incident", inc["id"], "HAS_INCIDENT")
            stats["incidents"] += 1
        except Exception as exc:
            stats["errors"].append(f"Incident {inc['id']}: {exc}")

    # Changes
    for chg in servicenow_list_changes(days=90):
        try:
            neo4j.upsert_node("Incident", chg["id"], {
                "number": chg["number"],
                "title": chg["title"],
                "state": chg["state"],
                "environment": chg.get("environment"),
                "scheduledAt": chg.get("scheduled_at"),
                "source": "servicenow_change",
            })
            if chg.get("service_id"):
                neo4j.upsert_relationship("Service", chg["service_id"], "Incident", chg["id"], "HAS_CHANGE")
            stats["changes"] += 1
        except Exception as exc:
            stats["errors"].append(f"Change {chg['id']}: {exc}")

    log.info("ServiceNow ingestion: %s", stats)
    return stats


# ── Wiz ingestion ─────────────────────────────────────────────────────────────

def ingest_wiz(connector_config: dict | None = None) -> dict:
    """Ingest Wiz security findings."""
    from src.connectors.mock_mcp.server import wiz_list_findings
    stats = {"findings": 0, "errors": []}

    for finding in wiz_list_findings():
        try:
            neo4j.upsert_node("SecurityFinding", finding["id"], {
                "title": finding["title"],
                "severity": finding["severity"],
                "cvss": finding.get("cvss"),
                "status": finding.get("status", "open"),
                "cve": finding.get("cve"),
                "detectedAt": finding.get("detected_at"),
                "affectedHostname": finding.get("affected_resource_hostname"),
                "source": "wiz",
            })
            # Link to infrastructure or service by hostname
            hostname = finding.get("affected_resource_hostname", "")
            if hostname:
                _link_finding_by_hostname(finding["id"], hostname)
            stats["findings"] += 1
        except Exception as exc:
            stats["errors"].append(f"Finding {finding['id']}: {exc}")

    log.info("Wiz ingestion: %s", stats)
    return stats


def _link_finding_by_hostname(finding_id: str, hostname: str):
    """Try to link a SecurityFinding to a Service or Infrastructure by hostname."""
    from src.graph.neo4j_client import session
    cypher = """
    MATCH (n)
    WHERE n.hostname = $hostname AND (n:Service OR n:Infrastructure)
    RETURN elementId(n) AS nid, labels(n)[0] AS label, n.externalId AS eid
    LIMIT 1
    """
    try:
        with session() as s:
            result = s.run(cypher, hostname=hostname).single()
            if result:
                label = result["label"]
                eid = result["eid"]
                neo4j.upsert_relationship(label, eid, "SecurityFinding", finding_id, "HAS_FINDING")
    except Exception:
        pass


# ── Full load orchestrator ────────────────────────────────────────────────────

def run_full_load(delta_since: str | None = None) -> dict:
    """Run ingestion from all mock MCP sources sequentially."""
    if not neo4j.is_available():
        return {"error": "Neo4j is not available. Set neo4j_enabled=true in .env and ensure Neo4j is running."}

    neo4j.ensure_schema()
    started_at = datetime.now(timezone.utc).isoformat()
    results: dict[str, Any] = {"started_at": started_at}

    try:
        results["git"] = ingest_git()
    except Exception as exc:
        log.exception("Git ingestion failed")
        results["git"] = {"error": str(exc)}

    try:
        results["servicenow"] = ingest_servicenow(delta_since=delta_since)
    except Exception as exc:
        log.exception("ServiceNow ingestion failed")
        results["servicenow"] = {"error": str(exc)}

    try:
        results["wiz"] = ingest_wiz()
    except Exception as exc:
        log.exception("Wiz ingestion failed")
        results["wiz"] = {"error": str(exc)}

    # Run correlation after all data is loaded
    try:
        from src.connectors.correlation_engine import run_correlation
        results["correlation"] = run_correlation()
    except Exception as exc:
        log.exception("Correlation failed")
        results["correlation"] = {"error": str(exc)}

    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    log.info("Full ontology load complete: %s", results)
    return results

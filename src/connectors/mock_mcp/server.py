"""Mock MCP data generator — simulates Git, ServiceNow, and Wiz data sources.

Generates realistic enterprise-scale fake data (~2,000 nodes, ~8,000 edges)
for local testing of the ontology ingestion pipeline.  Results are cached
in-memory so repeated calls to any tool are instant.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

# ── Seed for reproducible data ────────────────────────────────────────────────
random.seed(42)

# ── Base data pools ───────────────────────────────────────────────────────────
_TEAMS = [
    ("platform-team", "platform@aura.com"),
    ("claims-team", "claims@aura.com"),
    ("payments-team", "payments@aura.com"),
    ("identity-team", "identity@aura.com"),
    ("data-team", "data@aura.com"),
    ("infra-team", "infra@aura.com"),
    ("security-team", "security@aura.com"),
    ("actuarial-team", "actuarial@aura.com"),
    ("underwriting-team", "underwriting@aura.com"),
    ("digital-team", "digital@aura.com"),
]

_ENVIRONMENTS = ["prod", "staging", "dev", "dr"]
_REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]
_LANGUAGES = ["Python", "Java", "TypeScript", "Go", "C#", "Scala"]
_SEVERITIES = ["critical", "high", "medium", "low"]
_INCIDENT_STATES = ["open", "in_progress", "resolved", "closed"]
_CI_TYPES = ["server", "vm", "container", "load_balancer", "database", "network_device"]
_ENGINES = ["PostgreSQL", "MySQL", "Oracle", "MongoDB", "Redis", "Elasticsearch"]

_REPO_PREFIXES = [
    "aura-claims", "aura-payments", "aura-identity", "aura-policy", "aura-underwriting",
    "aura-actuarial", "aura-reporting", "aura-notifications", "aura-audit", "aura-portal",
    "aura-gateway", "aura-data-pipeline", "aura-ml-platform", "aura-risk", "aura-compliance",
]

_SERVICE_SUFFIXES = [
    "api", "service", "processor", "worker", "handler", "engine", "adapter",
    "orchestrator", "validator", "aggregator", "transformer", "scheduler",
]

_CVE_IDS = [f"CVE-2024-{random.randint(1000, 9999)}" for _ in range(30)]

_WIZ_FINDING_TITLES = [
    "Public S3 bucket with sensitive data exposure",
    "Unpatched critical CVE in container image",
    "Overly permissive IAM role",
    "Database port exposed to internet",
    "Missing encryption at rest",
    "Insecure TLS configuration (TLS 1.0 enabled)",
    "Root account usage detected",
    "Security group allows unrestricted SSH access",
    "Secrets stored in environment variables",
    "Container running as root",
    "Missing multi-factor authentication",
    "Privilege escalation path detected",
    "Outdated OS with known vulnerabilities",
    "API key hardcoded in source code",
    "Missing WAF protection",
]


def _ts_ago(days: int = 0, hours: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days, hours=hours)
    return dt.isoformat()


@lru_cache(maxsize=1)
def _generate_repos() -> list[dict]:
    repos = []
    for i, prefix in enumerate(_REPO_PREFIXES):
        team = _TEAMS[i % len(_TEAMS)]
        for j in range(2):
            rid = f"repo-{prefix}-{j}"
            repos.append({
                "id": rid,
                "name": f"{prefix}-{'core' if j == 0 else 'frontend'}",
                "url": f"https://github.com/aura/{prefix}-{'core' if j == 0 else 'frontend'}",
                "branch": "main",
                "language": random.choice(_LANGUAGES),
                "team": team[0],
                "last_commit": _ts_ago(days=random.randint(0, 30)),
                "source": "git",
            })
    return repos


@lru_cache(maxsize=1)
def _generate_services() -> list[dict]:
    repos = _generate_repos()
    services = []
    for repo in repos:
        for suffix in random.sample(_SERVICE_SUFFIXES, k=random.randint(2, 5)):
            base = repo["name"].replace("-core", "").replace("-frontend", "")
            hostname = f"{base}-{suffix}.prod.aura.internal"
            services.append({
                "id": f"svc-{base}-{suffix}",
                "name": f"{base}-{suffix}",
                "type": "microservice",
                "language": repo["language"],
                "hostname": hostname,
                "version": f"1.{random.randint(0, 20)}.{random.randint(0, 50)}",
                "repo_id": repo["id"],
                "team": repo["team"],
                "environment": "prod",
                "source": "git",
            })
    return services


@lru_cache(maxsize=1)
def _generate_cmdb_items() -> list[dict]:
    items = []
    services = _generate_services()
    # VMs and containers hosting services
    for svc in services:
        ip = f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
        hostname = svc["hostname"].replace(".internal", "-vm.internal")
        items.append({
            "id": f"ci-{svc['id']}",
            "name": hostname,
            "type": random.choice(["vm", "container"]),
            "hostname": hostname,
            "ip": ip,
            "region": random.choice(_REGIONS),
            "environment": svc["environment"],
            "team": svc["team"],
            "service_id": svc["id"],
            "source": "servicenow_cmdb",
        })
    # Additional standalone infra
    for i in range(50):
        ip = f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
        hostname = f"aura-{random.choice(['lb', 'db', 'cache', 'proxy'])}-{i:03d}.prod.aura.internal"
        items.append({
            "id": f"ci-standalone-{i}",
            "name": hostname,
            "type": random.choice(_CI_TYPES),
            "hostname": hostname,
            "ip": ip,
            "region": random.choice(_REGIONS),
            "environment": random.choice(_ENVIRONMENTS),
            "team": random.choice(_TEAMS)[0],
            "service_id": None,
            "source": "servicenow_cmdb",
        })
    return items


@lru_cache(maxsize=1)
def _generate_incidents() -> list[dict]:
    services = _generate_services()
    incidents = []
    for i in range(200):
        svc = random.choice(services)
        incidents.append({
            "id": f"INC{1000000 + i:07d}",
            "number": f"INC{1000000 + i:07d}",
            "title": f"{random.choice(['High latency in', 'Errors spiking on', 'Timeout on', 'Memory leak in'])} {svc['name']}",
            "severity": random.choice(_SEVERITIES),
            "state": random.choice(_INCIDENT_STATES),
            "service_ci": svc["hostname"],
            "service_id": svc["id"],
            "team": svc["team"],
            "created_at": _ts_ago(days=random.randint(0, 90)),
            "resolved_at": _ts_ago(days=random.randint(0, 30)) if random.random() > 0.3 else None,
            "source": "servicenow",
        })
    return incidents


@lru_cache(maxsize=1)
def _generate_changes() -> list[dict]:
    services = _generate_services()
    changes = []
    for i in range(80):
        svc = random.choice(services)
        changes.append({
            "id": f"CHG{2000000 + i:07d}",
            "number": f"CHG{2000000 + i:07d}",
            "title": f"Deploy {svc['name']} v{random.randint(1, 5)}.{random.randint(0, 20)}",
            "state": random.choice(["scheduled", "in_progress", "completed", "failed"]),
            "environment": random.choice(_ENVIRONMENTS),
            "service_id": svc["id"],
            "team": svc["team"],
            "scheduled_at": _ts_ago(days=-random.randint(1, 7)),
            "source": "servicenow",
        })
    return changes


@lru_cache(maxsize=1)
def _generate_wiz_findings() -> list[dict]:
    services = _generate_services()
    cmdb = _generate_cmdb_items()
    findings = []
    for i in range(100):
        target = random.choice(services + cmdb[:30])
        severity = random.choice(_SEVERITIES)
        cvss = {"critical": 9.0 + round(random.random(), 1),
                "high": 7.0 + round(random.random() * 1.9, 1),
                "medium": 4.0 + round(random.random() * 2.9, 1),
                "low": 0.1 + round(random.random() * 3.8, 1)}[severity]
        findings.append({
            "id": f"WIZ-{uuid.uuid4().hex[:8].upper()}",
            "title": random.choice(_WIZ_FINDING_TITLES),
            "severity": severity,
            "cvss": cvss,
            "status": random.choice(["open", "in_progress", "resolved"]),
            "cve": random.choice(_CVE_IDS) if random.random() > 0.5 else None,
            "affected_resource_hostname": target.get("hostname", target.get("name", "")),
            "affected_resource_id": target["id"],
            "remediation": "Apply security patch and rotate credentials",
            "detected_at": _ts_ago(days=random.randint(0, 60)),
            "source": "wiz",
        })
    return findings


# ── Public tool functions (called by ingestion service) ───────────────────────

def git_list_repos() -> list[dict]:
    """List all Git repositories."""
    return _generate_repos()


def git_list_services(repo_name: str | None = None) -> list[dict]:
    """List services, optionally filtered by repo name."""
    services = _generate_services()
    if repo_name:
        repos = {r["name"]: r["id"] for r in _generate_repos()}
        repo_id = repos.get(repo_name)
        return [s for s in services if s.get("repo_id") == repo_id] if repo_id else []
    return services


def servicenow_list_incidents(days: int = 30) -> list[dict]:
    """List incidents from the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [
        i for i in _generate_incidents()
        if datetime.fromisoformat(i["created_at"]) >= cutoff
    ]


def servicenow_list_cmdb_items(ci_type: str | None = None) -> list[dict]:
    """List CMDB configuration items, optionally filtered by type."""
    items = _generate_cmdb_items()
    if ci_type:
        items = [i for i in items if i["type"] == ci_type]
    return items


def servicenow_list_changes(days: int = 30) -> list[dict]:
    """List change requests from the last N days."""
    return _generate_changes()[:days]


def wiz_list_findings(severity: str | None = None) -> list[dict]:
    """List Wiz security findings, optionally filtered by severity."""
    findings = _generate_wiz_findings()
    if severity:
        findings = [f for f in findings if f["severity"] == severity]
    return findings


def wiz_get_finding_detail(finding_id: str) -> dict | None:
    """Get detailed info for a specific Wiz finding."""
    for f in _generate_wiz_findings():
        if f["id"] == finding_id:
            return {**f, "remediation_steps": [
                "Identify affected resources",
                "Apply the recommended security patch",
                "Rotate affected credentials",
                "Verify fix in staging before production",
                "Monitor for 48h post-remediation",
            ]}
    return None


def get_all_data_summary() -> dict:
    """Return a summary of all generated data counts."""
    return {
        "repos": len(_generate_repos()),
        "services": len(_generate_services()),
        "cmdb_items": len(_generate_cmdb_items()),
        "incidents": len(_generate_incidents()),
        "changes": len(_generate_changes()),
        "wiz_findings": len(_generate_wiz_findings()),
        "teams": len(_TEAMS),
    }

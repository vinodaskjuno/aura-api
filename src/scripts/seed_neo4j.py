"""
Seed Neo4j with realistic Aura enterprise ontology data.

Run from repo root:
    python -m src.scripts.seed_neo4j

Creates:
  1 Organization
  5 Teams
  6 Projects
  12 Repositories (2 per project)
  24 Services (4 per project)
  18 Infrastructure nodes (VMs/containers/LBs)
  6 Databases
  8 Security Findings (Wiz)
  4 Incidents (ServiceNow)
  ~100 Relationships
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from neo4j import GraphDatabase

# ── Connection ────────────────────────────────────────────────────────────────
NEO4J_URI      = os.getenv("NEO4J_URI",      "neo4j://127.0.0.1:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")


def run(driver, cypher: str, **params):
    with driver.session(database=NEO4J_DATABASE) as s:
        s.run(cypher, **params)


def merge_node(driver, label: str, eid: str, props: dict):
    props["externalId"] = eid
    props["updatedAt"] = "2026-08-11T00:00:00Z"
    run(driver,
        f"MERGE (n:{label} {{externalId: $eid}}) SET n += $props",
        eid=eid, props=props)


def merge_rel(driver, from_label, from_eid, to_label, to_eid, rel_type, props=None):
    p = props or {}
    run(driver, f"""
        MATCH (a:{from_label} {{externalId: $a}})
        MATCH (b:{to_label}   {{externalId: $b}})
        MERGE (a)-[r:{rel_type}]->(b)
        SET r += $props, r.active = true
    """, a=from_eid, b=to_eid, props=p)


# ─────────────────────────────────────────────────────────────────────────────
def seed(driver):
    print("▶ Clearing existing seed data...")
    run(driver, "MATCH (n) WHERE n.source IN ['seed','git','servicenow_cmdb','wiz','servicenow'] DETACH DELETE n")

    # ── 1. Organization ───────────────────────────────────────────────────────
    print("▶ Organization...")
    merge_node(driver, "Organization", "org-aura", {
        "name": "Aura Global",
        "domain": "aura.com",
        "source": "seed",
    })

    # ── 2. Teams ──────────────────────────────────────────────────────────────
    print("▶ Teams...")
    teams = [
        ("team-claims",       "Claims Engineering",        "claims-eng@aura.com"),
        ("team-payments",     "Payments Platform",         "payments@aura.com"),
        ("team-identity",     "Identity & Access",         "iam@aura.com"),
        ("team-data",         "Data Platform",             "data-platform@aura.com"),
        ("team-infra",        "Cloud Infrastructure",      "infra@aura.com"),
        ("team-security",     "Security Engineering",      "security@aura.com"),
    ]
    for eid, name, email in teams:
        merge_node(driver, "Team", eid, {"name": name, "email": email, "source": "seed"})
        merge_rel(driver, "Team", eid, "Organization", "org-aura", "BELONGS_TO")

    # ── 3. Projects ───────────────────────────────────────────────────────────
    print("▶ Projects...")
    projects = [
        ("proj-claims-mgmt",    "Claims Management",          "prod",    "active",   "team-claims"),
        ("proj-payment-gw",     "Payment Gateway",            "prod",    "active",   "team-payments"),
        ("proj-iam-platform",   "IAM Platform",               "prod",    "active",   "team-identity"),
        ("proj-data-lakehouse", "Data Lakehouse",             "staging", "active",   "team-data"),
        ("proj-policy-admin",   "Policy Administration",      "prod",    "active",   "team-claims"),
        ("proj-fraud-detect",   "Fraud Detection",            "prod",    "active",   "team-payments"),
    ]
    for eid, name, env, status, team in projects:
        merge_node(driver, "Project", eid, {
            "name": name, "environment": env,
            "status": status, "source": "git",
        })
        merge_rel(driver, "Project", eid, "Organization", "org-aura", "BELONGS_TO")
        merge_rel(driver, "Project", eid, "Team", team, "MANAGED_BY")

    # ── 4. Repositories ───────────────────────────────────────────────────────
    print("▶ Repositories...")
    repos = [
        # (eid, name, url, lang, project, team)
        ("repo-claims-api",       "claims-api",             "https://github.com/aura/claims-api",             "Python",     "proj-claims-mgmt",    "team-claims"),
        ("repo-claims-ui",        "claims-portal-ui",       "https://github.com/aura/claims-portal-ui",       "TypeScript", "proj-claims-mgmt",    "team-claims"),
        ("repo-payment-core",     "payment-core",           "https://github.com/aura/payment-core",           "Java",       "proj-payment-gw",     "team-payments"),
        ("repo-payment-ui",       "payment-portal",         "https://github.com/aura/payment-portal",         "TypeScript", "proj-payment-gw",     "team-payments"),
        ("repo-iam-core",         "iam-service",            "https://github.com/aura/iam-service",            "Go",         "proj-iam-platform",   "team-identity"),
        ("repo-iam-sdk",          "iam-client-sdk",         "https://github.com/aura/iam-client-sdk",         "Python",     "proj-iam-platform",   "team-identity"),
        ("repo-lakehouse-ingest", "lakehouse-ingestion",    "https://github.com/aura/lakehouse-ingestion",    "Python",     "proj-data-lakehouse", "team-data"),
        ("repo-lakehouse-query",  "lakehouse-query-engine", "https://github.com/aura/lakehouse-query-engine", "Scala",      "proj-data-lakehouse", "team-data"),
        ("repo-policy-api",       "policy-admin-api",       "https://github.com/aura/policy-admin-api",       "C#",         "proj-policy-admin",   "team-claims"),
        ("repo-policy-ui",        "policy-admin-ui",        "https://github.com/aura/policy-admin-ui",        "TypeScript", "proj-policy-admin",   "team-claims"),
        ("repo-fraud-model",      "fraud-ml-model",         "https://github.com/aura/fraud-ml-model",         "Python",     "proj-fraud-detect",   "team-payments"),
        ("repo-fraud-api",        "fraud-detection-api",    "https://github.com/aura/fraud-detection-api",    "Python",     "proj-fraud-detect",   "team-payments"),
    ]
    for eid, name, url, lang, proj, team in repos:
        merge_node(driver, "Repository", eid, {
            "name": name, "url": url, "language": lang,
            "branch": "main", "lastCommit": "2026-08-10T14:22:00Z", "source": "git",
        })
        merge_rel(driver, "Repository", eid, "Project", proj, "BELONGS_TO")
        merge_rel(driver, "Repository", eid, "Team", team, "MANAGED_BY")
        merge_rel(driver, "Repository", eid, "Organization", "org-aura", "BELONGS_TO")

    # ── 5. Services ───────────────────────────────────────────────────────────
    print("▶ Services...")
    services = [
        # (eid, name, type, lang, hostname, env, repo, team)
        ("svc-claims-api",       "claims-api",              "rest-api",      "Python",     "claims-api.prod.aura.internal",          "prod",    "repo-claims-api",       "team-claims"),
        ("svc-claims-processor", "claims-processor",        "worker",        "Python",     "claims-processor.prod.aura.internal",    "prod",    "repo-claims-api",       "team-claims"),
        ("svc-claims-ui",        "claims-portal",           "frontend",      "TypeScript", "claims-portal.prod.aura.internal",       "prod",    "repo-claims-ui",        "team-claims"),
        ("svc-claims-notif",     "claims-notification-svc", "worker",        "Python",     "claims-notif.prod.aura.internal",        "prod",    "repo-claims-api",       "team-claims"),

        ("svc-payment-api",      "payment-gateway-api",     "rest-api",      "Java",       "payment-gw.prod.aura.internal",          "prod",    "repo-payment-core",     "team-payments"),
        ("svc-payment-processor","payment-processor",       "worker",        "Java",       "payment-proc.prod.aura.internal",        "prod",    "repo-payment-core",     "team-payments"),
        ("svc-payment-ui",       "payment-portal",          "frontend",      "TypeScript", "payment-portal.prod.aura.internal",      "prod",    "repo-payment-ui",       "team-payments"),
        ("svc-payment-reconcile","payment-reconciliation",  "batch",         "Java",       "payment-recon.prod.aura.internal",       "prod",    "repo-payment-core",     "team-payments"),

        ("svc-iam-auth",         "auth-service",            "rest-api",      "Go",         "auth.prod.aura.internal",                "prod",    "repo-iam-core",         "team-identity"),
        ("svc-iam-token",        "token-service",           "rest-api",      "Go",         "token.prod.aura.internal",               "prod",    "repo-iam-core",         "team-identity"),
        ("svc-iam-mfa",          "mfa-service",             "rest-api",      "Go",         "mfa.prod.aura.internal",                 "prod",    "repo-iam-core",         "team-identity"),
        ("svc-iam-sdk",          "iam-client-sdk",          "library",       "Python",     "iam-sdk.prod.aura.internal",             "prod",    "repo-iam-sdk",          "team-identity"),

        ("svc-lake-ingest",      "lakehouse-ingestion",     "batch",         "Python",     "lake-ingest.staging.aura.internal",      "staging", "repo-lakehouse-ingest", "team-data"),
        ("svc-lake-catalog",     "data-catalog",            "rest-api",      "Python",     "data-catalog.staging.aura.internal",     "staging", "repo-lakehouse-ingest", "team-data"),
        ("svc-lake-query",       "query-engine",            "rest-api",      "Scala",      "query-engine.staging.aura.internal",     "staging", "repo-lakehouse-query",  "team-data"),
        ("svc-lake-transform",   "data-transformer",        "worker",        "Python",     "data-transform.staging.aura.internal",   "staging", "repo-lakehouse-ingest", "team-data"),

        ("svc-policy-api",       "policy-admin-api",        "rest-api",      "C#",         "policy-api.prod.aura.internal",          "prod",    "repo-policy-api",       "team-claims"),
        ("svc-policy-ui",        "policy-admin-portal",     "frontend",      "TypeScript", "policy-portal.prod.aura.internal",       "prod",    "repo-policy-ui",        "team-claims"),
        ("svc-policy-engine",    "underwriting-engine",     "worker",        "C#",         "uw-engine.prod.aura.internal",           "prod",    "repo-policy-api",       "team-claims"),
        ("svc-policy-docs",      "document-generator",      "worker",        "C#",         "doc-gen.prod.aura.internal",             "prod",    "repo-policy-api",       "team-claims"),

        ("svc-fraud-api",        "fraud-detection-api",     "rest-api",      "Python",     "fraud-api.prod.aura.internal",           "prod",    "repo-fraud-api",        "team-payments"),
        ("svc-fraud-model",      "fraud-ml-scoring",        "ml-inference",  "Python",     "fraud-model.prod.aura.internal",         "prod",    "repo-fraud-model",      "team-payments"),
        ("svc-fraud-stream",     "fraud-event-processor",   "stream",        "Python",     "fraud-stream.prod.aura.internal",        "prod",    "repo-fraud-api",        "team-payments"),
        ("svc-fraud-dashboard",  "fraud-ops-dashboard",     "frontend",      "TypeScript", "fraud-dash.prod.aura.internal",          "prod",    "repo-fraud-api",        "team-payments"),
    ]
    for eid, name, stype, lang, hostname, env, repo, team in services:
        merge_node(driver, "Service", eid, {
            "name": name, "type": stype, "language": lang,
            "hostname": hostname, "environment": env,
            "version": "1.0.0", "status": "active", "source": "git",
        })
        merge_rel(driver, "Service", eid, "Repository", repo, "HOSTED_IN")
        merge_rel(driver, "Service", eid, "Team", team, "MANAGED_BY")
        merge_rel(driver, "Service", eid, "Organization", "org-aura", "BELONGS_TO")

    # ── 6. Inter-service dependencies ─────────────────────────────────────────
    print("▶ Service dependencies...")
    deps = [
        # Claims depends on IAM for auth
        ("svc-claims-api",       "svc-iam-auth",      "auth"),
        ("svc-claims-processor", "svc-claims-api",    "internal"),
        ("svc-claims-notif",     "svc-claims-api",    "event"),
        # Payment depends on IAM + Fraud
        ("svc-payment-api",      "svc-iam-auth",      "auth"),
        ("svc-payment-api",      "svc-fraud-api",     "validation"),
        ("svc-payment-processor","svc-payment-api",   "internal"),
        ("svc-payment-reconcile","svc-payment-api",   "batch"),
        # Policy depends on IAM + Claims
        ("svc-policy-api",       "svc-iam-auth",      "auth"),
        ("svc-policy-api",       "svc-claims-api",    "data"),
        ("svc-policy-engine",    "svc-policy-api",    "internal"),
        # Fraud depends on data lake
        ("svc-fraud-model",      "svc-lake-query",    "data"),
        ("svc-fraud-api",        "svc-fraud-model",   "inference"),
        ("svc-fraud-stream",     "svc-fraud-api",     "event"),
        # Data lake
        ("svc-lake-transform",   "svc-lake-ingest",   "pipeline"),
        ("svc-lake-catalog",     "svc-lake-query",    "metadata"),
    ]
    for src, dst, dep_type in deps:
        merge_rel(driver, "Service", src, "Service", dst, "DEPENDS_ON", {"type": dep_type})

    # ── 7. Infrastructure ─────────────────────────────────────────────────────
    print("▶ Infrastructure...")
    infra = [
        # (eid, name, type, hostname, ip, region, zone, env, team)
        ("infra-claims-vm-1",   "claims-app-vm-01",    "vm",           "vm-claims-01.us-east-1.aura.internal",  "10.0.1.10", "us-east-1", "us-east-1a", "prod",    "team-claims"),
        ("infra-claims-vm-2",   "claims-app-vm-02",    "vm",           "vm-claims-02.us-east-1.aura.internal",  "10.0.1.11", "us-east-1", "us-east-1b", "prod",    "team-claims"),
        ("infra-claims-lb",     "claims-alb",          "load_balancer","alb-claims.us-east-1.aura.internal",    "10.0.1.5",  "us-east-1", "us-east-1a", "prod",    "team-infra"),
        ("infra-payment-k8s",   "payment-k8s-node-01", "container",    "k8s-payment-01.us-east-1.aura.internal","10.0.2.10", "us-east-1", "us-east-1a", "prod",    "team-payments"),
        ("infra-payment-k8s-2", "payment-k8s-node-02", "container",    "k8s-payment-02.us-east-1.aura.internal","10.0.2.11", "us-east-1", "us-east-1b", "prod",    "team-payments"),
        ("infra-payment-lb",    "payment-nlb",         "load_balancer","nlb-payment.us-east-1.aura.internal",   "10.0.2.5",  "us-east-1", "us-east-1a", "prod",    "team-infra"),
        ("infra-iam-vm-1",      "iam-app-vm-01",       "vm",           "vm-iam-01.us-east-1.aura.internal",     "10.0.3.10", "us-east-1", "us-east-1a", "prod",    "team-identity"),
        ("infra-fraud-gpu",     "fraud-gpu-node-01",   "vm",           "gpu-fraud-01.us-east-1.aura.internal",  "10.0.4.10", "us-east-1", "us-east-1a", "prod",    "team-payments"),
        ("infra-lake-emr",      "data-lake-emr-cluster","container",   "emr-lake-01.us-east-1.aura.internal",   "10.0.5.10", "us-east-1", "us-east-1a", "staging", "team-data"),
        ("infra-policy-vm",     "policy-app-vm-01",    "vm",           "vm-policy-01.us-east-1.aura.internal",  "10.0.6.10", "us-east-1", "us-east-1a", "prod",    "team-claims"),
    ]
    for eid, name, itype, hostname, ip, region, zone, env, team in infra:
        merge_node(driver, "Infrastructure", eid, {
            "name": name, "type": itype, "hostname": hostname,
            "ip": ip, "region": region, "zone": zone,
            "environment": env, "status": "active", "source": "servicenow_cmdb",
        })
        merge_rel(driver, "Infrastructure", eid, "Organization", "org-aura", "BELONGS_TO")
        merge_rel(driver, "Infrastructure", eid, "Team", team, "MANAGED_BY")

    # Services RUNS_ON infrastructure
    runs_on = [
        ("svc-claims-api",       "infra-claims-vm-1"),
        ("svc-claims-processor", "infra-claims-vm-2"),
        ("svc-claims-notif",     "infra-claims-vm-2"),
        ("svc-payment-api",      "infra-payment-k8s"),
        ("svc-payment-processor","infra-payment-k8s-2"),
        ("svc-payment-reconcile","infra-payment-k8s-2"),
        ("svc-iam-auth",         "infra-iam-vm-1"),
        ("svc-iam-token",        "infra-iam-vm-1"),
        ("svc-iam-mfa",          "infra-iam-vm-1"),
        ("svc-fraud-model",      "infra-fraud-gpu"),
        ("svc-fraud-api",        "infra-fraud-gpu"),
        ("svc-lake-ingest",      "infra-lake-emr"),
        ("svc-lake-query",       "infra-lake-emr"),
        ("svc-policy-api",       "infra-policy-vm"),
        ("svc-policy-engine",    "infra-policy-vm"),
    ]
    for svc, infra_eid in runs_on:
        merge_rel(driver, "Service", svc, "Infrastructure", infra_eid, "RUNS_ON")

    # ── 8. Databases ──────────────────────────────────────────────────────────
    print("▶ Databases...")
    databases = [
        ("db-claims-pg",    "claims-postgres",      "PostgreSQL", "db-claims.us-east-1.rds.aura.internal",   5432, "us-east-1", "prod"),
        ("db-payment-pg",   "payment-postgres",     "PostgreSQL", "db-payment.us-east-1.rds.aura.internal",  5432, "us-east-1", "prod"),
        ("db-iam-pg",       "iam-postgres",         "PostgreSQL", "db-iam.us-east-1.rds.aura.internal",      5432, "us-east-1", "prod"),
        ("db-fraud-redis",  "fraud-redis-cache",    "Redis",      "redis-fraud.us-east-1.aura.internal",     6379, "us-east-1", "prod"),
        ("db-lake-s3",      "data-lake-s3",         "S3",         "s3://aura-data-lakehouse-prod",            443,  "us-east-1", "staging"),
        ("db-policy-oracle","policy-oracle-db",     "Oracle",     "db-policy.us-east-1.rds.aura.internal",   1521, "us-east-1", "prod"),
    ]
    for eid, name, engine, hostname, port, region, env in databases:
        merge_node(driver, "Database", eid, {
            "name": name, "engine": engine, "hostname": hostname,
            "port": port, "region": region, "environment": env,
            "status": "active", "source": "servicenow_cmdb",
        })
        merge_rel(driver, "Database", eid, "Organization", "org-aura", "BELONGS_TO")

    # Services STORED_IN databases
    stored_in = [
        ("svc-claims-api",       "db-claims-pg"),
        ("svc-claims-processor", "db-claims-pg"),
        ("svc-payment-api",      "db-payment-pg"),
        ("svc-payment-processor","db-payment-pg"),
        ("svc-iam-auth",         "db-iam-pg"),
        ("svc-iam-token",        "db-iam-pg"),
        ("svc-fraud-api",        "db-fraud-redis"),
        ("svc-fraud-model",      "db-fraud-redis"),
        ("svc-lake-ingest",      "db-lake-s3"),
        ("svc-lake-transform",   "db-lake-s3"),
        ("svc-policy-api",       "db-policy-oracle"),
        ("svc-policy-engine",    "db-policy-oracle"),
    ]
    for svc, db in stored_in:
        merge_rel(driver, "Service", svc, "Database", db, "STORED_IN")

    # ── 9. Security Findings (Wiz) ────────────────────────────────────────────
    print("▶ Security Findings...")
    findings = [
        ("wiz-001", "Public S3 bucket exposes PII data",              "CRITICAL", "open",     "db-lake-s3",        "Missing S3 bucket policy blocking public access"),
        ("wiz-002", "Unpatched CVE-2024-3400 in PAN-OS",              "CRITICAL", "open",     "infra-claims-vm-1", "PAN-OS firewall running vulnerable version"),
        ("wiz-003", "Overly permissive IAM role on fraud GPU node",   "HIGH",     "open",     "infra-fraud-gpu",   "IAM role allows s3:* on all buckets"),
        ("wiz-004", "Database port 5432 exposed to 0.0.0.0/0",        "HIGH",     "open",     "db-claims-pg",      "Security group allows unrestricted PostgreSQL access"),
        ("wiz-005", "Container running as root in payment cluster",    "HIGH",     "in_review","infra-payment-k8s", "Pod security context missing runAsNonRoot"),
        ("wiz-006", "Missing encryption at rest on Oracle DB",        "MEDIUM",   "open",     "db-policy-oracle",  "RDS instance does not have storage encryption enabled"),
        ("wiz-007", "TLS 1.0 enabled on claims load balancer",        "MEDIUM",   "resolved", "infra-claims-lb",   "ALB listener accepts deprecated TLS 1.0 connections"),
        ("wiz-008", "API key hardcoded in fraud-ml-model repo",       "HIGH",     "open",     "repo-fraud-model",  "AWS_ACCESS_KEY_ID found in plain text in config.py"),
    ]
    for eid, title, severity, status, affected_eid, remediation in findings:
        merge_node(driver, "SecurityFinding", eid, {
            "name": title, "title": title, "severity": severity,
            "status": status, "remediation": remediation,
            "cvss": {"CRITICAL": 9.8, "HIGH": 7.5, "MEDIUM": 5.5}.get(severity, 4.0),
            "source": "wiz",
        })
        # Link finding to affected resource (infra, db, or repo)
        for label in ("Infrastructure", "Database", "Repository"):
            run(driver, f"""
                MATCH (resource:{label} {{externalId: $eid}})
                MATCH (finding:SecurityFinding {{externalId: $fid}})
                MERGE (resource)-[:HAS_FINDING]->(finding)
            """, eid=affected_eid, fid=eid)
        merge_rel(driver, "SecurityFinding", eid, "Organization", "org-aura", "BELONGS_TO")

    # ── 10. Incidents (ServiceNow) ────────────────────────────────────────────
    print("▶ Incidents...")
    incidents = [
        ("inc-001", "INC0001234", "Payment gateway latency spike > 5s",         "HIGH",     "in_progress", "svc-payment-api"),
        ("inc-002", "INC0001235", "Claims API returning 502 errors intermittently","CRITICAL","resolved",    "svc-claims-api"),
        ("inc-003", "INC0001236", "IAM auth service token expiry bug",           "MEDIUM",   "resolved",    "svc-iam-token"),
        ("inc-004", "INC0001237", "Fraud model scoring latency degraded",        "HIGH",     "open",        "svc-fraud-model"),
    ]
    for eid, number, title, severity, state, affected_svc in incidents:
        merge_node(driver, "Incident", eid, {
            "name": title, "number": number, "title": title,
            "severity": severity, "state": state, "source": "servicenow",
        })
        merge_rel(driver, "Service", affected_svc, "Incident", eid, "HAS_INCIDENT")
        merge_rel(driver, "Incident", eid, "Organization", "org-aura", "BELONGS_TO")

    print("✓ Seed complete!\n")


# ── Summary ───────────────────────────────────────────────────────────────────
def print_summary(driver):
    with driver.session(database=NEO4J_DATABASE) as s:
        result = s.run("""
            MATCH (n) WHERE n.source IN ['seed','git','servicenow_cmdb','wiz','servicenow']
            RETURN labels(n)[0] AS label, count(n) AS count
            ORDER BY count DESC
        """)
        print("─" * 35)
        print(f"{'Node Type':<22} {'Count':>5}")
        print("─" * 35)
        total = 0
        for row in result:
            print(f"{row['label']:<22} {row['count']:>5}")
            total += row["count"]
        print("─" * 35)
        print(f"{'TOTAL':<22} {total:>5}")

    with driver.session(database=NEO4J_DATABASE) as s:
        result = s.run("""
            MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS count ORDER BY count DESC
        """)
        print("\n─" * 18)
        print(f"{'Relationship':<28} {'Count':>5}")
        print("─" * 35)
        for row in result:
            print(f"{row['rel']:<28} {row['count']:>5}")


if __name__ == "__main__":
    print(f"Connecting to {NEO4J_URI} ...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        print("Connected ✓\n")
        seed(driver)
        print_summary(driver)
    finally:
        driver.close()

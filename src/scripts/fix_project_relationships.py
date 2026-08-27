"""
Fix missing Project relationships in Neo4j.

Adds direct edges from Services, Incidents, Infrastructure, Databases,
and SecurityFindings to their parent Project nodes.

Run from repo root:
    py -m src.scripts.fix_project_relationships
"""
from __future__ import annotations
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from neo4j import GraphDatabase

NEO4J_URI      = os.getenv("NEO4J_URI",      "neo4j://127.0.0.1:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

FIXES = [
    (
        "Service ──BELONGS_TO──► Project  (via HOSTED_IN → Repository → Project)",
        """
        MATCH (svc:Service)-[:HOSTED_IN]->(repo:Repository)-[:BELONGS_TO]->(proj:Project)
        MERGE (svc)-[:BELONGS_TO]->(proj)
        RETURN count(*) AS created
        """,
    ),
    (
        "Incident ──AFFECTS_PROJECT──► Project  (via Service)",
        """
        MATCH (svc:Service)-[:HAS_INCIDENT]->(inc:Incident)
        MATCH (svc)-[:BELONGS_TO]->(proj:Project)
        MERGE (inc)-[:AFFECTS_PROJECT]->(proj)
        RETURN count(*) AS created
        """,
    ),
    (
        "Infrastructure ──BELONGS_TO──► Project  (via Service RUNS_ON)",
        """
        MATCH (svc:Service)-[:RUNS_ON]->(infra:Infrastructure)
        MATCH (svc)-[:BELONGS_TO]->(proj:Project)
        MERGE (infra)-[:BELONGS_TO]->(proj)
        RETURN count(*) AS created
        """,
    ),
    (
        "Database ──BELONGS_TO──► Project  (via Service STORED_IN)",
        """
        MATCH (svc:Service)-[:STORED_IN]->(db:Database)
        MATCH (svc)-[:BELONGS_TO]->(proj:Project)
        MERGE (db)-[:BELONGS_TO]->(proj)
        RETURN count(*) AS created
        """,
    ),
    (
        "SecurityFinding ──AFFECTS_PROJECT──► Project  (via affected resource)",
        """
        MATCH (resource)-[:HAS_FINDING]->(sf:SecurityFinding)
        MATCH (resource)-[:BELONGS_TO]->(proj:Project)
        MERGE (sf)-[:AFFECTS_PROJECT]->(proj)
        RETURN count(*) AS created
        """,
    ),
]

VERIFY = """
MATCH (proj:Project)
OPTIONAL MATCH (proj)<-[:BELONGS_TO|AFFECTS_PROJECT]-(incident:Incident)
OPTIONAL MATCH (proj)<-[:BELONGS_TO|AFFECTS_PROJECT]-(sf:SecurityFinding)
OPTIONAL MATCH (proj)<-[:BELONGS_TO]-(svc:Service)
OPTIONAL MATCH (proj)<-[:BELONGS_TO]-(infra:Infrastructure)
OPTIONAL MATCH (proj)<-[:BELONGS_TO]-(db:Database)
RETURN proj.name AS project,
       count(DISTINCT svc)      AS services,
       count(DISTINCT infra)    AS infra,
       count(DISTINCT db)       AS databases,
       count(DISTINCT incident) AS incidents,
       count(DISTINCT sf)       AS security_findings
ORDER BY proj.name
"""


def run(driver, cypher: str, **params):
    with driver.session(database=NEO4J_DATABASE) as s:
        return list(s.run(cypher, **params))


if __name__ == "__main__":
    print(f"Connecting to {NEO4J_URI} ...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        print("Connected ✓\n")

        for label, cypher in FIXES:
            rows = run(driver, cypher)
            count = rows[0]["created"] if rows else 0
            print(f"  ✓  {label}")
            print(f"     → {count} relationship(s) merged\n")

        print("─" * 70)
        print("Verification — project coverage after fix:\n")
        rows = run(driver, VERIFY)
        print(f"  {'Project':<26} {'Svc':>4} {'Infra':>5} {'DB':>4} {'Inc':>4} {'SecFinding':>10}")
        print("  " + "─" * 58)
        for r in rows:
            print(
                f"  {r['project']:<26} {r['services']:>4} {r['infra']:>5}"
                f" {r['databases']:>4} {r['incidents']:>4} {r['security_findings']:>10}"
            )
        print()
    finally:
        driver.close()

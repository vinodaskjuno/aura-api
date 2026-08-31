"""Build a test plan from the knowledge graph.

Code analysis already writes what a planner needs (src/graph/code_graph.py:302-318):

    Repository -[:EXPOSES]->    API        {method, path, framework, sourceFile}
    Repository -[:IMPLEMENTS]-> Service    {name}
    Repository -[:DEPENDS_ON]-> Dependency {name, ecosystem}

so the deterministic plan needs no LLM call — the graph IS the specification. That
matters beyond cost: a generated plan is reproducible, and every case carries the
element id of the node it verifies, which is what lets results be written back as an
edge and lets a later change select only the cases covering what moved.
"""
from __future__ import annotations

import logging
import re

from src.qatest.types import Case

log = logging.getLogger(__name__)

# A path with a parameter cannot be fetched without knowing a value, and guessing one
# produces a red test that says nothing. Those become skipped cases with the reason
# attached rather than being dropped silently — a plan should show what it declined.
_PARAM = re.compile(r"[{:<][^/}>]+[}>]?")


def _is_parameterised(path: str) -> bool:
    return bool(_PARAM.search(path or ""))


def _browser_reachable(method: str, path: str) -> bool:
    """A GET with no parameters is something a browser can simply open."""
    return method.upper() == "GET" and not _is_parameterised(path)


def fetch_facts(project_id: str) -> dict:
    """Read the project's APIs, services and dependencies from the graph.

    Returns empty lists rather than raising when the graph is unreachable: a run
    should report "nothing to plan" and why, not crash the caller.
    """
    apis: list[dict] = []
    services: list[dict] = []
    dependencies: list[dict] = []

    # Archival in code_graph is on the RELATIONSHIP (rel.active = false), not the node
    # — _archive_stale never touches node properties. Filtering on a node-level
    # `archived` flag both missed real archival and made Neo4j warn on every query
    # that the property does not exist. Traverse from the Repository instead, which is
    # how archival is actually expressed.
    try:
        from src.graph.backends import routed_session
        with routed_session() as session:
            rows = session.run(
                "MATCH (r:Repository)-[rel:EXPOSES]->(n:API) "
                "WHERE n.projectId = $pid AND coalesce(rel.active, true) = true "
                "  AND coalesce(n.status, 'active') = 'active' "
                "RETURN DISTINCT n.externalId AS eid, n.method AS method, n.path AS path, "
                "n.framework AS framework, n.sourceFile AS sourceFile, n.name AS name",
                {"pid": project_id})
            apis = [dict(r) for r in rows]

            rows = session.run(
                "MATCH (r:Repository)-[rel:IMPLEMENTS]->(n:Service) "
                "WHERE n.projectId = $pid AND coalesce(rel.active, true) = true "
                "  AND coalesce(n.status, 'active') = 'active' "
                "RETURN DISTINCT n.externalId AS eid, n.name AS name",
                {"pid": project_id})
            services = [dict(r) for r in rows]

            rows = session.run(
                "MATCH (r:Repository)-[rel:DEPENDS_ON]->(n:Dependency) "
                "WHERE n.projectId = $pid AND coalesce(rel.active, true) = true "
                "  AND coalesce(n.status, 'active') = 'active' "
                "RETURN DISTINCT n.externalId AS eid, n.name AS name, n.ecosystem AS ecosystem",
                {"pid": project_id})
            dependencies = [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001 — an unreachable graph is a reported state
        log.warning("qatest: graph read failed for %s: %s", project_id, exc)

    return {"apis": apis, "services": services, "dependencies": dependencies}


def build_plan(project_id: str, facts: dict | None = None) -> list[Case]:
    """One case per API node, plus a smoke case per service.

    Deterministic and ordered, so two runs of an unchanged graph produce the same
    plan in the same sequence — which is what makes step-by-step evidence comparable
    between runs.
    """
    facts = facts if facts is not None else fetch_facts(project_id)
    cases: list[Case] = []

    # Always check the application ROOT first. Without it a frontend cannot be tested
    # at all: code analysis extracts server-side route tables, and a React SPA has no
    # such table, so the graph holds no frontend routes to plan from. This one case
    # covers what actually matters for a UI — does it load, does it render, does it
    # throw — and it is the first thing worth knowing about a backend too.
    cases.append(Case(
        case_id="root-001", kind="ui", name="application loads",
        verifies_label="", verifies_eid="", method="GET", path="/"))

    for api in sorted(facts.get("apis") or [],
                      key=lambda a: (str(a.get("path") or ""), str(a.get("method") or ""))):
        method = str(api.get("method") or "GET").upper()
        path = str(api.get("path") or "/")
        eid = str(api.get("eid") or "")
        cases.append(Case(
            case_id=f"api-{len(cases):03d}",
            kind="ui" if _browser_reachable(method, path) else "api",
            name=f"{method} {path}",
            verifies_label="API", verifies_eid=eid,
            method=method, path=path,
            source_file=str(api.get("sourceFile") or ""),
        ))

    for svc in sorted(facts.get("services") or [], key=lambda s: str(s.get("name") or "")):
        cases.append(Case(
            case_id=f"smoke-{len(cases):03d}",
            kind="smoke",
            name=f"service {svc.get('name')} is reachable",
            verifies_label="Service", verifies_eid=str(svc.get("eid") or ""),
        ))

    return cases


def covered_nodes(cases: list[Case]) -> list[dict[str, str]]:
    """The distinct graph nodes a plan touches, for the report."""
    seen: dict[str, dict[str, str]] = {}
    for c in cases:
        if c.verifies_eid:
            seen[c.verifies_eid] = {"label": c.verifies_label, "externalId": c.verifies_eid}
    return sorted(seen.values(), key=lambda d: d["externalId"])

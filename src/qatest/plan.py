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

# Cases the user did not select are still PLANNED — they are simply not run. Keeping
# the distinction is what lets coverage say "not covered because you excluded it"
# rather than blaming the application.
ALL_KINDS: tuple[str, ...] = ("ui", "api", "smoke", "structure", "stack",
                              "policy")

# The application-root case survives every filter. It is the only case a frontend can
# be tested by (code analysis extracts server-side route tables; a React SPA has none),
# and `runner._base_for` special-cases its id. A run without it tests nothing at all.
ROOT_CASE_ID = "root-001"

log = logging.getLogger(__name__)

# A path with a parameter cannot be fetched without knowing a value, and guessing one
# produces a red test that says nothing. Those become skipped cases with the reason
# attached rather than being dropped silently — a plan should show what it declined.
_PARAM = re.compile(r"[{:<][^/}>]+[}>]?")


def _is_parameterised(path: str) -> bool:
    return bool(_PARAM.search(path or ""))


def why_unrunnable(case: Case) -> str:
    """Why this case cannot be executed, in the words its step will carry — or "".

    The single source of truth for "planned, but cannot run". Three places used to
    decide this independently: the planner (to pick a kind), the runner (to skip), and
    a reader trying to explain a low coverage number. They disagreed — the runner
    tested `"{" in path or ":" in path.split("/")[-1]` while the planner used the
    `_PARAM` regex. The plan is what the preview shows the user, so the plan is the
    authority and the runner now reads this field.
    """
    if case.kind in ("structure", "stack", "policy"):
        # A file check needs nothing started; a stack check needs the stack, which the
        # runner starts for it. Neither is ever "planned but unrunnable".
        return ""
    if case.kind == "smoke":
        return "no HTTP address for a Service node"
    if (case.method or "GET").upper() != "GET":
        return f"{case.method} needs a request body the graph does not describe"
    if _is_parameterised(case.path):
        return "path parameter has no known value"
    return ""


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


def structure_cases(root) -> list[Case]:
    """File checks for a project that does not serve HTTP.

    Planned from the working copy rather than the graph, because the graph holds API
    and Service nodes and an RPA application has neither. Without these, a BPMN or
    Airflow project can only ever be reported `unavailable` — true, and useless.
    """
    from src.qatest import structure

    out: list[Case] = []
    for check in structure.plan_checks(root):
        out.append(Case(
            case_id=check.check_id, kind="structure", name=check.name,
            path=check.rel_path, source_file=check.rel_path,
            # `method` carries the validator: it is the field the runner already
            # ships, and adding one to Case is a wire break for older agents.
            method=check.validator))
    return out


def policy_cases(root) -> list[Case]:
    """NIST controls over this project's IaC, as cases.

    Same shape as `structure_cases`, including carrying the validator name in `method`:
    `Case` has no spare structured field and adding one is a wire break for every
    deployed runner.
    """
    from src.qatest import policy

    return [Case(case_id=check.check_id, kind="policy", name=check.name,
                 path=check.rel_path, source_file=check.rel_path,
                 method=check.validator)
            for check in policy.plan_checks(root)]


def stack_cases(root) -> list[Case]:
    """Questions for the running stack a converted project ships.

    Only when the working copy actually contains one. A project with no compose file
    gets none of these, which is every project that did not come out of a migration.
    """
    from src.qatest import appserver, stack

    spec = appserver.detect_compose(root)
    if not spec:
        return []
    from src.migration import runtime as mig_runtime
    target = next((name for name in mig_runtime.RUNTIMES
                   if name in str(spec.directory).lower()
                   or _compose_mentions(spec, name)), "")
    checks = stack.for_target(target)
    if not checks:
        return []
    return [Case(case_id=a.assertion_id, kind="stack", name=a.name,
                 path=a.path, method=a.checker) for a in checks.assertions]


def _compose_mentions(spec, name: str) -> bool:
    from pathlib import Path as _P
    for candidate in ("docker-compose.yml", "docker-compose.yaml",
                      "compose.yml", "compose.yaml"):
        f = _P(spec.directory) / candidate
        if f.is_file() and name in f.read_text(errors="replace").lower():
            return True
    return False


def build_plan(project_id: str, facts: dict | None = None, root=None) -> list[Case]:
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
        case_id=ROOT_CASE_ID, kind="ui", name="application loads",
        verifies_label="", verifies_eid="", method="GET", path="/"))

    for api in sorted(facts.get("apis") or [],
                      key=lambda a: (str(a.get("path") or ""), str(a.get("method") or ""))):
        method = str(api.get("method") or "GET").upper()
        path = str(api.get("path") or "/")
        eid = str(api.get("eid") or "")
        cases.append(Case(
            case_id=f"api-{len(cases):03d}",
            # Every API node is an `api` case now, whatever its method or path shape.
            # Whether a browser can open it is a detail of HOW it runs, recorded in
            # skip_reason — not what kind of thing it is.
            kind="api",
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

    # File checks, when a working copy is readable. They need no server, so they are
    # the only thing that can be asserted about a project which does not have one.
    if root is not None:
        cases.extend(structure_cases(root))
        cases.extend(policy_cases(root))
        # And, for a project that ships a runnable stack, what can only be asked of
        # it once it is up.
        cases.extend(stack_cases(root))

    for case in cases:
        case.skip_reason = why_unrunnable(case)
    return cases


def filter_by_kind(cases: list[Case], kinds: list[str] | None = None,
                   skip_unrunnable: bool = False) -> list[Case]:
    """The subset of `cases` the caller asked for.

    Idempotent — filtering an already-filtered list is a no-op — which is what lets
    the same filter run server-side at claim time AND inside `service.execute` without
    the two fighting. The claim-time pass is the one that matters for compatibility: a
    runner that has never heard of `kinds` just receives a shorter list and cannot tell.

    An empty or absent `kinds` means ALL, so every existing caller is unchanged.
    """
    selected = [k for k in (kinds or []) if k in ALL_KINDS]
    out = []
    for case in cases:
        # The root case survives every filter; see ROOT_CASE_ID.
        if case.case_id != ROOT_CASE_ID:
            if selected and case.kind not in selected:
                continue
            if skip_unrunnable and (case.skip_reason or why_unrunnable(case)):
                continue
        out.append(case)
    return out


def graph_totals(facts: dict) -> dict[str, int]:
    """How many nodes this project has that a test could verify.

    The denominator for coverage. API and Service are counted separately because a
    Service case can never pass — `runner` records every smoke case as skipped, since a
    Service node names a code unit and not an address — so folding them into one
    number would cap coverage below 100% for a reason that has nothing to do with the
    application.
    """
    return {"apis": len(facts.get("apis") or []),
            "services": len(facts.get("services") or [])}


def covered_nodes(cases: list[Case],
                  statuses: dict[str, str] | None = None) -> list[dict[str, str]]:
    """The distinct graph nodes a plan touches, and how each one actually fared.

    `statuses` maps case_id -> worst step status. Without it this reports the nodes the
    plan REFERENCED, which is what it used to mean and what every existing caller
    expects. With it, each node also carries a `result`, and that is the difference
    between "we planned to check this" and "this is verified" — a run where every case
    failed used to report full coverage.
    """
    seen: dict[str, dict[str, str]] = {}
    rank = {"passed": 0, "unemulated": 1, "skipped": 2, "failed": 3}
    for c in cases:
        if not c.verifies_eid:
            continue
        entry = seen.setdefault(c.verifies_eid, {
            "label": c.verifies_label, "externalId": c.verifies_eid})
        if statuses is None:
            continue
        # Worst status wins: a node checked by two cases, one passing and one failing,
        # is not verified.
        status = statuses.get(c.case_id, "skipped")
        if rank.get(status, 3) >= rank.get(entry.get("result", "passed"), 0):
            entry["result"] = status
    return sorted(seen.values(), key=lambda d: d["externalId"])


# The preview is read every time the launcher opens, and the graph changes only when
# the project is re-analysed. Enqueue and claim still read fresh — the graph can move
# between the two — so this cache is scoped to the preview alone.
_PREVIEW_TTL_S = 30.0
_preview_cache: dict[str, tuple[float, dict]] = {}


def preview(project_id: str, refresh: bool = False) -> dict:
    """What a run WOULD do, so the launcher can offer a choice before starting one.

    Reports `graphReady: False` with a reason rather than an empty plan, because "0
    cases" and "this project was never analysed" look identical otherwise and only one
    of them is the user's problem.
    """
    import time

    now = time.monotonic()
    hit = _preview_cache.get(project_id)
    if not refresh and hit and (now - hit[0]) < _PREVIEW_TTL_S:
        return hit[1]

    facts = fetch_facts(project_id)
    # WITH the working copy, like the claim path does. Without it `build_plan` skips
    # every check that reads files, so the preview under-reported the run: structure has
    # always shown 0 here while a real run executed them, and the picker offered a kind
    # with a count of zero that nobody would ever select.
    #
    # Best-effort: an API that cannot see the code still previews the graph-derived
    # cases, which is what this did before.
    try:
        from src.qatest import appserver
        root, _checked = appserver.locate(project_id)
    except Exception as exc:                                  # noqa: BLE001
        log.debug("qa plan preview: no working copy for %s: %s", project_id, exc)
        root = None
    cases = build_plan(project_id, facts, root=root)
    totals = graph_totals(facts)

    from src.qatest import emulators
    counts = {k: sum(1 for c in cases if c.kind == k) for k in ALL_KINDS}
    runnable = sum(1 for c in cases if not c.skip_reason)
    ready = bool(totals["apis"] or totals["services"])

    out = {
        "projectId": project_id,
        "totalCases": len(cases),
        "runnableCases": runnable,
        "counts": counts,
        "cases": [c.as_dict() for c in cases],
        "clouds": [c.name for c in emulators.clouds_for(facts.get("dependencies") or [])],
        "graphTotals": totals,
        "graphReady": ready,
        "reason": ("" if ready else
                   "The knowledge graph holds no API or Service nodes for this "
                   "project. Analyse it in Dev Workspace first — a run would test "
                   "only that the application loads."),
    }
    _preview_cache[project_id] = (now, out)
    return out

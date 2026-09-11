"""Role-aware dashboard: what each role is shown, and what happens when a source dies.

The guards outnumber the behaviour tests on purpose. The failure modes this
feature has are all quiet ones — a role silently falling through to the wrong
view, an unmeasured value rendering as a confident `0`, a dead source blanking
the landing page — and none of them look like a failure at the call site.
"""
from __future__ import annotations

import pytest

from src.services import role_metrics as rm


# ── Fake sources ─────────────────────────────────────────────────────────────

class _Row(dict):
    """Neo4j rows are mapping-like; dict is close enough for these queries."""


class _Session:
    def __init__(self, rows_for, calls):
        self._rows_for, self._calls = rows_for, calls

    def run(self, cypher, *a, **k):
        self._calls.append(cypher)
        return self._rows_for(cypher)

    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def sources(monkeypatch):
    """Every source `_Data` can read, swapped for in-memory doubles.

    Returns a handle whose attributes are assigned by each test, plus a `calls`
    counter per source so "how many times did one page load hit the graph" is a
    thing a test can assert rather than a thing we hope about.
    """
    class Handle:
        projects: list = []
        runs: list = []
        jobs: list = []
        test_runs: list = []
        token_usage: list = []
        runners: list = []
        outbox: dict = {}
        engines: list = ["neo4j"]
        engine_up: dict = {"neo4j": True}
        graph_labels: dict = {}
        graph_projects: dict = {}
        critical: int = 0
        report: dict | None = None
        raise_on: set = set()
        calls: dict = {}

    h = Handle()
    h.raise_on = set()
    h.calls = {"graph": [], "dynamo": [], "s3": []}

    def boom(name):
        if name in h.raise_on:
            raise RuntimeError(f"{name} is down")

    # ── DynamoDB ──
    def scan_items(table, filter_expr=None, limit=500):
        h.calls["dynamo"].append(table)
        boom(table)
        return {"projects": h.projects, "scheduler-state": h.jobs,
                "test-results": h.test_runs, "token-usage": h.token_usage}.get(table, [])

    def query_items(table, pk_name, pk_value, **k):
        h.calls["dynamo"].append(table)
        boom(table)
        return []

    import src.database.dynamo_client as db
    monkeypatch.setattr(db, "scan_items", scan_items)
    monkeypatch.setattr(db, "query_items", query_items)

    # ── Pipeline runs ──
    import src.services.ontology_version_service as ovs
    def list_versions(limit=50, **k):
        boom("runs")
        return list(h.runs)
    monkeypatch.setattr(ovs, "list_versions", list_versions)

    # ── Graph ──
    import src.graph.neo4j_client as neo
    def rows_for(cypher):
        if "UNWIND labels" in cypher:
            return [_Row(label=k, cnt=v) for k, v in h.graph_labels.items()]
        if "n.projectId IS NOT NULL" in cypher:
            return [_Row(pid=k, cnt=v) for k, v in h.graph_projects.items()]
        class _One:
            def single(_self): return _Row(cnt=h.critical)
            def __iter__(_self): return iter([])
        return _One()

    def session():
        boom("graph")
        return _Session(rows_for, h.calls["graph"])

    monkeypatch.setattr(neo, "is_available", lambda: "graph" not in h.raise_on)
    monkeypatch.setattr(neo, "session", session)

    # ── Outbox and engines ──
    import src.graph.outbox as outbox
    import src.graph.backends as backends
    monkeypatch.setattr(outbox, "depth", lambda backend=None: (boom("outbox") or h.outbox))
    monkeypatch.setattr(backends, "configured_names", lambda: list(h.engines))
    monkeypatch.setattr(backends, "get_backend",
                        lambda name: type("B", (), {
                            "is_available": lambda _s, n=name: h.engine_up.get(n, True)})())

    # ── QA ──
    import src.qatest.queue as queue
    import src.qatest.evidence as evidence
    monkeypatch.setattr(queue, "online_runners", lambda *a, **k: (boom("runners") or h.runners))
    def read_report(pid, rid):
        h.calls["s3"].append(rid)
        boom("s3")
        return h.report
    monkeypatch.setattr(evidence, "read_report", read_report)

    return h


def _view(sources, role, **user):
    return rm.build_view({"role": role, "username": "tester",
                          "userId": "u1", **user})


def _metrics(view) -> dict:
    """Every metric in the payload, by label."""
    out = {}
    for block in view["blocks"]:
        for item in block.get("items", []):
            out[item["label"]] = item
    return out


# ── The registry cannot drift from the roles ─────────────────────────────────

def test_every_builtin_role_has_a_view():
    """A role in ROLE_PERMISSIONS with no view here would land its holder on a
    developer's dashboard, which looks like a feature rather than an omission."""
    from src.services.auth_service import ROLE_PERMISSIONS
    assert not set(ROLE_PERMISSIONS) - set(rm.ROLE_VIEWS)


def test_a_role_without_a_view_fails_at_import(monkeypatch):
    """The guard has to actually fire — a check nothing can trip is decoration."""
    import src.services.auth_service as auth
    monkeypatch.setitem(auth.ROLE_PERMISSIONS, "auditor", ["dashboard"])
    with pytest.raises(RuntimeError, match="auditor"):
        rm._assert_every_role_has_a_view()


def test_an_unknown_role_still_gets_a_dashboard(sources):
    """Directory users carry whatever role name their organisation invented."""
    view = _view(sources, "regional_delivery_lead_emea")
    assert view["headline"]["text"]
    assert view["blocks"]


# ── Absent is not zero ───────────────────────────────────────────────────────

def test_a_metric_with_no_data_is_unmeasured_not_zero(sources):
    """The regression the whole colour rule rests on. `0%` coverage reads as
    'nothing works'; the truth is that nothing has run."""
    sources.projects = [{"projectId": "p1", "name": "api", "userId": "u1"}]
    view = _view(sources, "user_qa")

    coverage = _metrics(view)["Graph coverage"]
    assert coverage["value"] is None
    assert coverage["state"] == "unmeasured"
    assert coverage["basis"] == "no run has completed"


def test_metric_refuses_to_publish_a_value_for_an_absent_state():
    """A caller passing a stale 0 alongside an absent state must not leak it."""
    assert rm.metric("Coverage", None)["state"] == "unmeasured"
    assert rm.metric("Coverage", 0)["value"] == 0            # a real zero survives
    assert rm.metric("Coverage", 0)["state"] == "ok"


def test_a_real_zero_is_not_turned_into_an_absence(sources):
    """Zero failures is a fact, and must read as one."""
    sources.runs = [{"versionId": "r1", "pipeline": "git", "status": "success",
                     "startedAt": "2099-01-01T00:00:00+00:00", "durationMs": 10}]
    view = _view(sources, "user_ops")
    rate = _metrics(view)["Failure rate"]
    assert rate["value"] == 0 and rate["state"] == "ok"


# ── A dead source degrades one block, not the page ───────────────────────────

def test_a_failing_source_yields_unavailable_with_a_reason(sources):
    sources.raise_on = {"graph"}
    sources.projects = [{"projectId": "p1", "name": "api", "userId": "u1"}]
    view = _view(sources, "user_dev")

    services = _metrics(view)["Services"]
    assert services["value"] is None
    assert services["state"] == "unavailable"
    assert "reason" in services and services["reason"]


def test_a_failing_source_does_not_take_the_rest_of_the_page_down(sources):
    """The graph is down; the DynamoDB half of the dashboard must still render."""
    sources.raise_on = {"graph"}
    sources.projects = [{"projectId": "p1", "name": "api", "userId": "u1"}]
    view = _view(sources, "user_dev")

    assert view["headline"]["text"]
    assert any(b["kind"] == "list" for b in view["blocks"])
    assert "graph" in view["degraded"]


def test_a_degraded_source_is_named_not_hidden(sources):
    sources.raise_on = {"projects"}
    view = _view(sources, "user_dev")
    assert view["degraded"] == ["projects"]


def test_a_builder_bug_still_returns_a_page(monkeypatch, sources):
    """The dashboard is the landing page. A 500 here looks like the product is down."""
    monkeypatch.setitem(rm.ROLE_VIEWS, "user_dev",
                        lambda data: (_ for _ in ()).throw(ValueError("bad builder")))
    view = _view(sources, "user_dev")
    assert view["headline"]["state"] == "unavailable"
    assert "bad builder" in view["headline"]["detail"]


# ── The views say what they are supposed to say ──────────────────────────────

def test_ops_surfaces_outbox_divergence_as_the_headline(sources):
    """The acceptance test: this is one of the three real problems that was
    invisible until someone read DynamoDB by hand."""
    sources.outbox = {"memgraph": 500}
    view = _view(sources, "user_ops")
    assert "500" in view["headline"]["text"]
    assert view["headline"]["state"] == "critical"
    assert any("memgraph" in i["title"] for i in view["attention"])


def test_ops_flags_a_job_that_has_never_run(sources):
    sources.jobs = [{"jobId": "qa_reaper", "lastRunAt": None}]
    view = _view(sources, "user_ops")
    assert any("never recorded a run" in i["title"] for i in view["attention"])


def test_ops_says_nominal_when_nothing_is_wrong(sources):
    view = _view(sources, "user_ops")
    assert view["headline"]["state"] == "ok"
    assert view["attention"] == []


def test_qa_reports_untestable_separately_from_failed(sources):
    """A run with 0 failures and a run where nothing could execute looked
    identical before this metric existed."""
    sources.test_runs = [{"testRunId": "r1", "projectId": "p1", "type": "qatest",
                          "status": "unavailable", "totalCases": 14,
                          "totalPassed": 0, "totalFailed": 0,
                          "totalUnemulated": 14, "totalSkipped": 0,
                          "createdAt": "2099-01-01T00:00:00+00:00"}]
    view = _view(sources, "user_qa")
    untestable = _metrics(view)["Untestable"]
    assert untestable["value"] == 100
    assert untestable["state"] == "critical"
    assert "14 of 14" in untestable["basis"]


def test_qa_flags_a_queue_with_no_runner(sources):
    sources.test_runs = [{"testRunId": "r1", "projectId": "p1", "type": "qatest",
                          "status": "queued", "createdAt": "2099-01-01T00:00:00+00:00"}]
    sources.runners = []
    view = _view(sources, "user_qa")
    assert any("no runner has claimed" in i["title"] for i in view["attention"])


def test_pm_derives_the_stage_from_runs_that_succeeded(sources):
    """A stage is reached because a pipeline succeeded, never because a field says so."""
    sources.projects = [{"projectId": "p1", "name": "api", "userId": "u1",
                         "createdAt": "2099-01-01T00:00:00+00:00"}]
    sources.runs = [
        {"versionId": "a", "projectId": "p1", "pipeline": "git",
         "status": "success", "startedAt": "2099-01-02T00:00:00+00:00"},
        {"versionId": "b", "projectId": "p1", "pipeline": "qa-mind",
         "status": "failed", "startedAt": "2099-01-03T00:00:00+00:00"},
    ]
    view = _view(sources, "project_manager")
    board = next(b for b in view["blocks"] if b["kind"] == "pipeline")
    marks = board["rows"][0]["marks"]
    assert marks[0] == "done"        # registered
    assert marks[1] == "done"        # analysed — the git run succeeded
    assert marks[3] == "failed"      # tested — the qa-mind run failed


def test_po_does_not_imply_a_trend_it_cannot_draw(sources):
    """Nothing snapshots finding counts, so every PO number is a current value
    and the page has to say so rather than implying history."""
    view = _view(sources, "product_owner")
    notes = [b for b in view["blocks"] if b["kind"] == "note"]
    assert any("current count" in b["text"] for b in notes)


def test_maintainer_keeps_the_label_counts_no_other_role_needs(sources):
    sources.graph_labels = {"Service": 125, "Repository": 50}
    view = _view(sources, "ontology_maintainer")
    inventory = next(b for b in view["blocks"]
                     if b["kind"] == "list" and b["title"] == "Label inventory")
    assert [c["text"] for c in inventory["rows"][0]["cells"]] == ["Service", "125"]


# ── Cost ─────────────────────────────────────────────────────────────────────

def test_one_page_load_reads_each_source_at_most_once(sources):
    """`_Data` memoises per request. Without it the developer view alone would
    issue the per-project graph query once per project."""
    sources.projects = [{"projectId": f"p{i}", "name": f"s{i}", "userId": "u1"}
                        for i in range(12)]
    sources.graph_labels = {"Service": 5}
    sources.graph_projects = {f"p{i}": 3 for i in range(12)}

    _view(sources, "user_dev")
    # Both halves matter: `>= 1` proves the graph was actually consulted, so the
    # upper bound is not passing because nothing ran.
    assert 1 <= len(sources.calls["graph"]) <= 2      # labels + per-project
    assert sources.calls["dynamo"].count("projects") == 1


def test_qa_reads_the_evidence_report_only_for_the_latest_run(sources):
    """Coverage is not in DynamoDB. One S3 GET per page load, not one per run."""
    sources.test_runs = [
        {"testRunId": f"r{i}", "projectId": "p1", "type": "qatest",
         "status": "passed", "totalCases": 4, "totalPassed": 4,
         "createdAt": f"2099-01-0{i + 1}T00:00:00+00:00"}
        for i in range(3)
    ]
    sources.report = {"coverage": {"nodePct": 62, "nodeCovered": 18, "nodeTotal": 29}}
    view = _view(sources, "user_qa")
    assert len(sources.calls["s3"]) == 1
    assert _metrics(view)["Graph coverage"]["value"] == 62


def test_qa_survives_an_unreadable_evidence_bucket(sources):
    """Execution and untestability come off the queue row, so they must still
    report when S3 is down — only graph coverage is lost."""
    sources.raise_on = {"s3"}
    sources.test_runs = [{"testRunId": "r1", "projectId": "p1", "type": "qatest",
                          "status": "passed", "totalCases": 10, "totalPassed": 8,
                          "totalFailed": 2, "createdAt": "2099-01-01T00:00:00+00:00"}]
    view = _view(sources, "user_qa")
    metrics = _metrics(view)
    assert metrics["Graph coverage"]["state"] == "unavailable"
    assert metrics["Plan executed"]["value"] == 100


# ── Access ───────────────────────────────────────────────────────────────────

def test_pm_and_po_hold_dashboard_and_nothing_else():
    """They consume the picture; every other menu in this product can start a
    run, upload a repository or delete a project."""
    from src.services.auth_service import ROLE_PERMISSIONS
    assert ROLE_PERMISSIONS["project_manager"] == ["dashboard"]
    assert ROLE_PERMISSIONS["product_owner"] == ["dashboard"]


def _client(user: dict):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.routers import auth as auth_router
    from src.routers import dashboard as dashboard_router
    from src.routers import dashboard_view as view_router

    app = FastAPI()
    app.include_router(view_router.router)
    app.include_router(dashboard_router.router)
    app.dependency_overrides[auth_router.get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def test_a_project_manager_can_read_their_dashboard(sources):
    body = _client({"userId": "u1", "username": "anita", "role": "project_manager",
                    "permissions": ["dashboard"]}).get("/api/dashboard/view").json()
    assert body["role"] == "project_manager"
    assert body["roleLabel"] == "Project Manager"
    assert any(b["kind"] == "pipeline" for b in body["blocks"])


def test_a_role_without_the_dashboard_permission_is_refused(sources):
    response = _client({"userId": "u2", "username": "nobody", "role": "user_qa",
                        "permissions": []}).get("/api/dashboard/view")
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/api/dashboard/infrastructure",
                                  "/api/dashboard/applications",
                                  "/api/dashboard/data-landscape"])
def test_the_dead_endpoints_are_gone(sources, path):
    """Each was fetched on every page load, rendered nowhere, and counted labels
    that do not exist. Deleting the tile without the endpoint would have left
    four round trips per load serving nothing."""
    response = _client({"userId": "u1", "username": "a", "role": "admin",
                        "permissions": ["dashboard"]}).get(path)
    assert response.status_code == 404


def test_the_summary_no_longer_counts_a_project_without_a_project_id():
    """"Active Projects" read 23 against 5 real projects, because `status IS NULL`
    swept in every seed Project node.

    Asserted against the query text rather than a result: there is no graph in
    the unit suite, and the defect was entirely in this predicate.
    """
    import inspect
    from src.routers import dashboard
    cypher = inspect.getsource(dashboard.get_dashboard_summary)
    assert "p.status IS NULL" not in cypher
    assert "p.projectId IS NOT NULL" in cypher


# ── "We cannot tell" is not "nothing happened" ───────────────────────────────
#
# Found against live data: 95 of 185 pipeline runs carried no projectId and 89
# carried the single character "p", while the graph held 1,666 nodes. The first
# draft of these views reported that as "0% of the estate is understood" and
# "never analysed" — a confident, wrong claim built on absent evidence.

def _unattributable(sources):
    """Projects that exist, and runs that name none of them."""
    sources.projects = [{"projectId": "real-1", "name": "api", "userId": "u1",
                         "createdAt": "2000-01-01T00:00:00+00:00"}]
    sources.runs = [{"versionId": "r1", "projectId": "", "pipeline": "mcp",
                     "status": "success", "startedAt": "2099-01-01T00:00:00+00:00"},
                    {"versionId": "r2", "projectId": "p", "pipeline": "mcp",
                     "status": "success", "startedAt": "2099-01-01T00:00:00+00:00"}]
    return sources


def test_an_unattributable_project_is_unknown_not_unanalysed(sources):
    data = rm._Data({"role": "user_dev", "userId": "u1", "username": "t"})
    _unattributable(sources)
    assert data.evidence_for({"projectId": "real-1"}) == data.UNKNOWN


def test_a_genuinely_unanalysed_project_still_reads_as_none(sources):
    """The distinction must not swallow the real case: when every run names a
    known project, a project with no run really has not been analysed."""
    sources.projects = [{"projectId": "a", "name": "a", "userId": "u1"},
                        {"projectId": "b", "name": "b", "userId": "u1"}]
    sources.runs = [{"versionId": "r1", "projectId": "a", "pipeline": "mcp",
                     "status": "success", "startedAt": "2099-01-01T00:00:00+00:00"}]
    data = rm._Data({"role": "user_dev", "userId": "u1", "username": "t"})
    assert data.evidence_for({"projectId": "a"}) == data.ANALYSED
    assert data.evidence_for({"projectId": "b"}) == data.NOT_ANALYSED


def test_po_refuses_to_report_a_percentage_it_cannot_support(sources):
    _unattributable(sources)
    view = _view(sources, "product_owner")
    understood = _metrics(view)["Understood"]
    assert understood["value"] is None
    assert understood["state"] == "unmeasured"
    assert view["headline"]["state"] == "unavailable"
    assert "cannot be assessed" in view["headline"]["text"]


def test_pm_marks_an_undeterminable_stage_as_unknown(sources):
    _unattributable(sources)
    view = _view(sources, "project_manager")
    board = next(b for b in view["blocks"] if b["kind"] == "pipeline")
    assert board["rows"][0]["marks"][1] == "unknown"     # not "none"
    assert "cannot be determined" in board["legend"]


def test_the_developer_list_says_unknown_rather_than_never(sources):
    _unattributable(sources)
    view = _view(sources, "user_dev")
    projects = next(b for b in view["blocks"]
                    if b["kind"] == "list" and b["title"] == "Your projects")
    assert projects["rows"][0]["cells"][1]["text"] == "unknown"


def test_ops_reports_the_attribution_gap_as_a_defect(sources):
    """Nobody would have found this by reading a dashboard. They should be able to."""
    _unattributable(sources)
    view = _view(sources, "user_ops")
    assert any("name no known project" in i["title"] for i in view["attention"])


def test_the_developer_headline_does_not_claim_zero_when_it_cannot_tell(sources):
    """"0 of 3 analysed" above "3 with no attributable history" is a headline
    contradicting its own subtitle. Only the honest half survives."""
    _unattributable(sources)
    view = _view(sources, "user_dev", userId="u1")
    assert "0 of" not in view["headline"]["text"]
    assert view["headline"]["state"] == "unavailable"


def test_a_zero_duration_is_a_measurement_not_an_absence(sources):
    """Live runs really do complete in under a millisecond. `if avg_ms` turned
    that into "unmeasured", which says we did not look."""
    sources.projects = [{"projectId": "a", "name": "a", "userId": "u1"}]
    sources.runs = [{"versionId": "r", "projectId": "a", "pipeline": "mcp",
                     "status": "success", "durationMs": 0,
                     "startedAt": "2099-01-01T00:00:00+00:00"}]
    view = _view(sources, "project_manager")
    avg = _metrics(view)["Avg time to analyse"]
    assert avg["value"] == "0ms" and avg["state"] == "ok"


# ── A sparkline is a claim about history ─────────────────────────────────────

def test_a_metric_without_history_gets_no_sparkline():
    """Drawing a decorative line on a metric with no series is the same lie as
    rendering an unmeasured value as zero."""
    assert "spark" not in rm.metric("Runs", 4)
    assert "spark" not in rm.metric("Runs", 4, spark=[])


def test_a_series_too_short_to_show_a_shape_is_dropped():
    assert "spark" not in rm.metric("Runs", 4, spark=[1, 2])
    assert rm.metric("Runs", 4, spark=[1, 2, 3])["spark"] == [1.0, 2.0, 3.0]


def test_an_all_zero_series_is_dropped_rather_than_drawn_flat():
    """A flat line at zero reads as "steady", which is the opposite of "nothing
    happened". This is why the live Failure-rate metric has no sparkline."""
    assert "spark" not in rm.metric("Failures", 0, spark=[0, 0, 0, 0])


def test_an_absent_metric_never_carries_a_sparkline(sources):
    """The UI hides the line for an absent state, but it must not be on the
    wire either — a series implies we measured something."""
    view = _view(sources, "user_qa")
    for block in view["blocks"]:
        for item in block.get("items", []):
            if item["state"] in ("unmeasured", "unavailable"):
                assert "spark" not in item, item["label"]


def test_daily_counts_reports_empty_days_as_zero():
    """A series that skipped empty days would draw steady activity across a gap."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    rows = [{"t": (now - timedelta(days=3)).isoformat()},
            {"t": (now - timedelta(days=3)).isoformat()},
            {"t": now.isoformat()}]
    series = rm.daily_counts(rows, "t", days=5)
    assert len(series) == 5
    assert series[-1] == 1.0            # today, oldest-first ordering
    assert series[1] == 2.0             # three days ago
    assert series[2] == 0.0             # a genuine gap


def test_daily_counts_can_weigh_rows_rather_than_count_them():
    """Spend is a sum of costs, not a count of calls."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"t": now, "cost": "0.25"}, {"t": now, "cost": "0.75"}]
    series = rm.daily_counts(rows, "t", days=3, weigh=rm._row_cost)
    assert series[-1] == 1.0


def test_the_gauge_only_ever_repeats_the_number_already_stated(sources):
    """The arc is redundant on purpose. It must never be a target or a second
    metric smuggled in beside the first."""
    sources.test_runs = [{"testRunId": "r1", "projectId": "p1", "type": "qatest",
                          "status": "passed", "totalCases": 10, "totalPassed": 8,
                          "createdAt": "2099-01-01T00:00:00+00:00"}]
    sources.report = {"coverage": {"nodePct": 62, "nodeCovered": 18, "nodeTotal": 29}}
    view = _view(sources, "user_qa")
    assert view["headline"]["gauge"] == 62
    assert "62%" in view["headline"]["text"]


# ── The hero trend ───────────────────────────────────────────────────────────
#
# A total cannot answer the question that immediately follows it — "is that
# normal?" — so the hero carries its own history. Same honesty rule as a
# sparkline: it is a claim about the past, and a fabricated one is worse than
# no chart at all.

def test_a_hero_trend_needs_a_real_series():
    assert rm.trend("Daily spend", []) is None
    assert rm.trend("Daily spend", [1, 2]) is None
    assert rm.trend("Daily spend", [0, 0, 0, 0]) is None
    assert rm.trend("Daily spend", [1, 0, 3])["series"] == [1.0, 0.0, 3.0]


def test_the_admin_hero_charts_the_spend_it_states(sources):
    """The headline says a 30-day total; the chart under it must be that total's
    own daily history, not some other series that happens to be available."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    sources.token_usage = [
        {"userId": "u1", "timestamp": (now - timedelta(days=d)).isoformat(),
         "cost": "0.50"} for d in (0, 1, 5)
    ]
    view = _view(sources, "admin")
    trend = view["headline"]["trend"]
    assert trend["money"] is True
    assert len(trend["series"]) == 30
    assert sum(trend["series"]) == 1.5          # matches the stated total


def test_the_ops_hero_carries_throughput_under_every_headline(sources):
    """Ops has four possible headlines — critical, some-attention, nominal and
    unavailable. Throughput is the context for all of them, so it must not be
    attached to only the happy one."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    # Runs must name a known project, or the attribution warning fires and this
    # stops being the nominal case — which is itself the correct behaviour. The
    # stamps must also be genuinely recent: a future date buckets to nothing and
    # the trend is correctly dropped, which is not what this test is about.
    sources.projects = [{"projectId": "a", "name": "a", "userId": "u1"}]
    sources.runs = [{"versionId": f"r{i}", "projectId": "a", "pipeline": "mcp",
                     "status": "success",
                     "startedAt": (now - timedelta(days=i)).isoformat()}
                    for i in range(4)]
    nominal = _view(sources, "user_ops")
    assert nominal["headline"]["state"] == "ok"
    assert nominal["headline"]["trend"]["label"] == "Pipeline runs per day"

    sources.outbox = {"memgraph": 500}
    critical = _view(sources, "user_ops")
    assert critical["headline"]["state"] == "critical"
    assert critical["headline"]["trend"] is not None


def test_the_product_owner_hero_has_no_trend(sources):
    """Deliberate: nothing snapshots finding counts, and a chart under a risk
    headline would imply a risk trend we cannot draw."""
    sources.projects = [{"projectId": "a", "name": "a", "userId": "u1"}]
    sources.runs = [{"versionId": "r", "projectId": "a", "pipeline": "mcp",
                     "status": "success", "startedAt": "2099-01-01T00:00:00+00:00"}]
    view = _view(sources, "product_owner")
    assert view["headline"].get("trend") is None

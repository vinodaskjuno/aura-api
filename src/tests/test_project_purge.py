"""Deleting one project's graph data — and nothing else's.

The guards outnumber the behaviour tests on purpose. This is a hard delete in a layer
whose contract is "never delete", and the realistic accidents are not "it failed" but
"it quietly took something it should not have": a node shared with another project, a
node belonging to a different project entirely, or a project id spliced into Cypher.
"""
from __future__ import annotations

import pytest

from src.graph import project_purge as pp


# ── Fakes, mirroring test_graph_wipe.py ──────────────────────────────────────

class FakeSession:
    def __init__(self, backend):
        self.b = backend

    def run(self, q, params=None, **kw):
        self.b.statements.append((q, params or {}))
        if self.b.fail_on and self.b.fail_on in q:
            raise RuntimeError("engine refused")
        if "count(n) AS c" in q:
            n = self.b.totals.pop(0) if self.b.totals else 0
            return type("R", (), {"single": staticmethod(lambda: {"c": n})})()
        if "count(DISTINCT r)" in q:
            return type("R", (), {"single": staticmethod(lambda: {"c": self.b.crossing})})()
        if "RETURN labels(n)[0]" in q:
            rows = self.b.excluded if "OR any(" in q else self.b.by_label
            return [{"label": k, "c": v} for k, v in rows.items()]
        if "DETACH DELETE" in q:
            n = self.b.deletes.pop(0) if self.b.deletes else 0
            return type("R", (), {"single": staticmethod(lambda: {"deleted": n})})()
        return type("R", (), {"consume": staticmethod(lambda: None)})()

    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeBackend:
    def __init__(self, name, totals=None, deletes=None, fail_on=""):
        from src.graph.dialects import DIALECTS
        self.name, self.dialect = name, DIALECTS["neo4j"]
        self.totals = list(totals if totals is not None else [10, 0])
        self.deletes = list(deletes if deletes is not None else [10, 0])
        self.by_label = {"API": 6, "Service": 3, "Repository": 1}
        self.excluded = {"DataFlow": 2}
        self.crossing = 4
        self.fail_on, self.statements = fail_on, []
        self.available = True

    def session(self): return FakeSession(self)
    def is_available(self): return self.available


@pytest.fixture
def engines(monkeypatch):
    neo, mem = FakeBackend("neo4j"), FakeBackend("memgraph")
    from src.graph import backends, graph_config, outbox
    monkeypatch.setattr(backends, "get_backend",
                        lambda name=None: {"neo4j": neo, "memgraph": mem}.get(name or "neo4j"))
    monkeypatch.setattr(backends, "configured_names", lambda: ["neo4j", "memgraph"])
    monkeypatch.setattr(graph_config, "get_config", lambda refresh=False:
                        graph_config.GraphConfig("neo4j", ("neo4j", "memgraph")))
    monkeypatch.setattr(outbox, "depth", lambda backend=None: {"neo4j": 0, "memgraph": 0})
    return neo, mem


# ── The scope predicate — where a mistake is silent ──────────────────────────

def test_the_project_id_travels_as_a_parameter_never_in_the_cypher(engines):
    """`wipe.match_clause` interpolates because every value in it is a constant. Here
    the value comes from a URL path, so interpolating it is an injection."""
    neo, _ = engines
    pp.purge_project("p1' OR 1=1 --")

    for statement, params in neo.statements:
        assert "OR 1=1" not in statement, "the project id was spliced into Cypher"
    assert any(p.get("pid") == "p1' OR 1=1 --" for _s, p in neo.statements)


def test_the_scope_matches_on_the_property_not_an_externalid_prefix():
    """There are two Project conventions — `project:{id}` and bare `{id}` — and
    neither covers Repository, Dependency, API, Service, TestRun or TestCase."""
    assert "n.projectId = $pid" in pp._SCOPE
    assert "project:" not in pp._SCOPE


def test_shared_labels_are_excluded_from_the_scope():
    for label in ("Runbook", "Organization", "Incident", "Alert", "AuditLog"):
        assert label in pp.SHARED_LABELS
    assert "NOT any(l IN labels(n) WHERE l IN $shared)" in pp._SCOPE


def test_the_shared_dataflow_prefix_is_excluded():
    """`dataflow:topic:{slug}` has no project in its id and its projectId is
    last-writer-wins, so another project's Services point at the same node."""
    assert "dataflow:topic:" in pp.SHARED_EID_PREFIXES
    assert "STARTS WITH p" in pp._SCOPE


def test_excluded_nodes_are_reported_not_silently_skipped(engines):
    """"41 nodes will go" is half an answer if two more match and are being kept."""
    report = pp.inventory("p1")
    assert report["engines"]["neo4j"]["excluded"] == {"DataFlow": 2}
    assert "DataFlow" not in report["engines"]["neo4j"]["byLabel"]


def test_crossing_edges_are_counted(engines):
    """They die with the DETACH DELETE while the far node survives — which is the
    number that says shared infrastructure loses its links and nothing more."""
    assert pp.inventory("p1")["engines"]["neo4j"]["crossingEdges"] == 4


# ── Guards ───────────────────────────────────────────────────────────────────

def test_a_blank_project_id_is_refused(engines):
    """A blank id would make the predicate match every node with no projectId."""
    out = pp.purge_project("")
    assert not out["ok"] and "blank" in out["error"]
    assert engines[0].statements == []


def test_an_unreachable_engine_is_refused_before_anything_is_deleted(monkeypatch, engines):
    """Clearing one engine of a dual-write pair leaves the mirror populated, and the
    data reappears the moment somebody switches the read source."""
    neo, mem = engines
    mem.available = False
    problems = pp.preflight()
    assert any("memgraph" in p and "not reachable" in p for p in problems)


def test_a_queued_write_for_THIS_project_blocks_the_delete(fake_dynamo, monkeypatch,
                                                          engines):
    """Replayed afterwards, it puts the project back."""
    from src.graph import backends
    monkeypatch.setattr(backends, "configured_names", lambda: ["memgraph"])
    fake_dynamo.put_item("graph-outbox", {"backend": "memgraph", "outboxId": "1",
                                          "params": {"pid": "p1"}})

    assert any("mention this project" in x for x in pp.preflight("p1"))


def test_another_projects_queued_writes_do_not_block(fake_dynamo, monkeypatch, engines):
    """The check was on TOTAL outbox depth, and nothing drains the outbox
    automatically — so one past outage left every project undeletable for ever, with a
    message telling the user to do something the UI never offered them."""
    from src.graph import backends
    monkeypatch.setattr(backends, "configured_names", lambda: ["memgraph"])
    for i in range(500):
        fake_dynamo.put_item("graph-outbox", {"backend": "memgraph",
                                              "outboxId": str(i),
                                              "params": {"pid": "someone-else"}})

    assert pp.preflight("p1") == [], "an unrelated backlog blocked the delete"


def test_the_blocker_says_how_to_clear_it(fake_dynamo, monkeypatch, engines):
    """"drain the outbox first" named no place to do it."""
    from src.graph import backends
    monkeypatch.setattr(backends, "configured_names", lambda: ["memgraph"])
    fake_dynamo.put_item("graph-outbox", {"backend": "memgraph", "outboxId": "1",
                                          "params": {"pid": "p1"}})

    assert any("Settings" in x for x in pp.preflight("p1"))


def test_a_huge_project_is_refused_rather_than_run_in_a_request(engines):
    """There is no index on projectId, so the predicate is a full scan."""
    neo, _ = engines
    neo.totals = [pp.MAX_NODES + 1]
    out = pp.purge_backend(neo, "p1")
    assert not out["ok"] and "above the" in out["error"]
    assert out["deleted"] == 0


# ── Execution across engines ─────────────────────────────────────────────────

def test_every_write_target_is_purged_not_just_the_read_source(engines):
    neo, mem = engines
    out = pp.purge_project("p1")
    assert {r["backend"] for r in out["results"]} == {"neo4j", "memgraph"}
    assert all(r["ok"] for r in out["results"])
    assert mem.statements, "the mirror was never touched"


def test_the_batch_loop_terminates_and_reverifies(engines):
    neo, _ = engines
    neo.deletes = [2000, 2000, 500, 0]
    neo.totals = [4500, 0]
    out = pp.purge_backend(neo, "p1")
    assert out["deleted"] == 4500 and out["ok"] is True


def test_leftovers_are_reported_not_claimed_clean(engines):
    """A recount that still matches means the delete did not finish. Saying "ok" then
    is how a half-deleted project looks fine until somebody notices it in the graph."""
    neo, _ = engines
    neo.totals = [10, 3]
    neo.deletes = [7, 0]
    out = pp.purge_backend(neo, "p1")
    assert not out["ok"] and "still match" in out["error"]


def test_one_engine_failing_still_reports_the_other(engines):
    neo, mem = engines
    mem.fail_on = "DETACH DELETE"
    out = pp.purge_project("p1")
    by_name = {r["backend"]: r for r in out["results"]}
    assert by_name["neo4j"]["ok"] is True
    assert by_name["memgraph"]["ok"] is False
    assert out["ok"] is False          # the whole purge is not ok


def test_the_delete_is_batched_with_a_limit(engines):
    """`CALL … IN TRANSACTIONS` exists only in the Neo4j dialect and is not one of
    Memgraph's rewrites, so the portable form is LIMIT plus a loop."""
    neo, _ = engines
    pp.purge_project("p1")
    deletes = [s for s, _p in neo.statements if "DETACH DELETE" in s]
    assert deletes and all("LIMIT $batch" in s for s in deletes)
    assert not any("IN TRANSACTIONS" in s for s in deletes)


# ── The outbox sweep ─────────────────────────────────────────────────────────

def test_queued_writes_mentioning_the_project_are_dropped(fake_dynamo, monkeypatch):
    """Belt and braces for anything enqueued WHILE the delete ran."""
    from src.graph import backends
    monkeypatch.setattr(backends, "configured_names", lambda: ["memgraph"])
    fake_dynamo.put_item("graph-outbox", {"backend": "memgraph", "outboxId": "1",
                                          "params": {"pid": "p1", "eid": "api:p1:x"}})
    fake_dynamo.put_item("graph-outbox", {"backend": "memgraph", "outboxId": "2",
                                          "params": {"pid": "other", "eid": "api:other:y"}})

    assert pp.drain_outbox_for("p1") == 1
    left = [r["outboxId"] for r in fake_dynamo.tables["graph-outbox"]]
    assert left == ["2"], "another project's queued write was dropped"

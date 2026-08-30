"""Graph wipe — the most destructive operation in the product.

The guard tests come first and outnumber the behaviour tests, deliberately. The
backend cannot tell dev from prod (APP_ENV is hardcoded to "prod" in every
environment), so the only thing standing between a demo convenience and an
irreversible production accident is an opt-in flag that defaults to off.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.graph import wipe
from src.main import app
from src.routers.auth import get_current_user
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)
ADMIN = {"userId": "u1", "username": "admin", "role": "admin",
         "permissions": ROLE_PERMISSIONS["admin"]}
DEV = {"userId": "u2", "username": "dev", "role": "user_dev",
       "permissions": ROLE_PERMISSIONS["user_dev"]}


def _as(user):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _auth():
    previous = app.dependency_overrides.get(get_current_user)
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


@pytest.fixture
def armed(monkeypatch):
    """Server explicitly opted in, as only the dev task definition does."""
    monkeypatch.setattr("src.routers.graph_config._wipe_allowed", lambda: True)


# ── Guards ───────────────────────────────────────────────────────────────────

def test_denied_when_the_server_has_not_opted_in(monkeypatch):
    """The gate that keeps a future production deployment safe: a new environment
    is protected by OMISSION, not by remembering to add a block."""
    monkeypatch.setattr("src.routers.graph_config._wipe_allowed", lambda: False)
    _as(ADMIN)
    r = client.post("/api/graph-config/wipe", json={"scope": "demo"})
    assert r.status_code == 403
    assert "not enabled on this server" in r.json()["detail"]


def test_a_super_admin_still_cannot_wipe_an_unarmed_server(monkeypatch):
    monkeypatch.setattr("src.routers.graph_config._wipe_allowed", lambda: False)
    _as({**ADMIN, "role": "super_admin",
         "permissions": ROLE_PERMISSIONS["super_admin"]})
    assert client.post("/api/graph-config/wipe", json={"scope": "all",
                                                       "confirm": "DELETE"}).status_code == 403


def test_a_non_admin_cannot_wipe_even_when_armed(armed):
    _as(DEV)
    assert client.post("/api/graph-config/wipe", json={"scope": "demo"}).status_code == 403


def test_full_wipe_requires_the_typed_word(armed, monkeypatch):
    monkeypatch.setattr(wipe, "wipe_graph", lambda scope, actor: {"results": [{"ok": True}]})
    _as(ADMIN)
    r = client.post("/api/graph-config/wipe", json={"scope": "all"})
    assert r.status_code == 400
    assert "Type DELETE" in r.json()["detail"]
    assert "cannot be undone" in r.json()["detail"]


def test_the_wrong_word_is_still_refused(armed):
    _as(ADMIN)
    assert client.post("/api/graph-config/wipe",
                       json={"scope": "all", "confirm": "delete"}).status_code == 400


def test_the_recoverable_scope_does_not_demand_typing(armed, monkeypatch):
    """Making the safe action equally tedious trains people to type without reading."""
    monkeypatch.setattr(wipe, "wipe_graph",
                        lambda scope, actor: {"results": [{"ok": True}], "scope": scope})
    _as(ADMIN)
    assert client.post("/api/graph-config/wipe", json={"scope": "demo"}).status_code == 200


def test_an_unknown_scope_is_refused(armed):
    _as(ADMIN)
    assert client.post("/api/graph-config/wipe",
                       json={"scope": "everything"}).status_code == 400


def test_status_explains_why_it_is_disabled(monkeypatch):
    monkeypatch.setattr("src.routers.graph_config._wipe_allowed", lambda: False)
    _as(ADMIN)
    body = client.get("/api/graph-config/wipe-status").json()
    assert body["enabled"] is False
    assert "ALLOW_GRAPH_WIPE" in body["reason"]
    assert body["confirmWord"] == "DELETE"


def test_no_reachable_engine_is_reported_rather_than_faked(armed, monkeypatch):
    monkeypatch.setattr(wipe, "wipe_graph", lambda scope, actor: {"results": []})
    _as(ADMIN)
    r = client.post("/api/graph-config/wipe", json={"scope": "demo"})
    assert r.status_code == 503
    assert "nothing was wiped" in r.json()["detail"]


# ── Scope predicate ──────────────────────────────────────────────────────────

def test_demo_scope_targets_only_seeded_sources():
    clause = wipe.match_clause(wipe.SCOPE_DEMO)
    assert clause.startswith("MATCH (n) WHERE n.source IN [")
    for src in ("seed", "servicenow_cmdb", "wiz"):
        assert f"'{src}'" in clause


def test_servicenow_change_is_included():
    """It was missing from reset-dev.sh's list, so a 'synthetic' reset left those
    nodes behind. This is now the single definition."""
    assert "servicenow_change" in wipe.DEMO_SOURCES
    assert "'servicenow_change'" in wipe.match_clause(wipe.SCOPE_DEMO)


def test_analysed_projects_survive_a_demo_wipe():
    """code_graph writes source="code-analysis"; a demo reset must not destroy the
    project a presenter just analysed."""
    assert "code-analysis" not in wipe.DEMO_SOURCES
    assert "code-analysis" not in wipe.match_clause(wipe.SCOPE_DEMO)


def test_full_scope_matches_everything():
    assert wipe.match_clause(wipe.SCOPE_ALL) == "MATCH (n)"


# ── Dialect ──────────────────────────────────────────────────────────────────

def test_neo4j_batches_the_delete():
    """A single-transaction DETACH DELETE over a few thousand connected nodes OOMs
    the 1 GB JVM heap and takes the container down."""
    from src.graph.dialects import NEO4J
    stmt = NEO4J.bulk_delete_statement("MATCH (n)")
    assert "IN TRANSACTIONS OF 5000 ROWS" in stmt
    # The scoped form; `CALL { WITH n … }` is deprecated and warns on every call.
    assert "CALL (n) {" in stmt


def test_memgraph_does_not_batch():
    """Memgraph has no CALL-in-transactions, and being in-memory has no heap to blow."""
    from src.graph.dialects import MEMGRAPH
    stmt = MEMGRAPH.bulk_delete_statement("MATCH (n)")
    assert "IN TRANSACTIONS" not in stmt
    assert stmt.endswith("DETACH DELETE n")


# ── Execution across engines ─────────────────────────────────────────────────

class FakeSession:
    def __init__(self, backend): self.b = backend
    def run(self, q, params=None, **kw):
        self.b.statements.append(q)
        if self.b.fail_on and self.b.fail_on in q:
            raise RuntimeError("engine refused")
        if "count(n)" in q:
            n = self.b.counts.pop(0) if self.b.counts else 0
            return type("R", (), {"single": staticmethod(lambda: {"c": n})})()
        return type("R", (), {"consume": staticmethod(lambda: None)})()
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeBackend:
    def __init__(self, name, counts=None, fail_on=""):
        from src.graph.dialects import DIALECTS
        self.name, self.dialect = name, DIALECTS["neo4j"]
        self.counts = list(counts if counts is not None else [10, 0])
        self.fail_on, self.statements = fail_on, []
    def session(self): return FakeSession(self)
    def is_available(self): return True


@pytest.fixture
def engines(monkeypatch):
    neo, mem = FakeBackend("neo4j"), FakeBackend("memgraph")
    from src.graph import backends, graph_config
    monkeypatch.setattr(backends, "get_backend",
                        lambda name=None: {"neo4j": neo, "memgraph": mem}.get(name or "neo4j"))
    monkeypatch.setattr(backends, "configured_names", lambda: ["neo4j", "memgraph"])
    monkeypatch.setattr(graph_config, "get_config", lambda refresh=False:
                        graph_config.GraphConfig("neo4j", ("neo4j", "memgraph")))
    monkeypatch.setattr(wipe, "_record", lambda report: None)
    return neo, mem


def test_every_write_target_is_cleared_not_just_the_read_source(engines):
    """Clearing one engine of a dual-write pair leaves the mirror populated, and the
    'deleted' data reappears when someone switches the read source."""
    neo, mem = engines
    report = wipe.wipe_graph(wipe.SCOPE_DEMO, "alice")
    assert [r["backend"] for r in report["results"]] == ["neo4j", "memgraph"]
    assert report["ok"] is True
    assert report["totalDeleted"] == 20        # 10 from each
    assert any("DETACH DELETE" in s for s in neo.statements)
    assert any("DETACH DELETE" in s for s in mem.statements)


def test_one_failing_engine_still_reports_the_other(engines, monkeypatch):
    neo, mem = engines
    mem.fail_on = "DETACH DELETE"
    report = wipe.wipe_graph(wipe.SCOPE_DEMO, "alice")
    assert report["ok"] is False
    by_name = {r["backend"]: r for r in report["results"]}
    assert by_name["neo4j"]["ok"] is True
    assert "engine refused" in by_name["memgraph"]["error"]


def test_leftover_nodes_are_reported_rather_than_claimed_clean(engines):
    neo, _ = engines
    neo.counts = [10, 3]           # 3 still match afterwards
    report = wipe.wipe_graph(wipe.SCOPE_DEMO, "alice")
    neo_result = report["results"][0]
    assert neo_result["ok"] is False
    assert "still match" in neo_result["error"]


def test_an_unreachable_engine_is_named(engines, monkeypatch):
    from src.graph import backends
    neo, mem = engines
    monkeypatch.setattr(mem, "is_available", lambda: False)
    report = wipe.wipe_graph(wipe.SCOPE_DEMO, "alice")
    assert {r["backend"]: r.get("error") for r in report["results"]}["memgraph"] == "not reachable"


def test_an_unknown_scope_deletes_nothing(engines):
    neo, mem = engines
    report = wipe.wipe_graph("everything", "alice")
    assert "error" in report
    assert neo.statements == [] and mem.statements == []


def test_the_wipe_is_recorded_where_it_will_survive(engines, monkeypatch):
    """A full wipe deletes the in-graph :AuditLog, so the record goes to DynamoDB —
    the account of a deletion has to outlive what it deleted."""
    written = {}
    monkeypatch.setattr("src.database.dynamo_client.write_changelog",
                        lambda entry: written.update(entry))
    monkeypatch.undo()  # restore _record patched out by the fixture
    monkeypatch.setattr("src.database.dynamo_client.write_changelog",
                        lambda entry: written.update(entry))
    wipe._record({"scope": "all", "actor": "alice", "at": "2026-01-01T00:00:00Z",
                  "results": [{"backend": "neo4j", "before": 10}], "totalDeleted": 10,
                  "ok": True})
    assert written["changeType"] == "WIPE_GRAPH"
    assert written["actor"] == "alice"
    assert written["source"] == "ui"

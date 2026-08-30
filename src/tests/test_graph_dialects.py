"""Dialect translation and backend registry.

The product is deployed per client against whichever engine that client permits,
so the translation below is what makes a Memgraph deployment a first-class target
rather than a degraded Neo4j. It is pure string work, so it is testable without
either engine running.
"""
from __future__ import annotations

import pytest

from src.graph import backends
from src.graph.dialects import DIALECTS, MEMGRAPH, NEO4J


# ── Neo4j is the reference dialect ───────────────────────────────────────────

@pytest.mark.parametrize("cypher", [
    "RETURN elementId(n) AS id",
    "MATCH (n) WHERE elementId(n) = $id RETURN n",
    "id: elementId(r), source: elementId(startNode(r)), target: elementId(endNode(r))",
    "CALL db.index.fulltext.queryNodes('runbook_fts', $terms) YIELD node, score",
])
def test_neo4j_never_rewrites_anything(cypher):
    """Every query in the codebase is written for Neo4j 5, so translating for it
    must be the identity — otherwise the refactor changed live behaviour."""
    assert NEO4J.adapt(cypher) == cypher


# ── Memgraph translation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("source,expected", [
    ("RETURN elementId(n) AS id", "RETURN toString(id(n)) AS id"),
    ("WHERE elementId(b) IN $ids", "WHERE toString(id(b)) IN $ids"),
    ("elementId(startNode(r))", "toString(id(startNode(r)))"),
    ("elementId(endNode(r))", "toString(id(endNode(r)))"),
    ("elementId( n )", "toString(id(n))"),
])
def test_memgraph_rewrites_element_id(source, expected):
    assert MEMGRAPH.adapt(source) == expected


def test_memgraph_rewrites_every_occurrence_in_one_statement():
    src = ("RETURN DISTINCT elementId(r) AS id, elementId(a) AS source, "
           "elementId(b) AS target")
    out = MEMGRAPH.adapt(src)
    assert "elementId" not in out
    assert out.count("toString(id(") == 3


def test_ids_stay_strings_on_both_engines():
    """Memgraph's id() returns an integer. Without toString every caller comparing
    an id against a string would silently stop matching."""
    assert MEMGRAPH.node_id_expr("n").startswith("toString(")
    assert NEO4J.node_id_expr("n") == "elementId(n)"


def test_constraint_ddl_differs_per_engine():
    neo = NEO4J.constraint_ddl("Service")
    mem = MEMGRAPH.constraint_ddl("Service")
    assert "REQUIRE" in neo and "IF NOT EXISTS" in neo
    assert "ASSERT" in mem and "REQUIRE" not in mem


def test_multi_database_kwarg_is_omitted_where_unsupported():
    """Memgraph Community rejects database= rather than ignoring it."""
    assert NEO4J.session_kwargs("neo4j") == {"database": "neo4j"}
    assert MEMGRAPH.session_kwargs("neo4j") == {}


def test_capabilities_are_declared_not_inferred_from_name():
    assert NEO4J.supports_fulltext is True
    assert MEMGRAPH.supports_fulltext is False
    assert MEMGRAPH.supports_apoc is False


def test_registry_exposes_both_dialects():
    assert set(DIALECTS) == {"neo4j", "memgraph"}


# ── Registry behaviour ───────────────────────────────────────────────────────

def test_unconfigured_backend_is_absent_not_broken(monkeypatch):
    """A client install runs one engine; the others must simply not exist."""
    backends.reset()
    monkeypatch.setattr(backends, "_configs_from_settings", lambda: {})
    assert backends.get_backend() is None
    assert backends.configured_names() == []
    backends.reset()


def test_backend_is_selectable_by_name(monkeypatch):
    backends.reset()
    monkeypatch.setattr(backends, "_configs_from_settings", lambda: {
        "neo4j": backends.BackendConfig("neo4j", "bolt://x", "u", "p",
                                        dialect_name="neo4j"),
        "memgraph": backends.BackendConfig("memgraph", "bolt://y", "u", "p",
                                           dialect_name="memgraph"),
    })
    assert backends.configured_names() == ["memgraph", "neo4j"]
    assert backends.get_backend("memgraph").dialect.name == "memgraph"
    assert backends.get_backend("neo4j").dialect.name == "neo4j"
    # No name means the default, so existing callers keep their behaviour.
    assert backends.get_backend().name == "neo4j"
    backends.reset()


def test_a_dead_backend_is_not_retried_on_every_call(monkeypatch):
    """Retrying a downed engine per call would add a connect timeout to every
    request, so failures are held for a cooldown."""
    backends.reset()
    monkeypatch.setattr(backends, "_configs_from_settings", lambda: {
        "neo4j": backends.BackendConfig("neo4j", "bolt://nope", "u", "p")})
    backend = backends.get_backend("neo4j")

    attempts = {"n": 0}

    class Boom:
        @staticmethod
        def driver(*a, **k):
            attempts["n"] += 1
            raise RuntimeError("refused")

    monkeypatch.setitem(__import__("sys").modules, "neo4j",
                        type("M", (), {"GraphDatabase": Boom}))
    assert backend.is_available() is False
    assert backend.is_available() is False
    assert attempts["n"] == 1

    backend.close()
    assert backend.is_available() is False
    assert attempts["n"] == 2      # close() clears the cooldown, so it retried
    backends.reset()


def test_a_backend_recovers_once_the_cooldown_expires(monkeypatch):
    """A permanent latch meant an API that started seconds before its graph
    reported the engine down until someone restarted it by hand — observed in dev,
    where the API came up at 07:13:30 and neo4j at 07:13:33."""
    backends.reset()
    monkeypatch.setattr(backends, "_configs_from_settings", lambda: {
        "neo4j": backends.BackendConfig("neo4j", "bolt://nope", "u", "p")})
    backend = backends.get_backend("neo4j")

    calls = {"n": 0}
    state = {"up": False}

    class Flaky:
        @staticmethod
        def driver(*a, **k):
            calls["n"] += 1
            if not state["up"]:
                raise RuntimeError("refused")

            class D:
                @staticmethod
                def verify_connectivity(): return None
            return D()

    monkeypatch.setitem(__import__("sys").modules, "neo4j",
                        type("M", (), {"GraphDatabase": Flaky}))

    assert backend.is_available() is False
    assert backend.is_available() is False
    assert calls["n"] == 1                     # held during the cooldown

    # The engine comes up, and the cooldown lapses.
    state["up"] = True
    clock = {"t": backends.time.monotonic() + backends.RETRY_COOLDOWN_SECONDS + 1}
    monkeypatch.setattr(backends.time, "monotonic", lambda: clock["t"])

    assert backend.is_available() is True      # recovered with no restart
    assert calls["n"] == 2
    backends.reset()


def test_adapting_session_translates_before_running():
    """The rewrite happens at the session boundary, which is what covers the 23
    raw `with session()` blocks outside this package."""
    ran: list[str] = []

    class RawSession:
        def run(self, query, parameters=None, **kw):
            ran.append(query)
            return []

        def close(self):
            ran.append("closed")

    adapting = backends._AdaptingSession(RawSession(), MEMGRAPH)
    adapting.run("RETURN elementId(n)")
    assert ran == ["RETURN toString(id(n))"]

    # Anything that is not `run` passes straight through.
    adapting.close()
    assert ran[-1] == "closed"


def test_graph_client_alias_exposes_the_same_api():
    from src.graph import graph_client, neo4j_client
    for name in ("upsert_node", "run_query", "is_available", "session", "ensure_schema"):
        assert getattr(graph_client, name) is getattr(neo4j_client, name)

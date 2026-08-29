"""Same operations, both engines, identical observable results.

Portability is a claim until something checks it. These tests run one sequence of
writes and reads against every configured engine and compare the outcomes, so a
Cypher change that silently works on only one engine fails here rather than in a
client's deployment.

Skips cleanly when fewer than two engines are reachable, so CI stays green without
Memgraph. Point it at a local pair with:

    podman run -d -p 7689:7687 memgraph/memgraph:2.18.1
    AURA_PARITY_MEMGRAPH_URI=bolt://127.0.0.1:7689 pytest src/tests/test_graph_parity.py

Memgraph 3.x is deliberately not the default: its Bolt handshake rejects the
neo4j driver's auth token ("scheme 'basic' is not supported"), verified against
3.12.0. 2.18.1 connects with the same driver unchanged.
"""
from __future__ import annotations

import os
import uuid

import pytest

from src.graph import backends

PREFIX = f"parity:{uuid.uuid4().hex[:8]}"


def _candidate_configs() -> dict[str, backends.BackendConfig]:
    """Engines to compare, from env so CI can run with none, one, or both."""
    from src.config_settings import get_settings
    s = get_settings()
    found: dict[str, backends.BackendConfig] = {}
    # Neo4j credentials come from the app's own settings rather than being
    # duplicated here — a second copy would drift and this would skip silently.
    found["neo4j"] = backends.BackendConfig(
        "neo4j", s.neo4j_uri, s.neo4j_user, s.neo4j_password,
        database=s.neo4j_database, dialect_name="neo4j")
    found["memgraph"] = backends.BackendConfig(
        "memgraph",
        os.environ.get("AURA_PARITY_MEMGRAPH_URI",
                       getattr(s, "memgraph_uri", "bolt://127.0.0.1:7689")),
        getattr(s, "memgraph_user", ""), getattr(s, "memgraph_password", ""),
        database="memgraph", dialect_name="memgraph")
    return found


@pytest.fixture(scope="module")
def engines():
    live: dict[str, backends.Backend] = {}
    for name, cfg in _candidate_configs().items():
        backend = backends.Backend(cfg)
        if backend.is_available():
            live[name] = backend
    if len(live) < 2:
        pytest.skip(f"parity needs two reachable engines, found {sorted(live)}")
    yield live
    for backend in live.values():
        try:
            with backend.session() as s:
                s.run("MATCH (n) WHERE n.externalId STARTS WITH $p DETACH DELETE n",
                      {"p": PREFIX})
        except Exception:      # noqa: BLE001 — cleanup must not mask a real failure
            pass
        backend.close()


def run(backend, cypher, params=None):
    with backend.session() as s:
        return [r.data() for r in s.run(cypher, params or {})]


def seed(backend):
    """Identical writes on every engine, expressed in the reference dialect."""
    with backend.session() as s:
        for i, (title, tags, svc) in enumerate([
            ("Checkout OOM recovery", "oom memory", "checkout-service"),
            ("Database failover", "database replica", "payments-service"),
            ("Cache eviction storm", "cache memory", "checkout-service"),
        ]):
            s.run("""
                MERGE (n:Runbook {externalId: $eid})
                SET n += {title: $title, tags: $tags, services: $svc,
                          bodySnippet: $body, status: 'active'}
            """, {"eid": f"{PREFIX}:rb:{i}", "title": title, "tags": tags,
                  "svc": svc, "body": f"steps for {title.lower()}"})
        s.run("MERGE (s:Service {externalId: $eid}) SET s.name = $n",
              {"eid": f"{PREFIX}:svc", "n": "checkout-service"})
        s.run("""
            MATCH (r:Runbook {externalId: $rb}), (s:Service {externalId: $svc})
            MERGE (r)-[:DOCUMENTS]->(s)
        """, {"rb": f"{PREFIX}:rb:0", "svc": f"{PREFIX}:svc"})


@pytest.fixture(scope="module")
def seeded(engines):
    for backend in engines.values():
        seed(backend)
    return engines


def compare(seeded, fn):
    """Run `fn` on every engine and assert all results are equal."""
    results = {name: fn(backend) for name, backend in seeded.items()}
    reference_name, reference = next(iter(results.items()))
    for name, value in results.items():
        assert value == reference, (
            f"{name} disagreed with {reference_name}:\n"
            f"  {reference_name}: {reference}\n  {name}: {value}")
    return reference


# ── Parity ───────────────────────────────────────────────────────────────────

def test_written_nodes_read_back_identically(seeded):
    compare(seeded, lambda b: run(b, """
        MATCH (n:Runbook) WHERE n.externalId STARTS WITH $p
        RETURN n.externalId AS eid, n.title AS title, n.tags AS tags
        ORDER BY eid
    """, {"p": PREFIX}))


def test_node_counts_agree(seeded):
    compare(seeded, lambda b: run(b, """
        MATCH (n) WHERE n.externalId STARTS WITH $p
        RETURN labels(n)[0] AS label, count(*) AS c ORDER BY label
    """, {"p": PREFIX}))


def test_relationship_traversal_agrees(seeded):
    compare(seeded, lambda b: run(b, """
        MATCH (r:Runbook)-[:DOCUMENTS]->(s:Service)
        WHERE r.externalId STARTS WITH $p
        RETURN r.externalId AS rb, s.name AS svc ORDER BY rb, svc
    """, {"p": PREFIX}))


def test_element_id_is_a_string_on_both_engines(seeded):
    """Ids differ between engines by design, so the parity assertion is on their
    TYPE and uniqueness — a Memgraph integer id would break every caller that
    compares an id to a string."""
    for name, backend in seeded.items():
        rows = run(backend, """
            MATCH (n:Runbook) WHERE n.externalId STARTS WITH $p
            RETURN elementId(n) AS id ORDER BY n.externalId
        """, {"p": PREFIX})
        ids = [r["id"] for r in rows]
        assert len(ids) == 3, name
        assert all(isinstance(i, str) for i in ids), f"{name} returned non-string ids: {ids}"
        assert len(set(ids)) == 3, f"{name} returned duplicate ids: {ids}"


def test_upsert_converges_rather_than_duplicating(seeded):
    """Re-running the same MERGE must not grow the graph on either engine."""
    before = compare(seeded, lambda b: run(b,
        "MATCH (n:Runbook) WHERE n.externalId STARTS WITH $p RETURN count(*) AS c",
        {"p": PREFIX}))
    for backend in seeded.values():
        seed(backend)
    after = compare(seeded, lambda b: run(b,
        "MATCH (n:Runbook) WHERE n.externalId STARTS WITH $p RETURN count(*) AS c",
        {"p": PREFIX}))
    assert before == after


def test_property_update_is_visible_identically(seeded):
    for backend in seeded.values():
        with backend.session() as s:
            s.run("MATCH (n:Runbook {externalId: $e}) SET n.status = 'retired'",
                  {"e": f"{PREFIX}:rb:1"})
    compare(seeded, lambda b: run(b, """
        MATCH (n:Runbook) WHERE n.externalId STARTS WITH $p
        RETURN n.externalId AS eid, n.status AS status ORDER BY eid
    """, {"p": PREFIX}))


def test_runbook_search_finds_the_same_runbooks(seeded, monkeypatch):
    """The engines take different retrieval paths — Neo4j's full-text index versus
    the portable CONTAINS scan — so this asserts the SET of matches agrees, not the
    scores, which are on different scales by construction."""
    from src.graph import neo4j_client as g

    found: dict[str, set] = {}
    for name, backend in seeded.items():
        monkeypatch.setattr(g, "active_backend", lambda b=backend: b)
        hits = g.search_runbooks(service="checkout-service",
                                 alert_signature="memory", limit=10)
        found[name] = {h["externalId"] for h in hits
                       if str(h.get("externalId", "")).startswith(PREFIX)}

    reference_name, reference = next(iter(found.items()))
    for name, value in found.items():
        assert value == reference, (
            f"{name} found {value}, {reference_name} found {reference}")
    assert reference, "both engines returned nothing — the fixture is not being matched"

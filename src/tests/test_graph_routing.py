"""Dual-write routing, the outbox, and the runtime read-source switch.

A client deployment runs one engine; the demo environment runs both so the source
can be switched live. The behaviours that matter are all failure behaviours: a
shadow engine going down must not take writes offline, and divergence must be
visible rather than silent.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.graph import backends, graph_config, outbox
from src.main import app
from src.routers.auth import get_current_user
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)
ADMIN = {"userId": "u1", "username": "admin", "role": "admin",
         "permissions": ROLE_PERMISSIONS["admin"]}


class FakeSession:
    def __init__(self, store, fail=False):
        self.store, self.fail = store, fail

    def run(self, query, parameters=None, **kwargs):
        if self.fail:
            raise RuntimeError("engine down")
        self.store.append((query, {**(parameters or {}), **kwargs}))
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeBackend:
    def __init__(self, name, fail=False):
        self.name, self.fail = name, fail
        self.statements: list = []
        from src.graph.dialects import DIALECTS
        self.dialect = DIALECTS["neo4j"]
        self.config = backends.BackendConfig(name, f"bolt://{name}:7687", "u", "p")

    def session(self):
        return FakeSession(self.statements, self.fail)

    def is_available(self):
        return not self.fail


@pytest.fixture
def routed(monkeypatch):
    primary, shadow = FakeBackend("neo4j"), FakeBackend("memgraph")
    queued: list[dict] = []

    monkeypatch.setattr(backends, "get_backend",
                        lambda name=None: {"neo4j": primary, "memgraph": shadow}.get(
                            name or "neo4j"))
    monkeypatch.setattr(graph_config, "get_config", lambda refresh=False:
                        graph_config.GraphConfig("neo4j", ("neo4j", "memgraph")))
    monkeypatch.setattr(outbox, "enqueue",
                        lambda b, c, p, e: queued.append(
                            {"backend": b, "cypher": c, "params": p, "error": e}))
    return primary, shadow, queued


# ── Fan-out ──────────────────────────────────────────────────────────────────

def test_writes_reach_every_target(routed):
    primary, shadow, _ = routed
    session = backends._FanOutSession(FakeSession(primary.statements), [shadow],
                                      primary.dialect)
    session.run("MERGE (n:Service {externalId: $e})", {"e": "svc:1"})
    assert len(primary.statements) == 1
    assert len(shadow.statements) == 1


def test_keyword_parameters_are_forwarded(routed):
    """The driver accepts params as a dict OR as kwargs, and this codebase uses
    both. Forwarding only the dict mirrored every write with no parameters, and the
    secondary rejected it with "Parameter $props not provided" — found only by
    running it against a real engine."""
    primary, shadow, _ = routed
    session = backends._FanOutSession(FakeSession(primary.statements), [shadow],
                                      primary.dialect)
    session.run("MERGE (n:S {externalId: $eid}) SET n += $props",
                eid="svc:1", props={"name": "x"})
    _, mirrored = shadow.statements[0]
    assert mirrored == {"eid": "svc:1", "props": {"name": "x"}}


def test_reads_are_not_mirrored(routed):
    """Replaying reads on every secondary would double query load for nothing."""
    primary, shadow, _ = routed
    session = backends._FanOutSession(FakeSession(primary.statements), [shadow],
                                      primary.dialect)
    session.run("MATCH (n:Service) RETURN n")
    assert primary.statements and shadow.statements == []


@pytest.mark.parametrize("cypher,expected", [
    ("MERGE (n:S) RETURN n", True),
    ("MATCH (n) SET n.x = 1", True),
    ("CREATE (n:S)", True),
    ("MATCH (n) DETACH DELETE n", True),
    ("MATCH (n) REMOVE n.x", True),
    ("MATCH (n:S) RETURN n", False),
    ("MATCH (n) WHERE n.x = 1 RETURN count(n)", False),
])
def test_write_detection(cypher, expected):
    assert backends.is_write(cypher) is expected


def test_a_failing_secondary_never_fails_the_caller(routed):
    """A shadow store must not be able to take production writes offline."""
    primary, _, queued = routed
    dead = FakeBackend("memgraph", fail=True)
    session = backends._FanOutSession(FakeSession(primary.statements), [dead],
                                      primary.dialect)
    session.run("MERGE (n:S {externalId: $e})", {"e": "svc:1"})   # must not raise
    assert len(primary.statements) == 1
    assert len(queued) == 1
    assert queued[0]["backend"] == "memgraph"
    assert queued[0]["params"] == {"e": "svc:1"}


# ── Config resolution ────────────────────────────────────────────────────────

def test_a_backend_absent_from_this_deployment_is_ignored(monkeypatch):
    """A client install runs one engine. A stale config row naming another must
    never route traffic at a backend that does not exist here."""
    graph_config.invalidate()
    monkeypatch.setattr(backends, "configured_names", lambda: ["neo4j"])
    monkeypatch.setattr(backends, "DEFAULT_BACKEND", "neo4j")
    monkeypatch.setattr("src.database.dynamo_client.get_item",
                        lambda t, k: {"readSource": "memgraph",
                                      "writeTargets": ["neo4j", "memgraph"]})
    config = graph_config.get_config(refresh=True)
    assert config.read_source == "neo4j"
    assert config.write_targets == ("neo4j",)
    graph_config.invalidate()


def test_defaults_come_from_what_is_configured(monkeypatch):
    graph_config.invalidate()
    monkeypatch.setattr(backends, "configured_names", lambda: ["memgraph"])
    monkeypatch.setattr(backends, "DEFAULT_BACKEND", "neo4j")
    monkeypatch.setattr("src.database.dynamo_client.get_item", lambda t, k: None)
    config = graph_config.get_config(refresh=True)
    assert config.read_source == "memgraph"      # not the absent default
    graph_config.invalidate()


# ── API guards ───────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    monkeypatch.setattr(backends, "configured_names", lambda: ["neo4j", "memgraph"])
    monkeypatch.setattr(backends, "get_backend",
                        lambda name=None: FakeBackend(name or "neo4j"))
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


def test_switching_to_a_lagging_source_is_refused(api, monkeypatch):
    """The reason the outbox reports depth instead of retrying quietly."""
    monkeypatch.setattr(outbox, "depth", lambda backend=None: {"memgraph": 7, "neo4j": 0})
    r = client.put("/api/graph-config", json={"readSource": "memgraph",
                                              "writeTargets": ["neo4j", "memgraph"]})
    assert r.status_code == 409
    assert "7 write(s) pending" in r.json()["detail"]


def test_read_source_must_also_be_written_to(api, monkeypatch):
    monkeypatch.setattr(outbox, "depth", lambda backend=None: {})
    r = client.put("/api/graph-config", json={"readSource": "memgraph",
                                              "writeTargets": ["neo4j"]})
    assert r.status_code == 400
    assert "must also be a write target" in r.json()["detail"]


def test_unknown_backend_is_rejected(api, monkeypatch):
    monkeypatch.setattr(outbox, "depth", lambda backend=None: {})
    r = client.put("/api/graph-config", json={"readSource": "neptune",
                                              "writeTargets": ["neptune"]})
    assert r.status_code == 400
    assert "Unknown backend" in r.json()["detail"]


def test_status_reports_capabilities_and_backlog(api, monkeypatch):
    monkeypatch.setattr(outbox, "depth", lambda backend=None: {"memgraph": 2})
    monkeypatch.setattr(graph_config, "get_config", lambda refresh=False:
                        graph_config.GraphConfig("neo4j", ("neo4j", "memgraph")))
    body = client.get("/api/graph-config").json()
    assert body["readSource"] == "neo4j"
    assert {b["name"] for b in body["backends"]} == {"neo4j", "memgraph"}
    assert body["pending"] == {"memgraph": 2}


def test_connection_credentials_are_not_returned(api, monkeypatch):
    """A Bolt URI can embed userinfo, and this response reaches the browser."""
    from src.routers import graph_config as router_mod
    assert router_mod._safe_uri("bolt://user:secret@host:7687") == "bolt://host:7687"
    assert router_mod._safe_uri("bolt://host:7687") == "bolt://host:7687"


def test_settings_permission_is_required(monkeypatch):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: {
        "userId": "u2", "username": "dev", "role": "user_dev",
        "permissions": ROLE_PERMISSIONS["user_dev"]}
    try:
        assert client.get("/api/graph-config").status_code == 403
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

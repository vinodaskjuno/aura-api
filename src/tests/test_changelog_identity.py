"""Audit history must survive a database rebuild and an engine switch.

History used to be keyed by Neo4j's elementId. That value is regenerated on a
rebuild and differs between engines, so a deployment that switched from Neo4j to
Memgraph would show an empty Changelog panel — exactly when an operator most wants
to compare the two. externalId is deterministic and identical everywhere.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.database import dynamo_client as dynamo
from src.main import app
from src.routers.auth import get_current_user
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)
ADMIN = {"userId": "u1", "username": "admin", "role": "admin",
         "permissions": ROLE_PERMISSIONS["admin"]}


@pytest.fixture(autouse=True)
def _auth():
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


# ── Entry construction ───────────────────────────────────────────────────────

def test_external_id_becomes_the_key():
    entry = dynamo.build_changelog_entry(
        entity_id="4:abc:123", external_id="dep:p1:pypi:fastapi",
        entity_type="Node", entity_label="Dependency", entity_name="fastapi",
        change_type="CREATE", actor="alice", before=None, after={"v": "1"})
    assert entry["entityId"] == "dep:p1:pypi:fastapi"
    # The engine id is kept so a row can still be traced to what it was written against.
    assert entry["elementId"] == "4:abc:123"


def test_engine_id_is_used_only_when_there_is_nothing_better():
    """Relationships and bulk-load events have no externalId."""
    entry = dynamo.build_changelog_entry(
        entity_id="4:abc:123", entity_type="Relationship", entity_label="DEPENDS_ON",
        entity_name="a->b", change_type="ARCHIVE", actor="alice",
        before=None, after=None)
    assert entry["entityId"] == "4:abc:123"


def test_the_same_node_keys_identically_on_both_engines():
    """The whole point: two engines produce different node ids for the same node."""
    on_neo4j = dynamo.build_changelog_entry(
        entity_id="4:e69138cf:16", external_id="service:p1:pricing",
        entity_type="Node", entity_label="Service", entity_name="pricing",
        change_type="CREATE", actor="a", before=None, after={})
    on_memgraph = dynamo.build_changelog_entry(
        entity_id="2", external_id="service:p1:pricing",
        entity_type="Node", entity_label="Service", entity_name="pricing",
        change_type="CREATE", actor="a", before=None, after={})
    assert on_neo4j["entityId"] == on_memgraph["entityId"]
    assert on_neo4j["elementId"] != on_memgraph["elementId"]


# ── Read path ────────────────────────────────────────────────────────────────

def test_node_changelog_resolves_by_external_id(monkeypatch):
    queried: list[str] = []

    monkeypatch.setattr("src.routers.ontology_universe.neo4j.get_node_by_id",
                        lambda node_id: {"externalId": "dep:p1:pypi:fastapi"})

    def fake_changelog(entity_id, limit=20):
        queried.append(entity_id)
        if entity_id == "dep:p1:pypi:fastapi":
            return [{"changeId": "c1", "timestamp": "2026-01-02", "changeType": "UPDATE"}]
        return []

    monkeypatch.setattr(dynamo, "get_entity_changelog", fake_changelog)
    rows = client.get("/api/ontology/nodes/4:abc:123/changelog").json()
    assert [r["changeId"] for r in rows] == ["c1"]
    assert "dep:p1:pypi:fastapi" in queried


def test_legacy_rows_written_before_the_rekey_are_still_found(monkeypatch):
    """The re-key must not appear to erase existing history."""
    monkeypatch.setattr("src.routers.ontology_universe.neo4j.get_node_by_id",
                        lambda node_id: {"externalId": "dep:p1:pypi:fastapi"})

    def fake_changelog(entity_id, limit=20):
        if entity_id == "dep:p1:pypi:fastapi":
            return [{"changeId": "new", "timestamp": "2026-02-01"}]
        if entity_id == "4:abc:123":
            return [{"changeId": "old", "timestamp": "2026-01-01"}]
        return []

    monkeypatch.setattr(dynamo, "get_entity_changelog", fake_changelog)
    rows = client.get("/api/ontology/nodes/4:abc:123/changelog").json()
    # Both keys queried, merged, newest first.
    assert [r["changeId"] for r in rows] == ["new", "old"]


def test_a_row_present_under_both_keys_is_not_duplicated(monkeypatch):
    monkeypatch.setattr("src.routers.ontology_universe.neo4j.get_node_by_id",
                        lambda node_id: {"externalId": "svc:1"})
    monkeypatch.setattr(dynamo, "get_entity_changelog",
                        lambda entity_id, limit=20: [
                            {"changeId": "same", "timestamp": "2026-01-01"}])
    rows = client.get("/api/ontology/nodes/4:abc:123/changelog").json()
    assert len(rows) == 1


def test_history_still_returned_when_the_graph_cannot_resolve_the_id(monkeypatch):
    """A downed graph must not blank the history panel."""
    def boom(node_id):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr("src.routers.ontology_universe.neo4j.get_node_by_id", boom)
    monkeypatch.setattr(dynamo, "get_entity_changelog",
                        lambda entity_id, limit=20: (
                            [{"changeId": "legacy", "timestamp": "2026-01-01"}]
                            if entity_id == "4:abc:123" else []))
    rows = client.get("/api/ontology/nodes/4:abc:123/changelog").json()
    assert [r["changeId"] for r in rows] == ["legacy"]

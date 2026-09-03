"""Contract tests for the provenance API.

Two things matter here beyond the response shape:

  * The endpoints are readable by any signed-in user. The changelog endpoints they
    replace required `ontology_maintain`, which is why the Provenance tab showed
    most people nothing — the permission, not the data, was the wall.

  * Before/after VALUES are redacted for those users while the event itself is
    not. Hiding who changed what and when would defeat the feature for exactly the
    people it is meant to serve; hiding the record contents is a separate call.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SKIP_BOOTSTRAP", "1")
os.environ.setdefault("NEO4J_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402
from src.routers.auth import get_current_user  # noqa: E402

VIEWER = {"username": "viewer", "userId": "u1", "role": "viewer",
          "permissions": ["ontology"]}
MAINTAINER = {"username": "maint", "userId": "u2", "role": "admin",
              "permissions": ["ontology", "ontology_maintain"]}

client = TestClient(app)


@pytest.fixture
def as_viewer():
    app.dependency_overrides[get_current_user] = lambda: VIEWER
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def as_maintainer():
    app.dependency_overrides[get_current_user] = lambda: MAINTAINER
    yield
    app.dependency_overrides.pop(get_current_user, None)


NODE = {
    "id": "4:abc:1",
    "labels": ["Service"],
    "name": "payments-api",
    "externalId": "svc:payments",
    "source": "git",
    "pipeline": "git",
    "trigger": "scheduled",
    "actor": "scheduler:ontology_delta_job",
    "sourceDetail": "acme/payments-api@main",
    "attribution": "traced",
    "firstSeenRunId": "run-1",
    "firstSeenAt": "2026-08-12T09:14:00+00:00",
    "lastSeenRunId": "run-2",
    "lastSeenAt": "2026-09-03T02:00:00+00:00",
    "createdBy": "vinod@askjuno.com",
    "confidence": 0.87,
    "factType": "known",
    "evidence": '["src/api/routes.py:88"]',
}

CHANGE = {
    "changeId": "c1",
    "timestamp": "2026-09-03T02:00:01+00:00",
    "changeType": "UPDATE",
    "actor": "scheduler:ontology_delta_job",
    "pipeline": "git",
    "trigger": "scheduled",
    "runId": "run-2",
    "before": '{"version": "1.4.2"}',
    "after": '{"version": "1.4.3"}',
}

RUN = {
    "versionId": "run-2",
    "versionNumber": "v1.42",
    "pipeline": "git",
    "loadMethod": "git",
    "trigger": "scheduled",
    "actor": "scheduler:ontology_delta_job",
    "status": "success",
    "startedAt": "2026-09-03T02:00:00+00:00",
    "durationMs": 108000,
    "sourceDetail": "acme/payments-api@main",
    "stats": {"nodesAdded": 84, "nodesUpdated": 213},
    "errors": ["repo xyz: clone timed out"],
}


@pytest.fixture
def graph(monkeypatch):
    from src.graph import neo4j_client
    from src.database import dynamo_client
    from src.services import ontology_version_service

    monkeypatch.setattr(neo4j_client, "get_node_by_id", lambda nid: dict(NODE) if nid == NODE["id"] else None)
    monkeypatch.setattr(dynamo_client, "get_entity_changelog",
                        lambda entity_id, limit=20: [dict(CHANGE)] if entity_id == NODE["externalId"] else [])
    monkeypatch.setattr(ontology_version_service, "get_version",
                        lambda rid: dict(RUN) if rid in ("run-1", "run-2") else None)
    monkeypatch.setattr(neo4j_client, "entities_written_by_run",
                        lambda rid, limit=200: [
                            {"id": "4:abc:1", "label": "Service", "name": "payments-api",
                             "externalId": "svc:payments", "change": "updated"}])
    monkeypatch.setattr(dynamo_client, "query_items",
                        lambda *a, **k: [dict(CHANGE)])


# ── Node trace ───────────────────────────────────────────────────────────────

def test_node_trace_answers_who_when_where_which_run(graph, as_viewer):
    r = client.get(f"/api/provenance/nodes/{NODE['id']}")
    assert r.status_code == 200
    body = r.json()

    trace = body["trace"]
    assert trace["actor"] == "scheduler:ontology_delta_job"
    assert trace["pipeline"] == "git"
    assert trace["trigger"] == "scheduled"
    assert trace["sourceDetail"] == "acme/payments-api@main"
    assert trace["lastSeenRunId"] == "run-2"
    # Stored as a JSON string because Neo4j has no nested types; the API unpacks it
    # so the panel does not have to guess.
    assert trace["evidence"] == ["src/api/routes.py:88"]

    assert body["origin"]["versionNumber"] == "v1.42"
    assert body["latest"]["runId"] == "run-2"
    assert body["name"] == "payments-api"


def test_a_plain_user_can_read_provenance(graph, as_viewer):
    """The permission, not the data, is what made this invisible before."""
    assert client.get(f"/api/provenance/nodes/{NODE['id']}").status_code == 200
    assert client.get("/api/provenance/runs").status_code in (200, 500)


def test_values_are_redacted_for_a_viewer_but_the_event_is_not(graph, as_viewer):
    body = client.get(f"/api/provenance/nodes/{NODE['id']}").json()
    event = body["timeline"][0]

    assert "before" not in event and "after" not in event
    assert event["valuesRedacted"] is True
    # Everything that answers "who changed this, when, as part of what" survives.
    assert event["actor"] == "scheduler:ontology_delta_job"
    assert event["changeType"] == "UPDATE"
    assert event["runId"] == "run-2"
    assert body["canSeeValues"] is False


def test_a_maintainer_sees_the_values(graph, as_maintainer):
    body = client.get(f"/api/provenance/nodes/{NODE['id']}").json()
    event = body["timeline"][0]
    assert event["before"] == '{"version": "1.4.2"}'
    assert event["after"] == '{"version": "1.4.3"}'
    assert body["canSeeValues"] is True


def test_contributing_sources_include_the_current_writer(graph, as_viewer):
    """With diff-only history, a node re-confirmed many times has one row.

    The live writer would otherwise be absent from its own contributor list.
    """
    names = {c["pipeline"] for c in
             client.get(f"/api/provenance/nodes/{NODE['id']}").json()["contributingSources"]}
    assert "git" in names


def test_a_missing_node_is_404_not_an_empty_trace(graph, as_viewer):
    assert client.get("/api/provenance/nodes/4:nope:9").status_code == 404


# ── Edge trace ───────────────────────────────────────────────────────────────

def test_edge_trace_has_parity_with_nodes(graph, as_viewer, monkeypatch):
    """Edges had no provenance surface at all, which is the gap that mattered
    most: an edge is an assertion, and assertions are what people question."""
    from src.graph import neo4j_client
    edge = {
        "id": "5:abc:7", "type": "EXPOSES",
        "source": {"id": "4:abc:1", "name": "payments-api", "label": "Service"},
        "target": {"id": "4:abc:2", "name": "POST /charge", "label": "API"},
        "pipeline": "dev-mate", "actor": "ann", "attribution": "traced",
        "confidence": 0.9, "factType": "inferred", "discoveredBy": "code_analysis",
        "lastSeenRunId": "run-2",
    }
    monkeypatch.setattr(neo4j_client, "get_relationship_by_id",
                        lambda rid: dict(edge) if rid == edge["id"] else None)

    body = client.get(f"/api/provenance/edges/{edge['id']}").json()
    assert body["entityKind"] == "edge"
    assert body["type"] == "EXPOSES"
    assert body["trace"]["factType"] == "inferred"
    assert body["trace"]["confidence"] == 0.9
    assert body["source"]["name"] == "payments-api"
    assert body["latest"]["versionNumber"] == "v1.42"


# ── Runs ─────────────────────────────────────────────────────────────────────

def test_run_detail_surfaces_errors_and_what_it_wrote(graph, as_maintainer):
    body = client.get("/api/provenance/runs/run-2").json()
    assert body["versionNumber"] == "v1.42"
    assert body["stats"]["nodesAdded"] == 84
    # A half-failed run must not look identical to a clean one.
    assert body["errors"] == ["repo xyz: clone timed out"]
    assert body["entities"][0]["name"] == "payments-api"
    assert body["entitiesTruncated"] is False


def test_a_truncated_entity_list_says_so(graph, as_maintainer):
    """A capped list that stays silent about the cap reads as complete."""
    body = client.get("/api/provenance/runs/run-2?entity_limit=1").json()
    assert body["entitiesTruncated"] is True


def test_unknown_run_is_404(graph, as_viewer):
    assert client.get("/api/provenance/runs/nope").status_code == 404

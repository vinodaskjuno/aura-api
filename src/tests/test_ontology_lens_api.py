"""Contract tests for the lens API.

Covers the response shape the frontend builds against, and the degradation
contract: reads return HTTP 200 with an empty payload plus ``warning`` when
Neo4j is down, while ``/lenses`` stays fully functional because it is static.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SKIP_BOOTSTRAP", "1")
os.environ.setdefault("NEO4J_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402
from src.ontology.lenses import LENSES  # noqa: E402
from src.routers.auth import get_current_user  # noqa: E402

app.dependency_overrides[get_current_user] = lambda: {
    "username": "test", "role": "admin", "permissions": ["ontology"],
}
client = TestClient(app)

LENS_IDS = sorted(LENSES)


# ── /lenses — static catalog ──────────────────────────────────────────────────

def test_lenses_catalog_works_without_neo4j():
    r = client.get("/api/ontology/lenses")
    assert r.status_code == 200
    body = r.json()
    assert {"nodeTiers", "relationshipTypes", "labels", "lenses"} <= set(body)
    assert [l["id"] for l in body["lenses"]] == list(LENSES)


@pytest.mark.parametrize("lens_id", LENS_IDS)
def test_catalog_entry_is_complete(lens_id):
    body = client.get("/api/ontology/lenses").json()
    lens = next(l for l in body["lenses"] if l["id"] == lens_id)
    assert {"id", "name", "description", "accent", "labels", "edges",
            "relationshipTypes", "tiers", "anchorLabels", "kpis"} <= set(lens)
    assert lens["labels"] and lens["edges"] and lens["kpis"]
    # Every lens label carries a tier — this is what replaces the frontend's
    # duplicated NODE_TIER map.
    assert set(lens["tiers"]) == set(lens["labels"])
    assert set(lens["anchorLabels"]) <= set(lens["labels"])


def test_catalog_edges_are_typed_not_flat():
    """A lens edge names both endpoints, not just a relationship type."""
    body = client.get("/api/ontology/lenses").json()
    git = next(l for l in body["lenses"] if l["id"] == "git")
    dep = [e for e in git["edges"] if e["type"] == "DEPENDS_ON"]
    assert dep, "Git lens should scope DEPENDS_ON"
    assert dep[0]["from"] == ["Repository"]
    assert dep[0]["to"] == ["Dependency"]


def test_catalog_marks_reversed_edges():
    """PART_OF is stored child→parent but drawn parent→child."""
    body = client.get("/api/ontology/lenses").json()
    git = next(l for l in body["lenses"] if l["id"] == "git")
    assert any(e["reverse"] for e in git["edges"])


# ── /lens/{id} — projection ───────────────────────────────────────────────────

@pytest.mark.parametrize("lens_id", LENS_IDS)
def test_lens_graph_degrades_to_200_with_warning(lens_id):
    """A read must never 500 just because the graph store is down."""
    r = client.get(f"/api/ontology/lens/{lens_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["nodes"] == []
    assert body["links"] == []
    assert body["warning"]
    assert body["meta"]["available"] is False
    # The frontend needs labels/tiers even on an empty graph to draw the legend.
    assert body["meta"]["labels"]
    assert body["meta"]["tiers"]


@pytest.mark.parametrize("lens_id", LENS_IDS)
def test_lens_response_is_a_superset_of_the_orggraph_shape(lens_id):
    """ontologyStore consumes {nodes, links} — meta must be purely additive."""
    body = client.get(f"/api/ontology/lens/{lens_id}").json()
    assert isinstance(body["nodes"], list)
    assert isinstance(body["links"], list)


def test_unknown_lens_is_404_not_500():
    r = client.get("/api/ontology/lens/definitely-not-a-lens")
    assert r.status_code == 404
    assert "unknown lens" in r.json()["detail"]


def test_unknown_lens_summary_is_404():
    assert client.get("/api/ontology/lens/nope/summary").status_code == 404


def test_limit_is_validated():
    assert client.get("/api/ontology/lens/git?limit=0").status_code == 422
    assert client.get("/api/ontology/lens/git?limit=-5").status_code == 422
    assert client.get("/api/ontology/lens/git?limit=999999").status_code == 422


def test_limit_is_echoed_in_meta():
    body = client.get("/api/ontology/lens/git?limit=250").json()
    assert body["meta"]["limit"] == 250


# ── /lens/{id}/summary ────────────────────────────────────────────────────────

@pytest.mark.parametrize("lens_id", LENS_IDS)
def test_summary_degrades_with_null_values(lens_id):
    r = client.get(f"/api/ontology/lens/{lens_id}/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["kpis"], "tiles must still be described so the header renders"
    # null, not 0 — an unknown KPI must not read as a real zero.
    assert all(k["value"] is None for k in body["kpis"])
    assert all({"id", "label", "format", "hint", "secondary"} <= set(k)
               for k in body["kpis"])


@pytest.mark.parametrize("lens_id", LENS_IDS)
def test_summary_kpi_ids_match_the_catalog(lens_id):
    catalog = client.get("/api/ontology/lenses").json()
    lens = next(l for l in catalog["lenses"] if l["id"] == lens_id)
    summary = client.get(f"/api/ontology/lens/{lens_id}/summary").json()
    assert [k["id"] for k in summary["kpis"]] == [k["id"] for k in lens["kpis"]]


def test_git_and_infra_have_distinct_kpis():
    g = {k["id"] for k in client.get("/api/ontology/lens/git/summary").json()["kpis"]}
    i = {k["id"] for k in client.get("/api/ontology/lens/infra/summary").json()["kpis"]}
    assert not g & i, f"lenses should not share KPI ids: {g & i}"


# ── no regression on the pre-existing endpoint ────────────────────────────────

def test_org_graph_still_degrades_the_same_way():
    r = client.get("/api/ontology/org-graph")
    assert r.status_code == 200
    assert r.json()["nodes"] == []
    assert r.json()["warning"]

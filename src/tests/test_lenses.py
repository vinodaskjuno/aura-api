"""Lens definition tests, driven by the real seed dataset.

These validate the *lens definitions* — the risky, hand-authored part — without
needing a running Neo4j. The seed is replayed into an in-memory graph and the
lens projection rules are applied exactly as ``get_lens_graph``'s Cypher does:
a node is in the lens when its label is, and an edge is in the lens when its
relationship type and *both* endpoint labels match one of the typed EdgeSpecs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ontology.lenses import GIT_LENS, INFRA_LENS, LENSES, Lens
from src.ontology.schema import ALL_LABELS, NODE_TIER, RELATIONSHIP_TYPES

SEED = Path(__file__).resolve().parents[2] / "data" / "aura-ontology-seed.json"


# ── seed fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def seed_graph():
    """(label_of: eid -> label, edges: list[(src_eid, rel, dst_eid)])

    Mirrors ``upsert_node``'s MERGE-on-externalId semantics: repeated records for
    the same externalId collapse to one node.
    """
    records = json.loads(SEED.read_text())
    label_of: dict[str, str] = {}
    for r in records:
        eid = r.get("properties", {}).get("externalId")
        if eid:
            label_of.setdefault(eid, r["label"])

    edges: list[tuple[str, str, str]] = []
    for r in records:
        src = r.get("properties", {}).get("externalId")
        if not src:
            continue
        for rel in r.get("relationships") or []:
            dst = rel.get("targetExternalId")
            if dst:
                edges.append((src, rel["type"], dst))
    return label_of, edges


def project(lens: Lens, seed_graph):
    """Apply the lens projection the same way the Cypher does."""
    label_of, edges = seed_graph
    lens_labels = set(lens.labels)
    nodes = {eid for eid, lab in label_of.items() if lab in lens_labels}

    specs = [(set(e.from_labels), e.rel_type, set(e.to_labels)) for e in lens.edges]
    links = []
    for src, rel, dst in edges:
        if src not in nodes or dst not in nodes:
            continue                       # no dangling links, by construction
        a, b = label_of[src], label_of[dst]
        if any(rel == r and a in f and b in t for f, r, t in specs):
            links.append((src, rel, dst))

    connected = {s for s, _, _ in links} | {d for _, _, d in links}
    return nodes, set(links), nodes - connected


# ── structural invariants (no seed needed) ────────────────────────────────────

@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_labels_are_canonical(lens):
    assert set(lens.labels) <= set(ALL_LABELS)


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_rel_types_are_canonical(lens):
    for e in lens.edges:
        assert e.rel_type in RELATIONSHIP_TYPES, f"{lens.id}: {e.rel_type}"


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_tiers_cover_exactly_the_lens_labels(lens):
    assert set(lens.tiers) == set(lens.labels)


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_edge_endpoints_stay_inside_the_lens(lens):
    """An edge to a label the lens doesn't include could never match."""
    for e in lens.edges:
        assert set(e.from_labels) <= set(lens.labels)
        assert set(e.to_labels) <= set(lens.labels)


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_anchors_are_lens_labels(lens):
    assert set(lens.anchor_labels) <= set(lens.labels)


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_kpi_ids_are_unique(lens):
    ids = [k.id for k in lens.kpis]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_no_duplicate_edge_specs(lens):
    seen = {(e.from_labels, e.rel_type, e.to_labels) for e in lens.edges}
    assert len(seen) == len(lens.edges)


def test_lens_ids_match_their_dict_keys():
    for key, lens in LENSES.items():
        assert key == lens.id


def test_node_tier_keys_are_canonical():
    """The global App↔Infra tier map must not drift from the schema."""
    assert set(NODE_TIER) <= set(ALL_LABELS)


# ── projection against the real seed ──────────────────────────────────────────

def test_git_lens_projects_a_useful_graph(seed_graph):
    nodes, links, orphans = project(GIT_LENS, seed_graph)
    # A lens that returns nothing is the failure mode this test exists to catch.
    assert len(nodes) > 60, f"only {len(nodes)} Git nodes"
    assert len(links) > 60, f"only {len(links)} Git links"
    assert len(orphans) < len(nodes) / 2


def test_infra_lens_projects_a_useful_graph(seed_graph):
    nodes, links, orphans = project(INFRA_LENS, seed_graph)
    assert len(nodes) > 40, f"only {len(nodes)} Infra nodes"
    assert len(links) > 40, f"only {len(links)} Infra links"
    assert len(orphans) < len(nodes) / 2


def test_git_lens_contains_the_structure_spine(seed_graph):
    """Repository → Module → CodeFile must survive projection."""
    _, links, _ = project(GIT_LENS, seed_graph)
    rels = {r for _, r, _ in links}
    assert "PART_OF" in rels
    assert "COMMITTED_TO" in rels


def test_git_lens_contains_the_release_spine(seed_graph):
    _, links, _ = project(GIT_LENS, seed_graph)
    rels = {r for _, r, _ in links}
    assert {"BUILT_BY", "PRODUCES", "DEPLOYED_TO"} <= rels, sorted(rels)


def test_infra_lens_contains_containment_and_placement(seed_graph):
    _, links, _ = project(INFRA_LENS, seed_graph)
    rels = {r for _, r, _ in links}
    assert "RUNS_ON" in rels
    assert {"DEPLOYED_TO", "BELONGS_TO"} & rels


def test_git_lens_excludes_the_service_dependency_mesh(seed_graph):
    """DEPENDS_ON is Repository→Dependency here, never Service→Service.

    This is the specific collision that a flat label+reltype filter cannot
    express, and the reason lens projection is server-side and typed.
    """
    label_of, _ = seed_graph
    _, links, _ = project(GIT_LENS, seed_graph)
    for src, rel, dst in links:
        if rel == "DEPENDS_ON":
            assert label_of[src] == "Repository"
            assert label_of[dst] == "Dependency"


def test_lenses_project_different_graphs(seed_graph):
    """Git and Infra must not be near-duplicates of each other."""
    git_nodes, _, _ = project(GIT_LENS, seed_graph)
    infra_nodes, _, _ = project(INFRA_LENS, seed_graph)
    overlap = git_nodes & infra_nodes
    assert len(overlap) < min(len(git_nodes), len(infra_nodes)) / 2


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_every_edge_spec_is_reachable_or_documented(lens, seed_graph):
    """Report EdgeSpecs the seed never exercises.

    Not a failure — real ingestion produces edges the seed lacks — but the list
    is what tells you a spec's direction is wrong rather than merely unused.
    """
    label_of, edges = seed_graph
    present = {(label_of.get(s), r, label_of.get(d)) for s, r, d in edges}
    unmatched = [
        f"{'|'.join(e.from_labels)} -{e.rel_type}-> {'|'.join(e.to_labels)}"
        for e in lens.edges
        if not any(
            r == e.rel_type and a in set(e.from_labels) and b in set(e.to_labels)
            for a, r, b in present
        )
    ]
    print(f"\n[{lens.id}] EdgeSpecs unexercised by the seed ({len(unmatched)}):")
    for u in unmatched:
        print("   ", u)


# ── get_lens_graph behaviour (stubbed session) ────────────────────────────────

class _Rec(dict):
    """Stands in for a neo4j Record."""


def _fake_session(nodes, rels):
    """Session stub: first query returns node rows, second returns rel rows."""
    calls: list[tuple[str, dict]] = []

    class _S:
        def run(self, cypher, **params):
            calls.append((cypher, params))
            return nodes if "labels(n)" in cypher else rels

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _S, calls


@pytest.fixture
def lens_graph(monkeypatch):
    """Returns a runner that stubs the session and calls get_lens_graph."""
    from src.graph import neo4j_client as nc

    def run(lens, nodes, rels, **kwargs):
        S, calls = _fake_session(nodes, rels)
        monkeypatch.setattr(nc, "session", lambda: S())
        result = nc.get_lens_graph(lens, **kwargs)
        return result, calls

    return run


def _node(eid, label, **props):
    return _Rec(id=eid, labels=[label], props={"name": eid, **props})


def _rel(rid, src, dst, rel_type, **props):
    return _Rec(id=rid, source=src, target=dst, rel_type=rel_type, props=props)


def test_lens_graph_stamps_lens_tier(lens_graph):
    result, _ = lens_graph(
        GIT_LENS,
        [_node("n1", "Repository"), _node("n2", "Function")],
        [],
    )
    tiers = {n["node_type"]: n["lensTier"] for n in result["nodes"]}
    assert tiers["Repository"] == GIT_LENS.tiers["Repository"]
    assert tiers["Function"] == GIT_LENS.tiers["Function"]
    # Tiers are lens-local, so Repository must outrank Function here.
    assert tiers["Repository"] < tiers["Function"]


def test_unknown_label_gets_the_deepest_tier_not_a_crash(lens_graph):
    """Defensive: a label outside lens.tiers must not KeyError."""
    result, _ = lens_graph(GIT_LENS, [_node("n1", "Unexpected")], [])
    assert result["nodes"][0]["lensTier"] == max(GIT_LENS.tiers.values())


def test_lens_graph_counts_orphans(lens_graph):
    result, _ = lens_graph(
        GIT_LENS,
        [_node("n1", "Repository"), _node("n2", "BuildPipeline"), _node("n3", "CodeFile")],
        [_rel("r1", "n1", "n2", "BUILT_BY")],
    )
    assert result["meta"]["orphanCount"] == 1        # n3
    assert result["meta"]["nodeCount"] == 3          # kept by default


def test_drop_orphans_removes_them_and_keeps_the_count(lens_graph):
    result, _ = lens_graph(
        GIT_LENS,
        [_node("n1", "Repository"), _node("n2", "BuildPipeline"), _node("n3", "CodeFile")],
        [_rel("r1", "n1", "n2", "BUILT_BY")],
        drop_orphans=True,
    )
    assert result["meta"]["nodeCount"] == 2
    assert {n["id"] for n in result["nodes"]} == {"n1", "n2"}
    # The orphan count is still reported — it's a data-quality signal.
    assert result["meta"]["orphanCount"] == 1


def test_truncated_flag_is_set_at_the_limit(lens_graph):
    nodes = [_node(f"n{i}", "Repository") for i in range(5)]
    result, _ = lens_graph(GIT_LENS, nodes, [], limit=5)
    assert result["meta"]["truncated"] is True

    result, _ = lens_graph(GIT_LENS, nodes, [], limit=50)
    assert result["meta"]["truncated"] is False


def test_provenance_source_is_renamed_on_links(lens_graph):
    """force-graph mutates link.source in place, so provenance must move aside."""
    result, _ = lens_graph(
        GIT_LENS,
        [_node("n1", "Repository"), _node("n2", "BuildPipeline")],
        [_rel("r1", "n1", "n2", "BUILT_BY", source="git", confidence=0.9)],
    )
    link = result["links"][0]
    assert link["source"] == "n1"
    assert link["prov_source"] == "git"


def test_edge_query_is_skipped_when_no_nodes_match(lens_graph):
    _, calls = lens_graph(GIT_LENS, [], [])
    assert len(calls) == 1


def test_node_query_orders_anchors_first(lens_graph):
    _, calls = lens_graph(GIT_LENS, [], [])
    node_cypher = calls[0][0]
    assert "ORDER BY CASE WHEN n:Repository|Service THEN 0 ELSE 1 END" in node_cypher


def test_node_query_excludes_retired_nodes(lens_graph):
    _, calls = lens_graph(GIT_LENS, [], [])
    assert "coalesce(n.status, 'active') <> 'retired'" in calls[0][0]


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_generated_cypher_uses_valid_label_expressions(lens, lens_graph):
    """`n:A|B`, never the invalid `n:A|n:B`."""
    _, calls = lens_graph(lens, [_node("n1", lens.anchor_labels[0])],
                          [_rel("r", "n1", "n1", lens.edges[0].rel_type)])
    for cypher, _params in calls:
        assert "|n:" not in cypher
        assert "|a:" not in cypher
        assert "|b:" not in cypher
        # At most one WHERE per MATCH clause. Two WHEREs on the same MATCH is
        # the original get_org_graph bug; two on different MATCHes is valid.
        for block in cypher.split("MATCH")[1:]:
            assert block.count("WHERE") <= 1, f"two WHEREs on one MATCH:\n{cypher}"


def test_source_and_env_filters_are_parameterised(lens_graph):
    _, calls = lens_graph(GIT_LENS, [], [], sources=["git"], envs=["prod"])
    cypher, params = calls[0]
    assert "n.source IN $sources" in cypher
    assert "n.environment IN $envs" in cypher
    assert params["sources"] == ["git"]
    assert params["envs"] == ["prod"]


@pytest.mark.parametrize("lens", list(LENSES.values()), ids=lambda l: l.id)
def test_every_label_participates_in_at_least_one_edge(lens):
    """A label with no EdgeSpec can only ever render as an orphan.

    Caught INFRA_LENS shipping ``Infrastructure`` as a label with no edges, while
    the live graph held 91 ``Service -RUNS_ON-> Infrastructure`` relationships.
    """
    used = set()
    for e in lens.edges:
        used |= set(e.from_labels) | set(e.to_labels)
    stranded = sorted(set(lens.labels) - used)
    assert not stranded, f"{lens.id}: labels in no EdgeSpec: {stranded}"

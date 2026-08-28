"""Regression tests for get_org_graph filter construction.

These cover the bugs that made ``?types=``/``?sources=`` unusable:
an invalid Neo4j 5 label expression, a duplicated WHERE clause, an alias
rewrite that silently no-op'd, NULL-``active`` edges being dropped, and raw
query-string interpolation into Cypher.

No Neo4j instance is required — the session is stubbed and we assert on the
Cypher that would be sent.
"""
import pytest

from src.graph import neo4j_client as nc
from src.ontology.schema import ALL_LABELS


# ── _label_expr ───────────────────────────────────────────────────────────────

def test_label_expr_uses_neo4j5_syntax():
    """`n:Service|API`, not the invalid `n:Service|n:API`."""
    assert nc._label_expr(["Service", "API"], "n") == "n:Service|API"


def test_label_expr_honours_the_variable_name():
    assert nc._label_expr(["Repository"], "a") == "a:Repository"


def test_label_expr_drops_unknown_labels():
    assert nc._label_expr(["Service", "NotALabel"], "n") == "n:Service"


def test_label_expr_rejects_cypher_injection():
    """A crafted ?types= value must never reach Cypher."""
    with pytest.raises(ValueError):
        nc._label_expr(["Service; DETACH DELETE n"], "n")
    with pytest.raises(ValueError):
        nc._label_expr(["`) DETACH DELETE (n"], "n")


def test_label_expr_rejects_all_unknown():
    with pytest.raises(ValueError):
        nc._label_expr(["Nope", "AlsoNope"], "n")


def test_every_label_in_the_schema_is_expressible():
    expr = nc._label_expr(ALL_LABELS, "n")
    assert expr.startswith("n:")
    assert expr.count("|") == len(ALL_LABELS) - 1


# ── row mappers ───────────────────────────────────────────────────────────────

class _Rec(dict):
    """Stands in for a neo4j Record (indexable by key)."""


def test_node_row_shape():
    row = nc._node_row(_Rec(
        id="4:abc:1",
        labels=["Repository", "Asset"],
        props={"name": "claims-api", "source": "git", "language": "Python"},
    ))
    assert row["id"] == "4:abc:1"
    assert row["label"] == "claims-api"
    assert row["node_type"] == "Repository"        # labels[0]
    assert row["source"] == "git"
    assert row["status"] == "active"               # defaulted
    assert row["language"] == "Python"             # props flattened through
    assert "name" not in row                       # promoted to `label`


def test_node_row_falls_back_to_external_id_then_element_id():
    assert nc._node_row(_Rec(id="4:a:1", labels=["Service"],
                             props={"externalId": "svc:x"}))["label"] == "svc:x"
    assert nc._node_row(_Rec(id="4:a:2", labels=["Service"],
                             props={}))["label"] == "4:a:2"


def test_link_row_renames_provenance_source():
    """force-graph mutates link.source in place, so provenance must move aside."""
    row = nc._link_row(_Rec(
        id="5:abc:7", source="4:abc:1", target="4:abc:2",
        rel_type="BUILT_BY", props={"source": "git", "confidence": 0.9},
    ))
    assert row["source"] == "4:abc:1"      # the endpoint, not the provenance
    assert row["prov_source"] == "git"
    assert row["type"] == "BUILT_BY"
    assert row["confidence"] == 0.9


# ── get_org_graph Cypher ──────────────────────────────────────────────────────

class _FakeSession:
    """Records every (cypher, params) pair and returns no rows."""

    def __init__(self, calls):
        self.calls = calls

    def run(self, cypher, **params):
        self.calls.append((cypher, params))
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def captured(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(nc, "session", lambda: _FakeSession(calls))
    return calls


def test_unfiltered_query_has_no_where_clause(captured):
    nc.get_org_graph()
    node_cypher = captured[0][0]
    assert "WHERE" not in node_cypher
    assert "ORDER BY" in node_cypher          # deterministic truncation


def test_type_filter_emits_one_valid_where(captured):
    nc.get_org_graph(type_filter=["Service", "API"])
    node_cypher, params = captured[0]
    assert "(n:Service|API)" in node_cypher
    assert node_cypher.count("WHERE") == 1     # was 2 — a syntax error
    assert "n:Service|n:API" not in node_cypher
    assert params["limit"] == 5000


def test_source_filter_is_parameterised(captured):
    nc.get_org_graph(source_filter=["git", "servicenow"])
    node_cypher, params = captured[0]
    assert "n.source IN $sources" in node_cypher
    assert params["sources"] == ["git", "servicenow"]


def test_both_filters_combine_with_and(captured):
    nc.get_org_graph(type_filter=["Repository"], source_filter=["git"])
    node_cypher = captured[0][0]
    assert node_cypher.count("WHERE") == 1
    assert " AND " in node_cypher


def test_bad_type_filter_raises_before_touching_the_session(captured):
    with pytest.raises(ValueError):
        nc.get_org_graph(type_filter=["Bogus"])
    assert captured == []


def test_relationship_query_is_skipped_when_no_nodes_matched(captured):
    """No node ids → no point querying edges."""
    nc.get_org_graph(type_filter=["Service"])
    assert len(captured) == 1                  # nodes only


def test_relationship_query_keeps_null_active_edges(monkeypatch):
    """`r.active <> false` evaluates to NULL and drops the row; coalesce doesn't."""
    calls: list[tuple[str, dict]] = []

    class _S(_FakeSession):
        def run(self, cypher, **params):
            calls.append((cypher, params))
            if "labels(n)" in cypher:
                return [_Rec(id="4:a:1", labels=["Service"], props={})]
            return []

    monkeypatch.setattr(nc, "session", lambda: _S(calls))
    nc.get_org_graph()

    rel_cypher, params = calls[1]
    assert "coalesce(r.active, true) = true" in rel_cypher
    assert "r.active <> false" not in rel_cypher
    # Edges are keyed off the returned node ids, so both endpoints are present.
    assert "elementId(b) IN $ids" in rel_cypher
    assert params["ids"] == ["4:a:1"]
    assert params["rel_limit"] == 15000

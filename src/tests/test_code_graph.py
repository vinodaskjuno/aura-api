"""Neo4j projection of code analysis: upsert, audit, and archival.

The three behaviours that matter are all about *repeat* runs, so the fake below is
stateful rather than a call recorder — a stateless double would let "re-analysing
an unchanged repo writes no audit rows" pass without being true.
"""
from __future__ import annotations

import pytest

from src.graph import code_graph
from src.services.code_parsers import Dependency, RepoFacts, Route


class FakeGraph:
    """Minimal in-memory stand-in for the bits of neo4j_client this module uses."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}          # externalId -> props
        self.labels: dict[str, str] = {}          # externalId -> label
        self.edges: dict[tuple[str, str, str], dict] = {}
        self.audits: list[dict] = []
        self.available = True

    # ── the neo4j_client surface ────────────────────────────────────────────
    def is_available(self):
        return self.available

    def upsert_node_returning_id(self, label, external_id, props):
        clean = {k: v for k, v in props.items() if v is not None}
        created = external_id not in self.nodes
        before = dict(self.nodes.get(external_id, {}))
        after = {**before, **clean}
        self.nodes[external_id] = after
        self.labels[external_id] = label
        return before, after, f"elem:{external_id}", created

    def upsert_relationship(self, from_label, from_eid, to_label, to_eid,
                            rel_type, props=None, provenance_props=None):
        self.edges[(from_eid, rel_type, to_eid)] = {
            "active": True, **(provenance_props or {})}
        return True

    def write_audit_log(self, actor, action, target_id, before, after):
        self.audits.append({"actor": actor, "action": action,
                            "targetId": target_id, "before": before, "after": after})
        return "audit-id"

    def run_query(self, cypher, params=None):
        """Only the archive statement reaches run_query from this module."""
        params = params or {}
        if "SET rel.active = false" not in cypher:
            return []
        live = set(params.get("live") or [])
        repo, rel_type = params.get("repo"), params.get("rel_type")
        rows = []
        for (src, rtype, dst), edge in self.edges.items():
            if src != repo or rtype != rel_type:
                continue
            if not edge.get("active", True) or dst in live:
                continue
            edge["active"] = False
            rows.append({"eid": dst, "id": f"elem:{dst}",
                         "name": self.nodes.get(dst, {}).get("name", dst),
                         "label": self.labels.get(dst, "Node")})
        return rows


@pytest.fixture
def graph(monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(code_graph, "neo4j", fake)
    changelog: list[dict] = []

    class FakeDynamo:
        @staticmethod
        def write_changelog(entry):
            changelog.append(entry)

        @staticmethod
        def build_changelog_entry(**kw):
            return kw

    monkeypatch.setattr(code_graph, "dynamo", FakeDynamo)
    fake.changelog = changelog
    return fake


PROJECT = {"projectId": "p1", "name": "Aura Demo Shop", "description": "d",
           "environment": "development"}


def facts(deps=(), routes=(), services=(), languages=None):
    return RepoFacts(
        languages=languages or {"Python": 3},
        dependencies=[Dependency(n, v, e) for n, v, e in deps],
        routes=[Route(m, p, "app/main.py", "FastAPI") for m, p in routes],
        services=list(services),
        tech_stack=["Python"], file_count=3,
    )


def sync(fx, **kw):
    return code_graph.sync_project(PROJECT, [], actor="alice",
                                   facts_by_label={"backend": fx}, **kw)


# ── Node creation ────────────────────────────────────────────────────────────

def test_creates_project_repository_and_children(graph):
    report = sync(facts(deps=[("fastapi", "1.0", "pypi")],
                        routes=[("GET", "/health")], services=["pricing"]))
    assert report.errors == []
    assert set(graph.nodes) == {
        "project:p1", "repo:p1:backend",
        "dep:p1:pypi:fastapi", "api:p1:GET:/health", "service:p1:pricing",
    }
    assert report.created == 5
    assert report.repositories == 1


def test_project_name_is_what_subgraph_lookup_matches_on(graph):
    """get_project_subgraph matches lower(name); the wrong value here means
    DevMate's Load Context silently finds nothing."""
    sync(facts())
    assert graph.nodes["project:p1"]["name"] == "Aura Demo Shop"


def test_every_node_is_tagged_with_its_source(graph):
    sync(facts(deps=[("react", "19", "npm")]))
    assert all(n["source"] == "code-analysis" for n in graph.nodes.values())


def test_relationships_link_repo_to_children(graph):
    sync(facts(deps=[("fastapi", "1.0", "pypi")], routes=[("GET", "/h")],
               services=["pricing"]))
    rels = {(s, r, d) for s, r, d in graph.edges}
    assert ("project:p1", "HAS_REPOSITORY", "repo:p1:backend") in rels
    assert ("repo:p1:backend", "DEPENDS_ON", "dep:p1:pypi:fastapi") in rels
    assert ("repo:p1:backend", "EXPOSES", "api:p1:GET:/h") in rels
    assert ("repo:p1:backend", "IMPLEMENTS", "service:p1:pricing") in rels


def test_regex_derived_edges_are_marked_inferred(graph):
    """Manifest facts are exact; routes and services are guesses. The graph must
    say which is which, or an inferred edge reads as established fact."""
    sync(facts(deps=[("fastapi", "1.0", "pypi")], routes=[("GET", "/h")]))
    assert graph.edges[("repo:p1:backend", "DEPENDS_ON", "dep:p1:pypi:fastapi")]["factType"] == "known"
    assert graph.edges[("repo:p1:backend", "EXPOSES", "api:p1:GET:/h")]["factType"] == "inferred"


def test_dependency_ids_are_project_scoped_but_keep_a_joinable_name(graph):
    sync(facts(deps=[("log4j", "2.0", "maven")]))
    node = graph.nodes["dep:p1:maven:log4j"]
    assert node["name"] == "log4j" and node["ecosystem"] == "maven"


# ── Upsert: the repeat-run behaviour ─────────────────────────────────────────

def test_rerunning_unchanged_creates_nothing_and_audits_nothing(graph):
    fx = facts(deps=[("fastapi", "1.0", "pypi")], routes=[("GET", "/health")])
    sync(fx)
    nodes_after_first = dict(graph.nodes)
    audits_after_first = len(graph.audits)

    second = sync(fx)

    assert set(graph.nodes) == set(nodes_after_first)   # upsert, not duplicate
    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == 4      # Project, Repository, Dependency, API
    # The point: an unchanged re-analysis must not grow the audit trail.
    assert len(graph.audits) == audits_after_first
    assert len(graph.changelog) == audits_after_first


def test_a_changed_field_is_audited_with_only_that_field(graph):
    sync(facts(deps=[("fastapi", "1.0", "pypi")]))
    graph.audits.clear(); graph.changelog.clear()

    report = sync(facts(deps=[("fastapi", "2.0", "pypi")]))

    assert report.updated == 1
    assert report.created == 0
    entry = next(a for a in graph.audits if a["action"] == "UPDATE:Dependency")
    assert entry["before"] == {"version": "1.0"}
    assert entry["after"] == {"version": "2.0"}
    assert entry["actor"] == "alice"


def test_audit_is_dual_written_and_carries_external_id(graph):
    """elementId is not stable across a DB rebuild, so history needs externalId."""
    sync(facts(deps=[("fastapi", "1.0", "pypi")]))
    row = next(c for c in graph.changelog if c["external_id"] == "dep:p1:pypi:fastapi")
    assert row["entity_id"] == "elem:dep:p1:pypi:fastapi"
    assert row["change_type"] == "CREATE"
    assert row["source"] == "code-analysis"
    assert row["actor"] == "alice"


def test_fields_owned_by_other_writers_survive(graph):
    """Ingestion and the maintainer UI write to the same nodes."""
    sync(facts())
    graph.nodes["repo:p1:backend"]["ownerTeam"] = "platform"
    sync(facts())
    assert graph.nodes["repo:p1:backend"]["ownerTeam"] == "platform"


# ── Archival ─────────────────────────────────────────────────────────────────

def test_removed_dependency_is_archived_not_deleted(graph):
    sync(facts(deps=[("fastapi", "1.0", "pypi"), ("httpx", "1.0", "pypi")]))
    report = sync(facts(deps=[("fastapi", "1.0", "pypi")]))

    assert report.archived == 1
    # The node itself survives — this layer never deletes.
    assert "dep:p1:pypi:httpx" in graph.nodes
    assert graph.edges[("repo:p1:backend", "DEPENDS_ON", "dep:p1:pypi:httpx")]["active"] is False
    assert graph.edges[("repo:p1:backend", "DEPENDS_ON", "dep:p1:pypi:fastapi")]["active"] is True
    # RELATIONSHIP_ARCHIVE, not ARCHIVE_RELATIONSHIP: this module used its own
    # spelling while routers/ontology_universe.py used the other, so one entity's
    # timeline could show the same kind of event under two names. The router's
    # spelling wins — the UI's change-type palette is already keyed on it.
    assert any(a["action"] == "RELATIONSHIP_ARCHIVE:Dependency" for a in graph.audits)


def test_archival_is_scoped_to_one_repository(graph):
    """A second repo's edges must not be archived by the first repo's sync."""
    code_graph.sync_project(PROJECT, [], actor="alice", facts_by_label={
        "backend": facts(deps=[("fastapi", "1.0", "pypi")]),
        "frontend": facts(deps=[("react", "19", "npm")]),
    })
    code_graph.sync_project(PROJECT, [], actor="alice", facts_by_label={
        "backend": facts(deps=[("fastapi", "1.0", "pypi")]),
        "frontend": facts(deps=[("react", "19", "npm")]),
    })
    assert graph.edges[("repo:p1:frontend", "DEPENDS_ON", "dep:p1:npm:react")]["active"] is True
    assert graph.edges[("repo:p1:backend", "DEPENDS_ON", "dep:p1:pypi:fastapi")]["active"] is True


def test_archiving_is_idempotent(graph):
    sync(facts(deps=[("httpx", "1.0", "pypi")]))
    first = sync(facts())
    second = sync(facts())
    assert first.archived == 1
    assert second.archived == 0      # already inactive, not re-archived


# ── Degradation and guards ───────────────────────────────────────────────────

def test_neo4j_unavailable_reports_instead_of_raising(graph):
    graph.available = False
    report = sync(facts())
    assert report.errors == ["neo4j unavailable — graph not updated"]
    assert graph.nodes == {}


def test_missing_project_identity_is_refused(graph):
    report = code_graph.sync_project({"projectId": "", "name": ""}, [], actor="a")
    assert "projectId" in report.errors[0]
    assert graph.nodes == {}


def test_no_readable_repo_is_reported(graph):
    report = code_graph.sync_project(PROJECT, [], actor="alice")
    assert report.errors == ["no readable local repository for this project"]
    assert "project:p1" in graph.nodes      # the project node is still recorded


def test_one_failing_node_does_not_abort_the_sync(graph, monkeypatch):
    original = graph.upsert_node_returning_id

    def flaky(label, eid, props):
        if eid.startswith("dep:"):
            raise RuntimeError("constraint violation")
        return original(label, eid, props)

    monkeypatch.setattr(graph, "upsert_node_returning_id", flaky)
    report = sync(facts(deps=[("fastapi", "1.0", "pypi")], routes=[("GET", "/h")]))
    assert len(report.errors) == 1 and "constraint violation" in report.errors[0]
    assert "api:p1:GET:/h" in graph.nodes    # the rest still landed


def test_mcp_connectors_are_skipped(graph):
    roots = code_graph._repo_roots([
        {"sourceType": "mcp", "repoUrl": "http://x/sse"},
        {"sourceType": "local", "localPath": "/nonexistent/path"},
    ])
    assert roots == []


def test_oversized_repos_report_truncation_rather_than_silently_dropping(graph):
    many = [(f"pkg{i}", "1.0", "pypi") for i in range(code_graph.MAX_DEPENDENCIES + 25)]
    report = sync(facts(deps=many))
    assert report.truncated and "kept 300" in report.truncated[0]


def test_special_characters_in_names_cannot_break_the_id_separator(graph):
    sync(facts(deps=[("@scope/pkg:weird", "1.0", "npm")]))
    assert "dep:p1:npm:@scope/pkg_weird" in graph.nodes

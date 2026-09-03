"""Provenance: attribution survives every path a write can take.

The failures these guard against are all silent. A node written with no context, a
thread that loses attribution, a run that overwrites a node's origin — none of them
raise, and each produces a graph that looks fully traced while lying about where its
data came from. So they are asserted rather than assumed.
"""
from __future__ import annotations

import threading

import pytest

from src.graph import provenance as prov


# ── Stamping ─────────────────────────────────────────────────────────────────

def test_no_context_is_recorded_not_silent():
    """Outside a run, "we don't know" must be a value, not a missing key."""
    props = prov.stamp({"name": "orphan"})
    assert props["attribution"] == prov.ATTRIBUTION_NONE
    assert props["pipeline"] == prov.PIPELINE_UNKNOWN
    assert "lastSeenRunId" not in props, "an empty run id would be worse than none"


def test_context_fills_who_when_where():
    with prov.trace_run(prov.PIPELINE_GIT, trigger=prov.TRIGGER_MANUAL,
                        actor="vinod@askjuno.com", source="git",
                        sourceDetail="acme/api@main", writtenBy="test",
                        record=False) as run:
        props = prov.stamp({"name": "svc"})

    assert props["actor"] == "vinod@askjuno.com"
    assert props["pipeline"] == prov.PIPELINE_GIT
    assert props["trigger"] == prov.TRIGGER_MANUAL
    assert props["sourceDetail"] == "acme/api@main"
    assert props["lastSeenRunId"] == run.runId
    # versionId is kept as an alias so the pre-existing /nodes/{id}/version
    # endpoint and the ("Service","versionId") index keep working.
    assert props["versionId"] == run.runId
    assert props["attribution"] == prov.ATTRIBUTION_TRACED


def test_caller_values_are_never_overwritten():
    """qatest writes a TestRun whose own `runId` is the test run, not the ingestion.

    Clobbering it would corrupt real data to record bookkeeping.
    """
    with prov.trace_run(prov.PIPELINE_QA_MIND, source="qa-test", record=False) as run:
        props = prov.stamp({"runId": "TEST-123", "source": "qa-test",
                            "pipeline": "caller-owned"})

    assert props["runId"] == "TEST-123"
    assert props["source"] == "qa-test"
    assert props["pipeline"] == "caller-owned"
    # …while the fields provenance owns outright still describe this write.
    assert props["lastSeenRunId"] == run.runId


def test_first_seen_is_separate_from_the_always_applied_half():
    """Origin facts go in ON CREATE SET, or a nightly job re-dates the whole graph."""
    with prov.trace_run(prov.PIPELINE_GIT, actor="ann", record=False) as run:
        first = prov.first_seen_props()
        always = prov.stamp({})

    assert first["firstSeenRunId"] == run.runId
    assert first["createdBy"] == "ann"
    assert "firstSeenAt" in first
    # The two halves must not overlap, or the ON CREATE guard is pointless.
    assert not (set(first) & set(always)), "a field in both halves is overwritten on update"


# ── Runs ─────────────────────────────────────────────────────────────────────

def test_nested_run_inherits_and_records_its_parent():
    with prov.trace_run(prov.PIPELINE_DEV_MATE, actor="ann", trigger=prov.TRIGGER_MANUAL,
                        sourceDetail="payments", record=False) as outer:
        with prov.trace_run(prov.PIPELINE_QA_MIND, record=False) as inner:
            assert inner.parentRunId == outer.runId
            assert inner.actor == "ann", "an unset field falls back to the parent"
            assert inner.sourceDetail == "payments"
            assert inner.pipeline == prov.PIPELINE_QA_MIND, "an explicit field wins"


def test_ensure_run_does_not_fragment_an_active_run():
    """A mid-level helper must not open a child run for every write."""
    with prov.trace_run(prov.PIPELINE_SELF_LEARNING, record=False) as outer:
        with prov.ensure_run(prov.PIPELINE_SELF_LEARNING) as inner:
            assert inner.runId == outer.runId

    with prov.ensure_run(prov.PIPELINE_MANUAL, record=False) as standalone:
        assert standalone.runId, "with no active run it must open one"


def test_refine_changes_detail_but_keeps_the_run_and_its_counters():
    """An MCP load spanning three servers is one run with three source labels."""
    with prov.trace_run(prov.PIPELINE_MCP, record=False) as run:
        with prov.refine(source="mcp:shop", sourceDetail="Shop MCP"):
            assert prov.stamp({})["lastSeenRunId"] == run.runId
            assert prov.stamp({})["source"] == "mcp:shop"
            prov.current().bump("nodesAdded", 5)
        with prov.refine(source="mcp:sre", sourceDetail="SRE MCP"):
            prov.current().bump("nodesAdded", 3)
        # Counting accrues to the one run, not to per-server copies.
        assert run.snapshot_stats()["nodesAdded"] == 8
        assert prov.current().source == "", "refine must not leak past its block"


def test_a_failing_run_records_the_error_and_re_raises():
    captured = {}

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with prov.trace_run(prov.PIPELINE_GIT, record=False) as run:
            captured["run"] = run
            raise Boom("clone failed")

    assert any("clone failed" in e for e in captured["run"].errors)


def test_errors_are_capped_so_one_bad_source_cannot_grow_unbounded():
    with prov.trace_run(prov.PIPELINE_GIT, record=False) as run:
        for i in range(500):
            run.fail(f"bad record {i}")
    assert len(run.errors) == 100


# ── Threads ──────────────────────────────────────────────────────────────────

def test_context_does_not_reach_a_bare_thread():
    """The failure mode this module is most likely to hit, asserted directly.

    If this ever starts passing, `traced_callable` is redundant — but until then,
    a plain Thread loses attribution silently.
    """
    seen = {}

    def worker():
        seen["actor"] = prov.current().actor

    with prov.trace_run(prov.PIPELINE_GIT, actor="ann", record=False):
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    assert seen["actor"] == "", "a bare thread starts with an empty context"


def test_traced_callable_carries_the_context_across_a_thread():
    seen = {}

    def worker():
        ctx = prov.current()
        seen["actor"] = ctx.actor
        seen["runId"] = ctx.runId
        ctx.bump("nodesAdded", 2)

    with prov.trace_run(prov.PIPELINE_GIT, actor="ann", record=False) as run:
        t = threading.Thread(target=prov.traced_callable(worker))
        t.start()
        t.join()

    assert seen["actor"] == "ann"
    assert seen["runId"] == run.runId
    assert run.snapshot_stats()["nodesAdded"] == 2, "counters are shared, not copied"


def test_spawn_traced_starts_an_attributed_thread():
    seen = {}
    with prov.trace_run(prov.PIPELINE_QA_MIND, actor="qatest", record=False) as run:
        prov.spawn_traced(lambda: seen.update(runId=prov.current().runId)).join()
    assert seen["runId"] == run.runId


# ── The write path ───────────────────────────────────────────────────────────
#
# The claim the whole design rests on is that provenance travels in the same
# statement as the data, and so reaches every configured engine identically. That
# is asserted here against the fan-out session rather than taken on trust.

class _Result(list):
    """Just enough of a driver result for the upsert helpers.

    `single()` returning None makes every write report "nothing came back", which
    is the shape these tests want — they assert on the statement that was sent, not
    on what the engine would have returned.
    """

    @staticmethod
    def single():
        return None


class _RecordingSession:
    def __init__(self, store, fail=False):
        self.store, self.fail = store, fail

    def run(self, query, parameters=None, **kwargs):
        if self.fail:
            raise RuntimeError("engine down")
        self.store.append((query, {**(parameters or {}), **kwargs}))
        return _Result()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _DrivableBackend:
    """FakeBackend plus the `driver()` that `routed_session` goes through.

    test_graph_routing exercises `_FanOutSession` directly, so its FakeBackend never
    needed one. These tests call the real `upsert_*` helpers, which reach the engine
    the way the application does.
    """

    def __init__(self, name, fail=False):
        from src.graph import backends
        from src.graph.dialects import DIALECTS
        self.name, self.fail = name, fail
        self.statements: list = []
        self.dialect = DIALECTS["neo4j"]
        self.config = backends.BackendConfig(name, f"bolt://{name}:7687", "u", "p")

    def _session(self):
        return _RecordingSession(self.statements, self.fail)

    session = _session

    def driver(self):
        outer = self

        class _Driver:
            @staticmethod
            def session(**_kwargs):
                return outer._session()

        return _Driver()

    def is_available(self):
        return not self.fail


@pytest.fixture
def routed(monkeypatch):
    """Two fake engines behind the real routing layer."""
    from src.graph import backends, graph_config, outbox

    primary, shadow = _DrivableBackend("neo4j"), _DrivableBackend("memgraph")
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


def test_upsert_node_carries_provenance_into_the_statement(routed):
    from src.graph import neo4j_client as neo4j
    primary, _, _ = routed

    with prov.trace_run(prov.PIPELINE_GIT, trigger=prov.TRIGGER_SCHEDULED,
                        actor="scheduler:delta", source="git",
                        sourceDetail="acme/api@main", record=False) as run:
        neo4j.upsert_node("Service", "svc:1", {"name": "payments"})

    cypher, params = primary.statements[-1]
    # Origin facts must be guarded, or every ingestion re-dates the node.
    assert "ON CREATE SET" in cypher
    assert params["first"]["firstSeenRunId"] == run.runId
    assert params["props"]["actor"] == "scheduler:delta"
    assert params["props"]["trigger"] == prov.TRIGGER_SCHEDULED
    assert params["props"]["sourceDetail"] == "acme/api@main"
    assert params["props"]["lastSeenRunId"] == run.runId
    assert "firstSeenAt" not in params["props"], "origin must not be in the always-set half"


def test_both_engines_receive_identical_provenance(routed):
    """The parity claim, without needing two live engines.

    `_FanOutSession` mirrors by replaying the same statement and parameters, so a
    divergence here would mean provenance is being derived per-engine — which would
    make the two stores disagree about where the data came from.
    """
    from src.graph import neo4j_client as neo4j
    primary, shadow, _ = routed

    with prov.trace_run(prov.PIPELINE_QA_MIND, actor="qatest", record=False):
        neo4j.upsert_node("TestCase", "case:1", {"name": "checkout"})

    assert shadow.statements, "the write did not reach the secondary at all"
    assert primary.statements[-1] == shadow.statements[-1]


def test_a_mirror_outage_queues_the_write_with_its_provenance(routed, monkeypatch):
    """The outbox replays parameters verbatim, so attribution must survive it."""
    from src.graph import backends, graph_config, outbox
    from src.graph import neo4j_client as neo4j

    primary, down = _DrivableBackend("neo4j"), _DrivableBackend("memgraph", fail=True)
    queued: list[dict] = []
    monkeypatch.setattr(backends, "get_backend",
                        lambda name=None: {"neo4j": primary, "memgraph": down}.get(
                            name or "neo4j"))
    monkeypatch.setattr(graph_config, "get_config", lambda refresh=False:
                        graph_config.GraphConfig("neo4j", ("neo4j", "memgraph")))
    monkeypatch.setattr(outbox, "enqueue",
                        lambda b, c, p, e: queued.append({"backend": b, "params": p}))

    with prov.trace_run(prov.PIPELINE_GIT, actor="ann", record=False) as run:
        neo4j.upsert_node("Service", "svc:2", {"name": "billing"})

    assert queued, "a failed mirror must be queued, not dropped"
    assert queued[0]["params"]["props"]["lastSeenRunId"] == run.runId
    assert queued[0]["params"]["props"]["actor"] == "ann"


def test_relationships_are_attributed_too(routed):
    """Edges had provenance only when a caller passed it; now the run always does."""
    from src.graph import neo4j_client as neo4j
    primary, _, _ = routed

    with prov.trace_run(prov.PIPELINE_DEV_MATE, actor="ann", source="code-analysis",
                        writtenBy="analyzer", record=False) as run:
        neo4j.upsert_relationship("Service", "svc:1", "API", "api:1", "EXPOSES",
                                  provenance_props={"confidence": 0.9,
                                                    "factType": "inferred"})

    _, params = primary.statements[-1]
    assert params["props"]["confidence"] == 0.9, "the caller's own judgement is kept"
    assert params["props"]["factType"] == "inferred"
    assert params["props"]["discoveredBy"] == "analyzer"
    assert params["props"]["lastSeenRunId"] == run.runId


def test_label_free_links_are_attributed(routed):
    """`link_nodes_by_eid` had no provenance at all — it is what MCP and the repo
    loader use, so their edges were the least traceable thing in the graph."""
    from src.graph import neo4j_client as neo4j
    primary, _, _ = routed

    with prov.trace_run(prov.PIPELINE_MCP, actor="ann", source="mcp:shop",
                        record=False) as run:
        neo4j.link_nodes_by_eid("service:a", "database:b", "CONNECTS_TO")

    _, params = primary.statements[-1]
    assert params["props"]["lastSeenRunId"] == run.runId
    assert params["props"]["source"] == "mcp:shop"
    assert params["first"]["firstSeenRunId"] == run.runId


# ── Diff-only history ────────────────────────────────────────────────────────
#
# The load-bearing property of the whole design: a run that re-confirms 50,000
# nodes without changing them writes no history rows. If this regresses, the
# ledger grows by the size of the graph on every nightly job.

class _FakeGraph:
    """Stateful, not a call recorder — "re-running writes nothing" cannot be
    tested against a double that forgets what it already holds."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.audits: list[dict] = []

    def upsert_node_returning_id(self, label, external_id, props):
        clean = {k: v for k, v in props.items() if v is not None}
        created = external_id not in self.nodes
        before = dict(self.nodes.get(external_id, {}))
        after = {**before, **clean}
        self.nodes[external_id] = after
        return before, after, f"elem:{external_id}", created

    def write_audit_log(self, actor, action, target_id, before, after):
        self.audits.append({"actor": actor, "action": action})
        return "audit-id"


class _FakeStore:
    def __init__(self):
        self.rows: list[dict] = []

    def write_changelog(self, entry):
        self.rows.append(entry)

    @staticmethod
    def build_changelog_entry(**kw):
        return kw


def test_an_unchanged_re_run_writes_no_history():
    graph, store = _FakeGraph(), _FakeStore()
    props = {"name": "payments", "version": "1.4.2", "source": "git"}

    with prov.trace_run(prov.PIPELINE_GIT, actor="ann", record=False):
        prov.traced_upsert("Service", "svc:1", props, graph=graph, store=store)
    assert len(store.rows) == 1 and store.rows[0]["change_type"] == "CREATE"

    # Same input, second run. Only bookkeeping (updatedAt, lastSeenRunId…) differs.
    with prov.trace_run(prov.PIPELINE_GIT, actor="ann", record=False) as run2:
        _eid, created, diff = prov.traced_upsert(
            "Service", "svc:1", props, graph=graph, store=store)

    assert created is False
    assert diff == {}, f"bookkeeping was mistaken for a change: {diff}"
    assert len(store.rows) == 1, "a no-op re-run must not write a history row"
    # …but the node is still recorded as seen by the second run, which is what
    # makes "re-confirmed, no change" showable in the timeline.
    assert run2.snapshot_stats()["nodesUnchanged"] == 1


def test_a_real_change_is_recorded_with_only_the_changed_field():
    graph, store = _FakeGraph(), _FakeStore()
    with prov.trace_run(prov.PIPELINE_GIT, actor="ann", record=False):
        prov.traced_upsert("Service", "svc:1",
                           {"name": "payments", "version": "1.4.2"},
                           graph=graph, store=store)
        store.rows.clear()
        _eid, created, diff = prov.traced_upsert(
            "Service", "svc:1", {"name": "payments", "version": "1.4.3"},
            graph=graph, store=store)

    assert created is False
    assert diff == {"version": ("1.4.2", "1.4.3")}
    row = store.rows[0]
    assert row["change_type"] == "UPDATE"
    # Only the field that moved — not the whole node, which is what made the old
    # JSON-blob diff unreadable.
    assert row["before"] == {"version": "1.4.2"}
    assert row["after"] == {"version": "1.4.3"}


def test_an_explicit_actor_beats_the_ambient_one():
    """`sync_project(actor=…)` knows who asked; the run may have been opened
    by something further out."""
    graph, store = _FakeGraph(), _FakeStore()
    with prov.trace_run(prov.PIPELINE_DEV_MATE, actor="run-opener", record=False):
        prov.traced_upsert("Service", "svc:1", {"name": "x"},
                           actor="alice", graph=graph, store=store)
    assert store.rows[0]["actor"] == "alice"


def test_the_history_row_carries_the_run_that_wrote_it():
    """Without this, the Run Inspector cannot answer "what did this run change?"."""
    import src.database.dynamo_client as real_dynamo
    graph = _FakeGraph()
    captured: list[dict] = []

    class Store:
        @staticmethod
        def write_changelog(entry):
            captured.append(entry)
        build_changelog_entry = staticmethod(real_dynamo.build_changelog_entry)

    with prov.trace_run(prov.PIPELINE_GIT, actor="ann", trigger=prov.TRIGGER_SCHEDULED,
                        record=False) as run:
        prov.traced_upsert("Service", "svc:1", {"name": "x"},
                           graph=graph, store=Store)

    assert captured[0]["runId"] == run.runId
    assert captured[0]["trigger"] == prov.TRIGGER_SCHEDULED
    assert captured[0]["entityKind"] == "node"

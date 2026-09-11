"""DevMate: the recording that makes its metrics mean something.

Every test here guards a gap found by probing the live environment, where
DevMate's entire footprint was ONE token-usage row — untagged, from a turn that
left no session record and no run record, alongside a proposal system in which
applying and discarding a change were byte-for-byte indistinguishable afterwards.
"""
from __future__ import annotations

import json

import pytest

from src.services.advisor import tools


# ── Staging carries provenance ───────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A cloned project whose staging area the tools can reach."""
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr(tools, "_resolve_project_dir", lambda pid: clone)
    return clone


@pytest.fixture
def written(monkeypatch):
    """Capture devmate-proposals rows instead of writing to DynamoDB."""
    rows: list[dict] = []
    import src.database.dynamo_client as db

    def put_item(table, item):
        if table == "devmate-proposals":
            rows.append(item)

    monkeypatch.setattr(db, "put_item", put_item)
    return rows


def test_a_staged_proposal_records_who_asked_for_it(repo):
    """The REST worker that later applies this knows only the project and the
    path, so provenance has to be captured at staging time or not at all."""
    tools._stage_write("p1", "src/a.py", "new", session_id="s1", user_id="u1")
    staged = tools._stage_read("p1", "src/a.py")
    assert staged["sessionId"] == "s1"
    assert staged["userId"] == "u1"
    assert staged["proposedAt"]


def test_applying_and_discarding_are_distinguishable_afterwards(repo, written):
    """The regression test for the behaviour this replaces: both paths called
    `_stage_pop`, which unlinks the file, and wrote nothing. Afterwards the two
    outcomes left exactly the same trace — none."""
    tools._stage_write("p1", "a.py", "content-a", session_id="s1", user_id="u1")
    tools.apply_pending("p1", "a.py", decided_by="vinoth")

    tools._stage_write("p1", "b.py", "content-b", session_id="s1", user_id="u1")
    tools.discard_pending("p1", "b.py", decided_by="vinoth")

    assert [r["decision"] for r in written] == ["applied", "discarded"]
    assert {r["path"] for r in written} == {"a.py", "b.py"}
    assert all(r["sessionId"] == "s1" for r in written)
    assert all(r["decidedBy"] == "vinoth" for r in written)


def test_applying_actually_writes_the_file(repo, written):
    tools._stage_write("p1", "a.py", "hello", session_id="s", user_id="u")
    assert tools.apply_pending("p1", "a.py")["success"] is True
    assert (repo / "a.py").read_text() == "hello"


def test_discarding_does_not_write_the_file(repo, written):
    tools._stage_write("p1", "b.py", "nope", session_id="s", user_id="u")
    assert tools.discard_pending("p1", "b.py")["success"] is True
    assert not (repo / "b.py").exists()


def test_the_recorded_diff_size_is_measured_before_the_write(repo, written):
    """Measured against the file as it was. Taken after the write it would
    always be zero, because the staged content IS the file by then."""
    (repo / "a.py").write_text("one\ntwo\n")
    tools._stage_write("p1", "a.py", "one\ntwo\nthree\nfour\n")
    tools.apply_pending("p1", "a.py")
    assert written[0]["additions"] == 2
    assert written[0]["deletions"] == 0


def test_a_failed_audit_write_does_not_fail_the_file_change(repo, monkeypatch):
    """The operator asked for a file change. A DynamoDB hiccup must not stop it
    — the audit row is best-effort, exactly as token-usage persistence is."""
    import src.database.dynamo_client as db
    monkeypatch.setattr(db, "put_item",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dynamo down")))

    tools._stage_write("p1", "a.py", "still written")
    result = tools.apply_pending("p1", "a.py")

    assert result["success"] is True
    assert (repo / "a.py").read_text() == "still written"


def test_a_decision_on_an_absent_proposal_is_refused_not_recorded(repo, written):
    assert "error" in tools.apply_pending("p1", "ghost.py")
    assert "error" in tools.discard_pending("p1", "ghost.py")
    assert written == []


def test_an_old_stage_file_without_provenance_still_records(repo, written):
    """Files staged before provenance existed have no sessionId. A decision on
    one must still produce a row rather than raising."""
    tools._stage_file("p1", "legacy.py").write_text(
        json.dumps({"path": "legacy.py", "content": "x"}), encoding="utf-8")
    tools.apply_pending("p1", "legacy.py", decided_by="vinoth")
    assert written[0]["decision"] == "applied"
    assert written[0]["sessionId"] == ""
    assert written[0]["userId"] == "vinoth"        # falls back to the decider


# ── Token attribution ────────────────────────────────────────────────────────

def test_a_devmate_turn_tags_its_token_row(monkeypatch):
    """Untagged, DevMate spend is indistinguishable from a row whose origin was
    never recorded — `get_tool_breakdown` files it under "other". Live, 11,494
    of 11,495 rows carried a source and DevMate's one row did not."""
    captured: list[dict] = []
    import src.database.dynamo_client as db
    monkeypatch.setattr(db, "put_item",
                        lambda table, item: captured.append((table, item)))

    from src.services.advisor.react_orchestrator import _persist_token_usage
    _persist_token_usage("u1", "s1", "p1", "claude-sonnet-4-5", 92, 133, 0.0022)

    table, row = captured[0]
    assert table == "token-usage"
    assert row["source"] == "dev-mate"
    assert row["tool"] == "dev-mate"
    assert row["sessionId"] == "s1" and row["projectId"] == "p1"


def test_the_token_row_keeps_every_field_it_had_before(monkeypatch):
    """Tagging must be additive — the metrics endpoints read these."""
    captured: list[dict] = []
    import src.database.dynamo_client as db
    monkeypatch.setattr(db, "put_item",
                        lambda table, item: captured.append(item))

    from src.services.advisor.react_orchestrator import _persist_token_usage
    _persist_token_usage("u1", "s1", "p1", "m", 10, 20, 1.5)

    row = captured[0]
    for field in ("userId", "sortKey", "sessionId", "projectId", "model",
                  "inputTokens", "outputTokens", "cost", "timestamp"):
        assert field in row, field
    assert row["cost"] == "1.5"          # still a string, as DynamoDB needs


# ── The run record ───────────────────────────────────────────────────────────

def test_the_dev_mate_pipeline_is_registered_for_a_run():
    """`PIPELINE_DEV_MATE` existed all along, but the only writer was the
    Reverse Engineering Analyse button — so `pipeline='dev-mate'` had zero rows
    live, and the delivery board's Mapped column was unreachable through runs."""
    from src.graph import provenance
    assert provenance.PIPELINE_DEV_MATE == "dev-mate"

    import inspect
    from src.services.advisor import react_orchestrator
    source = inspect.getsource(react_orchestrator.run_advisor)
    assert "PIPELINE_DEV_MATE" in source
    assert "trace_run" in source


# ── The view, and the scale fuse ─────────────────────────────────────────────

from src.services import role_metrics as rm     # noqa: E402


@pytest.fixture
def devmate(monkeypatch):
    """DevMate's sources, with a counter on the expensive one."""
    class H:
        projects: list = []
        sessions: list = []
        proposals: list = []
        tokens: list = []
        runs: list = []
        pending: dict = {}          # projectId -> number of staged changes
        calls: dict = {}

    h = H()
    h.calls = {"list_pending": [], "pending_count": []}

    import src.database.dynamo_client as db
    monkeypatch.setattr(db, "scan_items", lambda table, filter_expr=None, limit=500: {
        "projects": h.projects, "chat-sessions": h.sessions,
        "devmate-proposals": h.proposals, "token-usage": h.tokens,
    }.get(table, []))
    monkeypatch.setattr(db, "query_items", lambda *a, **k: [])

    import src.services.ontology_version_service as ovs
    monkeypatch.setattr(ovs, "list_versions", lambda limit=50, **k: list(h.runs))

    import src.graph.neo4j_client as neo
    monkeypatch.setattr(neo, "is_available", lambda: False)

    from src.services.advisor import tools
    def pending_count(pid):
        h.calls["pending_count"].append(pid)
        return h.pending.get(pid, 0)
    def list_pending(pid):
        h.calls["list_pending"].append(pid)
        return [{"path": f"f{i}.py", "diff": "", "additions": 1, "deletions": 0}
                for i in range(h.pending.get(pid, 0))]
    monkeypatch.setattr(tools, "pending_count", pending_count)
    monkeypatch.setattr(tools, "list_pending", list_pending)
    return h


def _dm(role="user_dev"):
    return rm.build_devmate_view({"role": role, "username": "t", "userId": "u1",
                                  "permissions": ["dev_workspace"]})


def _projects(n, **extra):
    return [{"projectId": f"p{i}", "name": f"proj-{i}", "userId": "u1",
             "updatedAt": f"2026-09-{(i % 28) + 1:02d}T00:00:00+00:00", **extra}
            for i in range(n)]


def test_the_hero_is_capped_whatever_the_estate_size(devmate):
    devmate.projects = _projects(100)
    view = _dm()
    assert view["hero"]["shown"] == rm.HERO_PROJECTS
    assert view["hero"]["total"] == 100
    assert len(view["hero"]["cards"]) == rm.HERO_PROJECTS


def test_the_expensive_pending_call_is_bounded_by_the_hero_cap(devmate):
    """`list_pending` resolves the project (a DynamoDB query on a cache miss),
    walks the staging directory and diffs every file. Called once per project it
    would make the page O(estate) queries per render — the regression that only
    shows up on a large client's machine."""
    devmate.projects = _projects(100)
    _dm()
    assert len(devmate.calls["list_pending"]) <= rm.HERO_PROJECTS
    # Ranking may probe every project, but only with the cheap filesystem check.
    assert len(devmate.calls["pending_count"]) == 100


def test_a_project_with_staged_changes_outranks_a_more_recent_one(devmate):
    devmate.projects = [
        {"projectId": "recent", "name": "recent", "userId": "u1",
         "updatedAt": "2026-09-11T00:00:00+00:00"},
        {"projectId": "waiting", "name": "waiting", "userId": "u1",
         "updatedAt": "2026-01-01T00:00:00+00:00"},
    ]
    devmate.pending = {"waiting": 3}
    assert [c["name"] for c in _dm()["hero"]["cards"]] == ["waiting", "recent"]


def test_a_project_never_touched_does_not_lead_the_hero(devmate):
    """An absent timestamp is an empty string, which sorts before every real
    date — so without an explicit key it led the list."""
    devmate.projects = [
        {"projectId": "never", "name": "never", "userId": "u1"},
        {"projectId": "recent", "name": "recent", "userId": "u1",
         "updatedAt": "2026-09-11T00:00:00+00:00"},
    ]
    assert [c["name"] for c in _dm()["hero"]["cards"]] == ["recent", "never"]


def test_a_small_estate_does_not_announce_a_subset(devmate):
    """At four projects a "4 of 4" line would be noise — the hero IS everything."""
    devmate.projects = _projects(4)
    view = _dm()
    assert view["hero"]["shown"] == 4
    assert "of" not in view["headline"]["detail"].split("\u00b7")[0]


def test_a_large_estate_states_the_true_total(devmate):
    """The cap must never be mistaken for the size of the estate."""
    devmate.projects = _projects(30)
    view = _dm()
    assert view["hero"]["total"] == 30
    assert "6 of 30 projects" in view["headline"]["detail"]


def test_the_catalogue_is_not_shipped_in_the_payload(devmate):
    """ProjectsPanel renders it client-side, with search, fetching its own rows.
    Sending a hundred more rows here would duplicate that list and lose the
    search that makes a large estate navigable at all."""
    devmate.projects = _projects(100)
    view = _dm()
    total_rows = sum(len(b.get("rows", [])) for b in view["blocks"])
    assert total_rows < rm.HERO_PROJECTS + 10, "payload should not carry the catalogue"


def test_waiting_total_says_it_covers_only_the_hero_set(devmate):
    """Claiming a sweep of 100 projects when six were checked would be a lie."""
    devmate.projects = _projects(100)
    devmate.pending = {"p0": 2}
    metrics = {i["label"]: i for b in _dm()["blocks"]
               for i in b.get("items", [])}
    assert "most active" in metrics["Awaiting you"]["basis"]


def test_sessions_report_unmeasured_not_zero_when_none_exist(devmate):
    """`chat-sessions` is empty live, and `0` would say conversations happen and
    none were kept — a different and untrue statement."""
    devmate.projects = _projects(2)
    metrics = {i["label"]: i for b in _dm()["blocks"] for i in b.get("items", [])}
    assert metrics["Sessions"]["value"] is None
    assert metrics["Sessions"]["state"] == "unmeasured"


def test_advice_applied_is_unmeasured_until_something_is_decided(devmate):
    devmate.projects = _projects(2)
    metrics = {i["label"]: i for b in _dm()["blocks"] for i in b.get("items", [])}
    assert metrics["Advice applied"]["value"] is None
    assert "no proposal has been decided" in metrics["Advice applied"]["basis"]


def test_advice_applied_reports_a_rate_once_decisions_exist(devmate):
    devmate.projects = _projects(2)
    devmate.proposals = [{"decision": "applied"}, {"decision": "applied"},
                         {"decision": "discarded"}, {"decision": "discarded"}]
    metrics = {i["label"]: i for b in _dm()["blocks"] for i in b.get("items", [])}
    assert metrics["Advice applied"]["value"] == 50


def test_only_an_admin_sees_spend(devmate):
    devmate.projects = _projects(2)
    labels = lambda role: {i["label"] for b in _dm(role)["blocks"]
                           for i in b.get("items", [])}
    assert "Spend" not in labels("user_dev")
    assert "Spend" in labels("admin")


def test_an_empty_estate_invites_a_first_project(devmate):
    view = _dm()
    assert "first project" in view["headline"]["text"]
    assert view["hero"]["cards"] == []


def test_a_builder_failure_still_returns_a_page(devmate, monkeypatch):
    monkeypatch.setattr(rm, "_devmate_cards",
                        lambda d: (_ for _ in ()).throw(ValueError("boom")))
    view = _dm()
    assert view["headline"]["state"] == "unavailable"
    assert "boom" in view["headline"]["detail"]

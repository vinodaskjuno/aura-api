"""DevMate's own lineage: recorded, and now actually readable.

A DevMate turn generates the most interesting provenance in the product — a human
asks, the agent proposes, the human decides, files change, a commit carries them —
and until now it captured about half of it and rendered none. Worse, two of the
things it did record were wrong in ways that reached other people's screens.
"""
from __future__ import annotations

import pytest

from src.graph import provenance
from src.services import role_metrics


# ── The run record says who, and points somewhere ───────────────────────────

def test_a_run_record_carries_the_session_and_the_actor_id(monkeypatch):
    """`TraceContext` has carried both since it was written and `create_version_record`
    dropped both, so a run could never be walked back to its conversation."""
    captured: dict = {}
    from src.services import ontology_version_service as versions
    monkeypatch.setattr(versions, "put_item", lambda *a, **k: None, raising=False)

    import src.database.dynamo_client as db
    monkeypatch.setattr(db, "put_item", lambda table, item: captured.update(item))
    monkeypatch.setattr(versions, "_next_version_number", lambda: "1")

    versions.create_version_record(
        load_method="dev-mate", actor="alice", session_id="sess-1", actor_id="u1",
        project_id="p1")

    assert captured["sessionId"] == "sess-1"
    assert captured["actorId"] == "u1"
    assert captured["actor"] == "alice"


def test_the_advisor_names_the_human_not_system():
    """`_open_run_record` falls back to `actor or "system"`, and the Lineage feed
    renders `actor` as its identity column. Passing only actorId — which is what the
    call did — filed every DevMate turn under "system"."""
    import inspect
    from src.services.advisor import react_orchestrator

    source = inspect.getsource(react_orchestrator.run_advisor)
    assert "actor=actor" in source, "the username is not passed to trace_run"

    # And the router has one to pass.
    from src.routers import advisor as advisor_router
    assert "actor=user.get(\"username\"" in inspect.getsource(advisor_router)


# ── "Mapped" must mean something was mapped ─────────────────────────────────

def _run(pipeline, status="success", **stats):
    return {"pipeline": pipeline, "status": status, "projectId": "p1",
            "stats": stats or {}}


def test_a_chat_turn_does_not_mark_a_project_mapped(monkeypatch):
    """Every DevMate turn now opens a `pipeline='dev-mate'` run carrying a projectId,
    and a conversational turn always closes "success" — status only flips on an
    exception. So asking "what does this repo do?" lit the Mapped column, under a
    legend promising the stage is derived from runs that actually succeeded."""
    data = role_metrics._Data.__new__(role_metrics._Data)
    data.__dict__["runs_by_project"] = {"p1": [_run("dev-mate")]}
    data.__dict__["graph_by_project"] = {}
    data.__dict__["unattributed_runs"] = []

    marks = role_metrics._Data.stage_marks(data, {"projectId": "p1"})

    assert marks[2] != "done", "a zero-write chat turn marked the project Mapped"


def test_a_run_that_wrote_nodes_still_marks_it_mapped(monkeypatch):
    data = role_metrics._Data.__new__(role_metrics._Data)
    data.__dict__["runs_by_project"] = {"p1": [_run("dev-mate", nodesAdded=12)]}
    data.__dict__["graph_by_project"] = {}
    data.__dict__["unattributed_runs"] = []

    assert role_metrics._Data.stage_marks(data, {"projectId": "p1"})[2] == "done"


def test_an_unchanged_run_is_not_evidence_of_mapping():
    """`nodesUnchanged` means the pipeline looked at everything and produced nothing.
    That is evidence it ran, not evidence it mapped."""
    assert not role_metrics._wrote_anything({"stats": {"nodesUnchanged": 900}})
    assert role_metrics._wrote_anything({"stats": {"relsAdded": 1}})


# ── The proposal ledger is readable, and links forward ──────────────────────

def test_a_commit_is_attached_to_the_changes_it_carried(monkeypatch):
    """`git_ops` computed the SHA, returned it in a response body and persisted
    nothing, so proposal -> commit -> PR could never be resolved in either direction."""
    from src.services.advisor import tools

    rows = [
        {"projectId": "p1", "proposalId": "a", "decision": "applied", "path": "x.py"},
        {"projectId": "p1", "proposalId": "b", "decision": "discarded", "path": "y.py"},
        {"projectId": "p1", "proposalId": "c", "decision": "applied", "path": "z.py",
         "commitSha": "older"},
    ]
    updates: list = []
    import src.database.dynamo_client as db
    monkeypatch.setattr(db, "query_items", lambda *a, **k: rows)
    monkeypatch.setattr(db, "update_item",
                        lambda table, key, patch: updates.append((key, patch)))

    attached = tools.attach_commit("p1", commit_sha="deadbeef")

    assert attached == 1, "only the applied, unattached change should be taken"
    assert updates[0][0]["proposalId"] == "a"
    assert updates[0][1]["commitSha"] == "deadbeef"


def test_attaching_a_commit_never_raises(monkeypatch):
    """A bookkeeping write must not be able to fail a commit the operator already made."""
    from src.services.advisor import tools
    import src.database.dynamo_client as db

    def boom(*a, **k):
        raise RuntimeError("dynamo is down")

    monkeypatch.setattr(db, "query_items", boom)
    assert tools.attach_commit("p1", commit_sha="x") == 0

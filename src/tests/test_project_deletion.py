"""Deleting a project — everything it owns, and nothing it does not.

The guards outnumber the behaviour tests. This is irreversible: the S3 buckets have no
versioning (verified — there is no `aws_s3_bucket_versioning` in the infra), so there
are no delete markers and no restore. The accidents worth engineering against are not
"it failed" but "it quietly took something else too".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.routers.auth import get_current_user
from src.services import project_deletion as pd
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)
BASE = "/api/projects"

OWNER = {"userId": "u-own", "username": "owner", "role": "user_dev",
         "permissions": ROLE_PERMISSIONS["user_dev"]}
OTHER = {"userId": "u-other", "username": "other", "role": "user_dev",
         "permissions": ROLE_PERMISSIONS["user_dev"]}
ADMIN = {"userId": "u-admin", "username": "admin", "role": "admin",
         "permissions": ROLE_PERMISSIONS["admin"]}


@pytest.fixture(autouse=True)
def _auth():
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: OWNER
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


def _as(user):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture
def two_projects(fake_dynamo, fake_s3, monkeypatch, tmp_path):
    """Two projects with rows in every registry table, so "the other one survived" is
    a real assertion rather than a hopeful one."""
    from src.graph import project_purge
    from src.routers import git_ops

    for pid, owner in (("p1", "u-own"), ("p2", "u-own")):
        fake_dynamo.put_item("projects", {"projectId": pid, "userId": owner,
                                          "name": f"Project {pid}", "status": "analyzed"})
        fake_dynamo.put_item("connectors", {"connectorId": f"c-{pid}", "projectId": pid})
        fake_dynamo.put_item("test-results", {"testRunId": f"r-{pid}", "projectId": pid,
                                              "type": "qatest", "status": "passed"})
        fake_dynamo.put_item("ai-traces", {"projectId": pid, "sortKey": "t1",
                                           "traceId": f"tr-{pid}"})
        fake_dynamo.put_item("ai-spans", {"traceId": f"tr-{pid}", "spanSortKey": "s1"})
        fake_dynamo.put_item("services", {"projectId": pid, "serviceId": f"s-{pid}"})
        fake_dynamo.put_item("sops", {"sopId": f"sop-{pid}", "projectId": pid})
        fake_s3.setdefault(f"test-artifacts/{pid}/run/report.json", b"{}")
        fake_s3.setdefault(f"analysis/{pid}/knowledge_graph.json", b"{}")

    # The runner sentinels, which belong to no project.
    fake_dynamo.put_item("test-results", {"testRunId": "runner:laptop",
                                          "projectId": "_runners", "type": "qa-runner"})
    fake_s3.setdefault("test-artifacts/_runners/laptop/logs/x.log", b"log")

    # A migration child of p1 — kept, not cascaded.
    fake_dynamo.put_item("projects", {"projectId": "p1-child", "userId": "u-own",
                                      "name": "P1 to Airflow", "migratedFrom": "p1"})

    monkeypatch.setattr(project_purge, "purge_project",
                        lambda pid: {"ok": True, "results": [], "totalDeleted": 0})
    monkeypatch.setattr(project_purge, "preflight", lambda pid="": [])
    monkeypatch.setattr(project_purge, "inventory",
                        lambda pid: {"engines": {}, "nodes": 0})
    monkeypatch.setattr(project_purge, "drain_outbox_for", lambda pid, limit=500: 0)
    monkeypatch.setattr(git_ops, "_clone_path", lambda pid: tmp_path / pid)
    return fake_dynamo, fake_s3


def _rows(fake_dynamo, table, pid):
    return [r for r in fake_dynamo.tables.get(table, []) if r.get("projectId") == pid]


# ── Ownership — the hole this closes ─────────────────────────────────────────

def test_another_users_project_is_404_not_403(two_projects):
    """403 confirms the id is real to somebody who has no business knowing that. A
    non-existent id and someone else's must be indistinguishable from outside."""
    fake_dynamo, _ = two_projects
    _as(OTHER)
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.status_code == 404
    assert _rows(fake_dynamo, "projects", "p1"), "it was deleted anyway"


def test_an_admin_may_delete_someone_elses_project(two_projects):
    _as(ADMIN)
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_a_non_owner_cannot_even_preview(two_projects):
    _as(OTHER)
    assert client.get(f"{BASE}/p1/deletion-preview").status_code == 404


# ── Confirmation ─────────────────────────────────────────────────────────────

def test_the_wrong_name_is_refused_and_nothing_is_touched(two_projects):
    fake_dynamo, fake_s3 = two_projects
    before = len(fake_dynamo.tables["projects"])
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "wrong"})
    assert r.status_code == 400
    assert "Project p1" in r.json()["detail"], "the expected phrase is not echoed"
    assert len(fake_dynamo.tables["projects"]) == before


def test_another_projects_name_does_not_confirm_this_one(two_projects):
    """The realistic accident is deleting the wrong project, which is exactly what
    typing a constant word like DELETE would not prevent."""
    fake_dynamo, _ = two_projects
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p2"})
    assert r.status_code == 400
    assert _rows(fake_dynamo, "projects", "p1")


def test_no_body_at_all_is_refused(two_projects):
    assert client.request("DELETE", f"{BASE}/p1").status_code == 400


def test_a_dry_run_changes_nothing(two_projects):
    fake_dynamo, fake_s3 = two_projects
    snapshot = {t: len(rows) for t, rows in fake_dynamo.tables.items()}
    keys = set(fake_s3)

    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "x", "dryRun": True})
    assert r.status_code == 200 and r.json()["dryRun"] is True
    assert {t: len(rows) for t, rows in fake_dynamo.tables.items()} == snapshot
    assert set(fake_s3) == keys


# ── Blockers ─────────────────────────────────────────────────────────────────

def test_a_pending_outbox_blocks_the_delete(two_projects, monkeypatch):
    from src.graph import project_purge
    monkeypatch.setattr(project_purge, "preflight",
                        lambda pid="": ["memgraph has 3 write(s) pending replay"])
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.status_code == 409 and "pending replay" in r.json()["detail"]


def test_a_live_qa_run_blocks_the_delete(two_projects, monkeypatch):
    """It writes results back when it finishes, resurrecting the project."""
    from src.qatest import queue as qa_queue
    monkeypatch.setattr(qa_queue, "list_for_project",
                        lambda pid, limit=200: [{"testRunId": "run-9",
                                                 "status": "running"}])
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.status_code == 409 and "run-9" in r.json()["detail"]


def test_a_project_mid_analysis_blocks_the_delete(two_projects):
    fake_dynamo, _ = two_projects
    for row in fake_dynamo.tables["projects"]:
        if row["projectId"] == "p1":
            row["status"] = "analyzing"
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.status_code == 409 and "analyzing" in r.json()["detail"]


# ── Scope — the heart of it ──────────────────────────────────────────────────

def test_the_other_project_survives_completely(two_projects):
    fake_dynamo, fake_s3 = two_projects
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.json()["ok"] is True

    for table in ("projects", "connectors", "test-results", "ai-traces",
                  "services", "sops"):
        assert _rows(fake_dynamo, table, "p2"), f"p2 lost its {table} rows"
        assert not _rows(fake_dynamo, table, "p1"), f"p1 kept its {table} rows"

    assert any(k.startswith("analysis/p2/") for k in fake_s3)
    assert not any(k.startswith("analysis/p1/") for k in fake_s3)


def test_cascaded_spans_follow_their_traces(two_projects):
    fake_dynamo, _ = two_projects
    client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    spans = fake_dynamo.tables.get("ai-spans", [])
    assert not [s for s in spans if s["traceId"] == "tr-p1"]
    assert [s for s in spans if s["traceId"] == "tr-p2"], "p2's spans went too"


def test_runner_sentinels_survive(two_projects):
    """`projectId == "_runners"` rows are runner liveness, not a project's."""
    fake_dynamo, fake_s3 = two_projects
    client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert [r for r in fake_dynamo.tables["test-results"]
            if r.get("projectId") == "_runners"]
    assert any(k.startswith("test-artifacts/_runners/") for k in fake_s3)


def test_a_migration_child_survives_and_is_named_in_the_preview(two_projects):
    fake_dynamo, _ = two_projects
    preview = client.get(f"{BASE}/p1/deletion-preview").json()
    assert any("P1 to Airflow" in (n["what"] or "") for n in preview["excluded"])

    client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert [r for r in fake_dynamo.tables["projects"] if r["projectId"] == "p1-child"]


def test_the_changelog_is_kept(two_projects, fake_dynamo):
    """It is where the record of this deletion lives — it has to outlive it."""
    fake_dynamo.put_item("ontology-changelog", {"changeId": "c1", "projectId": "p1"})
    client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert [r for r in fake_dynamo.tables["ontology-changelog"]
            if r["changeId"] == "c1"]


def test_a_blank_or_slashed_project_id_is_refused():
    assert "blank" in pd._reject("")
    assert "'/'" in pd._reject("a/b")
    assert "sentinel" in pd._reject("_runners")


# ── Ordering and partial failure ─────────────────────────────────────────────

def test_the_projects_row_is_deleted_last(two_projects):
    """A crash then leaves a project that is still listed and still re-deletable."""
    tables = [s.table for s in pd.SCOPES if not s.keep]
    assert tables[-1] == "projects"
    assert tables.index("ai-spans") < tables.index("ai-traces")


def test_a_graph_failure_leaves_the_project_row_alone(two_projects, monkeypatch):
    """The row is the handle to what is left. Destroying it is how a half-deleted
    project becomes invisible."""
    fake_dynamo, fake_s3 = two_projects
    from src.graph import project_purge
    monkeypatch.setattr(project_purge, "purge_project", lambda pid: {
        "ok": False, "results": [{"backend": "neo4j", "ok": False, "error": "down"}]})

    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["retryable"] is True

    row = [p for p in fake_dynamo.tables["projects"] if p["projectId"] == "p1"][0]
    assert row["status"] == "deletion_failed"
    # And nothing downstream ran.
    assert any(k.startswith("analysis/p1/") for k in fake_s3)


def test_a_failed_delete_can_be_re_run(two_projects, monkeypatch):
    fake_dynamo, _ = two_projects
    from src.graph import project_purge
    monkeypatch.setattr(project_purge, "purge_project",
                        lambda pid: {"ok": False, "results": []})
    client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})

    monkeypatch.setattr(project_purge, "purge_project",
                        lambda pid: {"ok": True, "results": [], "totalDeleted": 0})
    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.json()["ok"] is True
    assert not _rows(fake_dynamo, "projects", "p1")


# ── Audit ────────────────────────────────────────────────────────────────────

def test_the_intent_is_recorded_before_anything_is_destroyed(two_projects):
    fake_dynamo, _ = two_projects
    client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})

    rows = fake_dynamo.tables.get("ontology-changelog", [])
    kinds = [r.get("changeType") for r in rows]
    assert "DELETE_PROJECT_STARTED" in kinds
    assert "DELETE_PROJECT" in kinds
    assert kinds.index("DELETE_PROJECT_STARTED") < kinds.index("DELETE_PROJECT")
    assert all(r.get("entityId") == "project:p1" for r in rows
               if r.get("changeType", "").startswith("DELETE_PROJECT"))


def test_a_failed_delete_is_recorded_too(two_projects, monkeypatch):
    fake_dynamo, _ = two_projects
    from src.graph import project_purge
    monkeypatch.setattr(project_purge, "purge_project",
                        lambda pid: {"ok": False, "results": []})
    client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert any(r.get("changeType") == "DELETE_PROJECT_FAILED"
               for r in fake_dynamo.tables.get("ontology-changelog", []))


# ── The preview ──────────────────────────────────────────────────────────────

def test_the_preview_reports_what_would_go(two_projects):
    body = client.get(f"{BASE}/p1/deletion-preview").json()
    assert body["confirmPhrase"] == "Project p1"
    assert body["canDelete"] is True
    by_table = {t["table"]: t for t in body["dynamodb"]["tables"]}
    assert by_table["connectors"]["rows"] == 1
    assert by_table["ontology-changelog"]["kept"] is True
    assert body["s3"]["objects"] == 2


def test_the_preview_explains_every_exclusion(two_projects):
    body = client.get(f"{BASE}/p1/deletion-preview").json()
    assert body["excluded"]
    assert all(n.get("detail") for n in body["excluded"]), "an exclusion with no reason"
    assert any("Runner working copies" in n["what"] for n in body["excluded"])


def test_the_preview_surfaces_blockers_before_the_user_types(two_projects, monkeypatch):
    from src.graph import project_purge
    monkeypatch.setattr(project_purge, "preflight",
                        lambda pid="": ["neo4j is not reachable"])
    body = client.get(f"{BASE}/p1/deletion-preview").json()
    assert body["canDelete"] is False and body["blockers"]


# ── An unreadable store is not an empty one ──────────────────────────────────

def test_a_store_that_cannot_be_read_is_reported_not_counted_as_zero(two_projects,
                                                                     monkeypatch):
    """Seen for real: a table whose GSI had not been created yet raised, and the
    inventory reported zero rows — which reads as "nothing to delete"."""
    real = pd._rows_for

    def explode(scope, pid, found):
        if scope.table == "token-usage":
            raise pd._LookupFailed("token-usage: the index does not exist")
        return real(scope, pid, found)
    monkeypatch.setattr(pd, "_rows_for", explode)

    body = client.get(f"{BASE}/p1/deletion-preview").json()
    entry = next(t for t in body["dynamodb"]["tables"] if t["table"] == "token-usage")
    assert entry["counted"] is False
    assert entry["rows"] is None, "an unreadable table reported a number"
    assert "index does not exist" in entry["reason"]
    assert body["dynamodb"]["exact"] is False


def test_a_delete_refuses_when_a_store_cannot_be_read(two_projects, monkeypatch):
    """Deleting what we could see and calling it done is how data survives a delete
    that reported success."""
    fake_dynamo, _ = two_projects
    real = pd._rows_for

    def explode(scope, pid, found):
        if scope.table == "connectors":
            raise pd._LookupFailed("connectors: boom")
        return real(scope, pid, found)
    monkeypatch.setattr(pd, "_rows_for", explode)

    r = client.request("DELETE", f"{BASE}/p1", json={"confirm": "Project p1"})
    assert r.json()["ok"] is False
    assert any("could not read connectors" in e for e in r.json()["report"]["errors"])
    assert _rows(fake_dynamo, "projects", "p1"), "the project row was destroyed anyway"

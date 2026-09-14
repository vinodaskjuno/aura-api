"""The QA run queue and the self-hosted runner.

The claim test carries the most weight. A run is executed on a machine that is not the
API, so "claimed" has to be a real mutual exclusion — two runners polling seconds apart
would otherwise both execute the same run, and because the floci emulators publish FIXED
host ports (-p 4566:4566) the second would collide with the first and write a second set
of evidence over the same S3 prefix.

The reaper matters nearly as much: before the queue existed a run in progress was simply
invisible (report.json is written last and its presence IS the done signal), so an
interrupted run vanished. With a queue it does the opposite and sticks at `running`
forever unless something reaps it.
"""
from __future__ import annotations

import itertools

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.qatest import queue
from src.routers.auth import get_current_user
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)

# user_qa, not user_dev: qa_workspace lives on user_qa/admin/super_admin only, and a
# user_dev token gets a 403 from every endpoint here.
QA = {"userId": "u-qa", "username": "qa", "role": "user_qa",
      "permissions": ROLE_PERMISSIONS["user_qa"]}

BASE = "/api/qa"


def _as(user: dict):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.pop(get_current_user, None)


# ── The queue ─────────────────────────────────────────────────────────────────

def test_enqueue_creates_a_queued_run(fake_dynamo):
    row = queue.enqueue("p1", app_url="http://app", ran_by="qa")
    assert row["status"] == queue.QUEUED
    assert row["type"] == queue.KIND
    assert row["testRunId"] and row["projectId"] == "p1"


def test_claim_takes_the_oldest_queued_run(fake_dynamo):
    first = queue.enqueue("p1")
    # createdAt is an ISO string, so lexical order is chronological.
    fake_dynamo.tables[queue.TABLE][0]["createdAt"] = "2020-01-01T00:00:00+00:00"
    second = queue.enqueue("p1")

    claimed = queue.claim("runner-a")
    assert claimed["testRunId"] == first["testRunId"], "must be FIFO"
    assert claimed["status"] == queue.CLAIMED
    assert claimed["runner"] == "runner-a"
    assert second["testRunId"] != first["testRunId"]


def test_a_run_can_only_be_claimed_once(fake_dynamo):
    """The whole point of the conditional write. A read-then-write would let both
    runners through and the same run would execute twice."""
    queue.enqueue("p1")

    assert queue.claim("runner-a") is not None
    assert queue.claim("runner-b") is None, "a second runner must find nothing"


def test_claim_returns_none_when_nothing_is_queued(fake_dynamo):
    assert queue.claim("runner-a") is None


def test_claim_skips_runs_that_are_already_running(fake_dynamo):
    row = queue.enqueue("p1")
    queue.heartbeat(row["testRunId"], "p1", "running")
    assert queue.claim("runner-a") is None


def test_heartbeat_moves_to_running_and_records_phase(fake_dynamo):
    row = queue.enqueue("p1")
    updated = queue.heartbeat(row["testRunId"], "p1", "emulator", "runner-a")
    assert updated["status"] == queue.RUNNING
    assert updated["phase"] == "emulator"
    assert updated["runner"] == "runner-a"


def test_finish_records_the_terminal_state(fake_dynamo):
    row = queue.enqueue("p1")
    done = queue.finish(row["testRunId"], "p1", {
        "status": "failed", "totalPassed": 3, "totalFailed": 1, "totalSkipped": 2,
        "appUrl": "http://app", "reason": "", "completedAt": "2026-09-02T00:00:00+00:00"})
    assert done["status"] == "failed"
    assert (done["totalPassed"], done["totalFailed"], done["totalSkipped"]) == (3, 1, 2)
    assert done["phase"] == "done"


def test_reap_abandons_a_run_whose_runner_went_away(fake_dynamo):
    row = queue.enqueue("p1")
    queue.heartbeat(row["testRunId"], "p1", "running", "runner-a")
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    fake_dynamo.tables[queue.TABLE][0]["updatedAt"] = stale

    assert queue.reap(stale_after_s=900) == 1
    assert fake_dynamo.tables[queue.TABLE][0]["status"] == queue.ABANDONED


def test_reap_leaves_a_live_run_alone(fake_dynamo):
    row = queue.enqueue("p1")
    queue.heartbeat(row["testRunId"], "p1", "running", "runner-a")
    assert queue.reap(stale_after_s=900) == 0
    assert fake_dynamo.tables[queue.TABLE][0]["status"] == queue.RUNNING


def test_reap_leaves_finished_runs_alone(fake_dynamo):
    row = queue.enqueue("p1")
    queue.finish(row["testRunId"], "p1", {"status": "passed"})
    fake_dynamo.tables[queue.TABLE][0]["updatedAt"] = "2020-01-01T00:00:00+00:00"
    assert queue.reap(stale_after_s=1) == 0


def test_list_for_project_is_scoped_and_newest_first(fake_dynamo):
    queue.enqueue("p1")
    fake_dynamo.tables[queue.TABLE][0]["createdAt"] = "2020-01-01T00:00:00+00:00"
    queue.enqueue("p1")
    queue.enqueue("p2")

    rows = queue.list_for_project("p1")
    assert len(rows) == 2, "must not leak another project's runs"
    assert rows[0]["createdAt"] > rows[1]["createdAt"]


# ── Capabilities ──────────────────────────────────────────────────────────────

def test_capabilities_is_false_with_no_local_tools_and_no_runner(fake_dynamo, monkeypatch):
    """The state a deployed environment is in. It must say WHY and how to fix it —
    this string is the whole UX of the disabled button."""
    monkeypatch.setattr("src.qatest.emulators.podman_available", lambda: False)
    monkeypatch.setattr("src.qatest.runner._playwright_available",
                        lambda: (False, "playwright is not installed"))
    _as(QA)

    body = client.get(f"{BASE}/capabilities").json()
    assert body["canRun"] is False
    assert body["runners"] == []
    assert "src.qatest.agent" in body["reason"], "must name the command to fix it"


def test_capabilities_is_true_when_a_runner_is_online(fake_dynamo, monkeypatch):
    """The change that makes the deployed button usable: this process still has neither
    podman nor a browser, and that is now irrelevant."""
    monkeypatch.setattr("src.qatest.emulators.podman_available", lambda: False)
    monkeypatch.setattr("src.qatest.runner._playwright_available",
                        lambda: (False, "playwright is not installed"))
    queue.touch_runner("laptop")
    _as(QA)

    body = client.get(f"{BASE}/capabilities").json()
    assert body["canRun"] is True
    assert body["local"] is False
    assert [r["name"] for r in body["runners"]] == ["laptop"]
    assert body["reason"] == ""


def test_a_stale_runner_does_not_count_as_online(fake_dynamo, monkeypatch):
    """A laptop that closed must not leave the button enabled — otherwise the run is
    queued and nothing ever picks it up."""
    monkeypatch.setattr("src.qatest.emulators.podman_available", lambda: False)
    monkeypatch.setattr("src.qatest.runner._playwright_available",
                        lambda: (False, "no playwright"))
    queue.touch_runner("laptop")
    fake_dynamo.tables[queue.TABLE][0]["updatedAt"] = "2020-01-01T00:00:00+00:00"
    _as(QA)

    body = client.get(f"{BASE}/capabilities").json()
    assert body["canRun"] is False
    assert body["runners"] == []


def test_capabilities_still_true_locally_with_no_runner(fake_dynamo, monkeypatch):
    """The local developer experience must not regress."""
    monkeypatch.setattr("src.qatest.emulators.podman_available", lambda: True)
    monkeypatch.setattr("src.qatest.runner._playwright_available", lambda: (True, ""))
    _as(QA)

    body = client.get(f"{BASE}/capabilities").json()
    assert body["canRun"] is True and body["local"] is True


# ── The enqueue and listing endpoints ─────────────────────────────────────────

def test_post_runs_returns_202_without_executing(fake_dynamo):
    """202, not 200: nothing has run yet. Returning 200 with a report shape would
    invite the UI to treat a queued run as finished.

    An app_url is supplied because without one the run needs a working copy this test
    server does not have, and the endpoint now refuses that case up front."""
    _as(QA)
    res = client.post(f"{BASE}/runs",
                      json={"project_id": "p1", "app_url": "http://localhost:3000"})
    assert res.status_code == 202
    assert res.json()["status"] == queue.QUEUED


def test_a_run_that_cannot_find_code_is_refused_at_the_button(fake_dynamo):
    """It used to be accepted, queued, claimed, and fail on the runner minutes later
    quoting a filesystem path from a machine the reader has never seen. The condition is
    knowable here: no app_url means the runner needs the copy this server ships, and
    this server has none."""
    _as(QA)
    res = client.post(f"{BASE}/runs", json={"project_id": "p1", "app_url": ""})

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "No working copy found" in detail
    # The paths that were checked, so the reader can act rather than guess.
    assert "Looked in:" in detail


def test_an_app_url_makes_the_working_copy_unnecessary(fake_dynamo):
    """Pointing a run at something already serving needs no code on this machine at
    all, so the guard must not fire."""
    _as(QA)
    res = client.post(f"{BASE}/runs",
                      json={"project_id": "p1", "app_url": "https://staging.example.com"})
    assert res.status_code == 202


def test_active_runs_have_their_own_endpoint(fake_dynamo):
    """evidence.list_runs keys off report.json, which is written LAST — so a run in
    progress is invisible to it. That is exactly the gap the queue fills.

    A SEPARATE endpoint, not a field on /results. Widening that response is what broke
    a cached browser bundle with "n.map is not a function": an object arrived where an
    array was expected. An older client never calls this path at all."""
    row = queue.enqueue("p1")
    queue.heartbeat(row["testRunId"], "p1", "running", "laptop")
    _as(QA)

    body = client.get(f"{BASE}/active/p1").json()
    assert [r["runId"] for r in body["active"]] == [row["testRunId"]]
    assert body["active"][0]["phase"] == "running"


def test_results_stays_a_bare_array(fake_dynamo, monkeypatch):
    """The shape is part of the contract. Returning an object here breaks every client
    running older JS — which is not hypothetical, it happened on dev."""
    monkeypatch.setattr("src.qatest.evidence.list_runs", lambda pid: ["r1"])
    monkeypatch.setattr("src.qatest.evidence.read_report",
                        lambda pid, rid: {"runId": rid, "status": "passed"})
    _as(QA)

    body = client.get(f"{BASE}/results/p1").json()
    assert isinstance(body, list), "must be a bare array, not an envelope"
    assert body[0]["runId"] == "r1"


def test_finished_runs_come_from_s3_not_the_queue(fake_dynamo, monkeypatch):
    """A run executed straight from the CLI never touches the queue, so S3 has to stay
    the source of truth for finished runs.

    Note list_runs returns run IDS, which read_report then resolves — not reports."""
    monkeypatch.setattr("src.qatest.evidence.list_runs", lambda pid: ["cli-run"])
    monkeypatch.setattr("src.qatest.evidence.read_report",
                        lambda pid, rid: {"runId": rid, "status": "passed"})
    _as(QA)
    assert client.get(f"{BASE}/results/p1").json()[0]["runId"] == "cli-run"
    assert client.get(f"{BASE}/active/p1").json()["active"] == []


# ── execute() with a supplied plan ────────────────────────────────────────────

def test_execute_with_supplied_cases_does_not_read_the_graph(monkeypatch):
    """The runner cannot reach Neo4j. If execute() still planned from the graph, every
    remote run would fail at the first Cypher query."""
    from src.qatest import service
    from src.qatest.types import Case

    def explode(*a, **k):
        raise AssertionError("fetch_facts must not be called when cases are supplied")

    monkeypatch.setattr("src.qatest.plan.fetch_facts", explode)
    monkeypatch.setattr("src.qatest.plan.build_plan", explode)

    captured = {}

    def fake_run_plan(project_id, run_id, urls, cases, **kwargs):
        captured["cases"] = cases
        captured["clouds"] = [e.cloud for e in (kwargs.get("emulators") or [])]
        from src.qatest.types import Report
        return Report(run_id=run_id, project_id=project_id, app_url="http://app",
                      status="passed")

    monkeypatch.setattr("src.qatest.runner.run_plan", fake_run_plan)
    monkeypatch.setattr("src.qatest.evidence.write_report", lambda r: None)
    monkeypatch.setattr("src.qatest.emulators.podman_available", lambda: True)

    supplied = [Case(case_id="root-001", kind="ui", name="loads",
                     method="GET", path="/")]
    out = service.execute("p1", app_url="http://app", run_id="r1",
                          cases=supplied, clouds=[], write_graph=False)

    assert out["status"] == "passed"
    assert [c.case_id for c in captured["cases"]] == ["root-001"]


def test_supplied_cases_may_arrive_as_dicts(monkeypatch):
    """They cross the wire as JSON, and run_plan reads ATTRIBUTES not keys — so a dict
    would fail with AttributeError deep inside the run."""
    from src.qatest import service

    monkeypatch.setattr("src.qatest.plan.fetch_facts",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no graph")))
    seen = {}

    def fake_run_plan(project_id, run_id, urls, cases, **kwargs):
        seen["ok"] = all(hasattr(c, "case_id") for c in cases)
        from src.qatest.types import Report
        return Report(run_id=run_id, project_id=project_id, app_url="u", status="passed")

    monkeypatch.setattr("src.qatest.runner.run_plan", fake_run_plan)
    monkeypatch.setattr("src.qatest.evidence.write_report", lambda r: None)
    monkeypatch.setattr("src.qatest.emulators.podman_available", lambda: True)

    service.execute("p1", app_url="u", run_id="r1", write_graph=False, clouds=[],
                    cases=[{"case_id": "api-001", "kind": "api", "name": "GET /x",
                            "method": "GET", "path": "/x"}])
    assert seen["ok"], "dicts must be rebuilt into Case objects"


def test_qa_runner_key_label_is_provisionable():
    """The allowlist is what get_or_create_tool_key validates against — without an
    entry the runner cannot be given a key at all, which is how the demo-agent labels
    silently failed until they were added."""
    from src.routers.gateway_keys import _VALID_TOOL_LABELS
    assert "qa-runner" in _VALID_TOOL_LABELS


# ── Runner liveness ───────────────────────────────────────────────────────────

def test_a_runner_is_online_before_it_has_claimed_anything(fake_dynamo):
    """The deadlock this exists to prevent, found on dev with a live runner.

    Liveness used to be derived from claimed runs. A runner that had never claimed
    anything therefore looked offline → canRun false → button disabled → nothing
    queued → it never claimed. Polling has to be the signal, because polling is what
    the runner does before there is any work.
    """
    queue.touch_runner("laptop")
    assert [r["name"] for r in queue.online_runners()] == ["laptop"]


def test_runner_liveness_rows_never_appear_as_runs(fake_dynamo):
    """They share the table, so a different `type` is what keeps them apart. Without
    that they would show up as a project's runs and be reaped as abandoned ones."""
    queue.touch_runner("laptop")
    queue.enqueue("p1")

    assert [r["testRunId"] for r in queue.list_for_project("p1")] != []
    assert all(not r["testRunId"].startswith("runner:")
               for r in queue.list_for_project("p1"))
    assert "runner:laptop" not in [r["testRunId"]
                                   for r in queue.list_for_project(queue.RUNNER_SK)]


def test_reap_ignores_runner_liveness_rows(fake_dynamo):
    queue.touch_runner("laptop")
    fake_dynamo.tables[queue.TABLE][0]["updatedAt"] = "2020-01-01T00:00:00+00:00"
    assert queue.reap(stale_after_s=1) == 0, "a runner row is not an abandoned run"


def test_touching_the_same_runner_twice_keeps_one_row(fake_dynamo):
    """One row per runner, keyed on its name — not one per poll. At a poll every 5s
    that would be 17k rows a day in a table the queue scans."""
    queue.touch_runner("laptop")
    queue.touch_runner("laptop")
    rows = [r for r in fake_dynamo.tables[queue.TABLE]
            if r.get("type") == queue.RUNNER_KIND]
    assert len(rows) == 1


@pytest.fixture
def isolate_aws():
    """Snapshot and restore everything `_apply_credentials` mutates.

    It deliberately reaches past monkeypatch's reach — process env vars, boto3's
    DEFAULT_SESSION, and three module-level caches — because that is the only way to
    make a running process adopt new credentials. Left leaking, later tests resolve
    credentials for real and sit through IMDS retries: the suite went from 10 seconds
    to 17 MINUTES before this fixture existed.
    """
    import os

    import boto3

    from src.config_settings import get_settings
    from src.database import dynamo_client
    from src.storage import s3_client

    settings = get_settings()
    saved = {
        "key": settings.aws_access_key_id,
        "secret": settings.aws_secret_access_key,
        "region": settings.s3_region,
        "session": boto3.DEFAULT_SESSION,
        "client": s3_client._client,
        "acct": s3_client._account_id_cache,
        "resource": dynamo_client._resource,
        "env": {k: os.environ.get(k) for k in
                ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                 "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION")},
    }
    try:
        yield
    finally:
        settings.aws_access_key_id = saved["key"]
        settings.aws_secret_access_key = saved["secret"]
        settings.s3_region = saved["region"]
        boto3.DEFAULT_SESSION = saved["session"]
        s3_client._client = saved["client"]
        s3_client._account_id_cache = saved["acct"]
        dynamo_client._resource = saved["resource"]
        for name, value in saved["env"].items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_scoped_credentials_override_stale_local_config(isolate_aws, monkeypatch):
    """Setting the env alone is not enough, and this failed on dev.

    s3_client._get_client() passes settings.aws_access_key_id explicitly when set,
    which beats the environment — so a machine with stale keys in .env ignored the
    scoped credentials and every upload failed with InvalidAccessKeyId. The cached
    client and account id have to be dropped too.
    """
    from src.config_settings import get_settings
    from src.qatest.agent import _apply_credentials
    from src.storage import s3_client

    settings = get_settings()
    monkeypatch.setattr(settings, "aws_access_key_id", "AKIA-STALE", raising=False)
    monkeypatch.setattr(settings, "aws_secret_access_key", "stale", raising=False)
    monkeypatch.setattr(s3_client, "_client", object(), raising=False)
    monkeypatch.setattr(s3_client, "_account_id_cache", "wrong", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "")

    _apply_credentials({"accessKeyId": "ASIA-FRESH", "secretAccessKey": "s",
                        "sessionToken": "t", "region": "us-east-1"})

    import os
    assert os.environ["AWS_ACCESS_KEY_ID"] == "ASIA-FRESH"
    assert settings.aws_access_key_id == "", "stale explicit keys must be cleared"
    assert s3_client._client is None, "the cached client must be dropped"
    assert s3_client._account_id_cache == ""

    import boto3
    assert boto3.DEFAULT_SESSION is None, (
        "boto3's default session caches resolved credentials, so the next run would "
        "keep writing as the previous run's assumed-role session")


def test_no_credentials_leaves_the_ambient_config_alone(isolate_aws, monkeypatch):
    """Local development: the developer's own credentials must not be wiped."""
    from src.config_settings import get_settings
    from src.qatest.agent import _apply_credentials

    settings = get_settings()
    monkeypatch.setattr(settings, "aws_access_key_id", "AKIA-MINE", raising=False)
    _apply_credentials(None)
    assert settings.aws_access_key_id == "AKIA-MINE"


def test_report_survives_the_round_trip_through_json():
    """A report now crosses a network boundary. graph_writeback reads ATTRIBUTES, so a
    raw dict fails with "'dict' object has no attribute 'project_id'" — after the run
    has succeeded and its evidence is stored, which is the worst time to find out."""
    import json

    from src.qatest.types import Case, EmulatorRecord, Report

    original = Report(
        run_id="r1", project_id="p1", app_url="http://app", status="failed",
        total_passed=3, total_failed=1, total_skipped=2, duration_ms=1234,
        cases=[Case(case_id="api-001", kind="api", name="GET /x", method="GET",
                    path="/x", verifies_label="API", verifies_eid="api:1")],
        emulators=[EmulatorRecord(cloud="aws", image="floci", digest="sha256:x",
                                  port=4566, container="c", started=True)],
        covered=[{"label": "API", "externalId": "api:1"}])

    # Exactly what the runner does: as_dict -> JSON -> HTTP -> dict.
    rebuilt = Report.from_dict(json.loads(json.dumps(original.as_dict())))

    assert rebuilt.project_id == "p1" and rebuilt.run_id == "r1"
    assert rebuilt.status == "failed" and rebuilt.total_failed == 1
    assert rebuilt.cases[0].case_id == "api-001"
    assert rebuilt.cases[0].verifies_eid == "api:1", "the VERIFIES edge needs this"
    assert rebuilt.emulators[0].cloud == "aws"
    assert rebuilt.covered == [{"label": "API", "externalId": "api:1"}]


def test_report_from_dict_tolerates_missing_keys():
    """An older runner should degrade to a partial graph write, not crash the endpoint."""
    from src.qatest.types import Report

    r = Report.from_dict({"runId": "r1", "projectId": "p1"})
    assert r.run_id == "r1" and r.cases == [] and r.total_passed == 0


def test_a_claim_without_credentials_clears_the_previous_run_s(isolate_aws, monkeypatch):
    """Two runs in one agent process. The second must not inherit the first's grant.

    The first run's session policy is scoped to the FIRST run's S3 prefix, so
    inheriting it fails with AccessDenied naming a session id belonging to a different
    run — which is as confusing as it sounds, and is what happened on dev.
    """
    import os

    import boto3

    from src.qatest.agent import _apply_credentials

    _apply_credentials({"accessKeyId": "ASIA-RUN-1", "secretAccessKey": "s",
                        "sessionToken": "t", "region": "us-east-1"})
    assert os.environ["AWS_ACCESS_KEY_ID"] == "ASIA-RUN-1"

    monkeypatch.setattr(boto3, "DEFAULT_SESSION", object(), raising=False)
    _apply_credentials(None)

    assert "AWS_ACCESS_KEY_ID" not in os.environ, "run 1's key must be gone"
    assert "AWS_SESSION_TOKEN" not in os.environ
    assert boto3.DEFAULT_SESSION is None


def test_dynamo_resource_is_reset_too(isolate_aws, monkeypatch):
    """service.execute writes the run's index row from the RUNNER, so dynamo_client's
    cached resource carries the same stale identity as the S3 client."""
    from src.database import dynamo_client
    from src.qatest.agent import _apply_credentials

    monkeypatch.setattr(dynamo_client, "_resource", object(), raising=False)
    _apply_credentials({"accessKeyId": "ASIA-X", "secretAccessKey": "s",
                        "sessionToken": "t", "region": "us-east-1"})
    assert dynamo_client._resource is None


def test_missing_working_copy_message_names_the_right_machine():
    """The reader is in a browser pointed at a deployed environment; the paths belong
    to the RUNNER. "clone it on this machine" reads as the wrong machine entirely."""
    from src.qatest.service import _no_working_copy

    msg = _no_working_copy("p1", ["/Users/dev/aura/data/workspace/p1"])
    assert "machine running the test" in msg
    assert "/Users/dev/aura/data/workspace/p1" in msg, "the path is most of the answer"
    assert "URL of an already-running instance" in msg


def test_missing_working_copy_calls_out_the_container_path_case():
    """Every candidate under /workspace means the project was cloned inside a deployed
    container and its code never existed on the runner — the likeliest cause for
    anything created through the deployed UI, and invisible in a bare 'not found'."""
    from src.qatest.service import _no_working_copy

    msg = _no_working_copy("p1", ["/local/ws/p1", "/workspace/p1"])
    assert "inside a deployed container" in msg


# ── Shipping the working copy ─────────────────────────────────────────────────
# "Clone it automatically" cannot work for a project created by UPLOADING code: its
# Repository nodes carry url=None and its connectors record repoUrl as a container path
# like /workspace/<id>/backend. There is no git remote to clone. Aura ships its own
# copy instead — which also guarantees the code matches the graph the plan came from.

def test_packaging_excludes_dependencies_and_history(tmp_path, monkeypatch):
    """node_modules and .git are the bulk of a checkout and the runner installs its
    own. test1 is 592 KB of source against ~40 MB of node_modules."""
    from src.config_settings import get_settings
    from src.qatest import workspace

    root = tmp_path / "p1"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "main.py").write_text("app = 1")
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.js").write_text("x" * 5000)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("y" * 5000)
    (root / "frontend").mkdir()
    (root / "frontend" / "package.json").write_text("{}")

    monkeypatch.setattr(get_settings(), "aura_workspace", str(tmp_path), raising=False)

    data, digest = workspace.package("p1")
    assert digest and len(digest) == 64

    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = {n.lstrip("./") for n in tar.getnames()}
    assert "backend/main.py" in names
    assert "frontend/package.json" in names
    assert not any("node_modules" in n for n in names), "dependencies must not ship"
    assert not any(".git" in n for n in names), "history must not ship"


def test_packaging_is_reproducible(tmp_path, monkeypatch):
    """The hash is used as the runner's cache key, so identical source must produce an
    identical archive — otherwise every run re-downloads and re-extracts."""
    from src.config_settings import get_settings
    from src.qatest import workspace

    root = tmp_path / "p1"
    root.mkdir()
    (root / "a.py").write_text("x = 1")
    monkeypatch.setattr(get_settings(), "aura_workspace", str(tmp_path), raising=False)

    assert workspace.package("p1")[1] == workspace.package("p1")[1]


def test_packaging_returns_none_without_a_working_copy(tmp_path, monkeypatch):
    from src.config_settings import get_settings
    from src.qatest import workspace

    monkeypatch.setattr(get_settings(), "aura_workspace", str(tmp_path), raising=False)
    assert workspace.package("nope") is None


def test_symlinks_are_dropped(tmp_path, monkeypatch):
    """A symlink could point anywhere on the source filesystem, and a runner extracting
    one would either break or read something it should not."""
    import os

    from src.config_settings import get_settings
    from src.qatest import workspace

    root = tmp_path / "p1"
    root.mkdir()
    (root / "real.py").write_text("x = 1")
    os.symlink("/etc/passwd", root / "escape")
    monkeypatch.setattr(get_settings(), "aura_workspace", str(tmp_path), raising=False)

    import io
    import tarfile
    data, _ = workspace.package("p1")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert not any(m.issym() for m in tar.getmembers())


def test_extraction_refuses_to_escape_the_destination(tmp_path):
    """The archive is ours today, but an extractor that trusts its input is a
    path-traversal bug waiting for the day something else writes it."""
    import io
    import tarfile

    from src.qatest.provision import _safe_extract

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))

    buffer.seek(0)
    with tarfile.open(fileobj=buffer, mode="r") as tar:
        with pytest.raises(ValueError, match="escapes the destination"):
            _safe_extract(tar, tmp_path)
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_dependency_cache_keys_on_the_lockfile_not_the_source(tmp_path):
    """Source changes every commit; dependencies rarely. Keying on the source would
    make every run pay a cold `npm ci` — minutes, every time."""
    from src.qatest.provision import _lock_hash, _NODE_LOCKS

    (tmp_path / "package.json").write_text('{"name":"a"}')
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}')
    before = _lock_hash(tmp_path, _NODE_LOCKS)

    (tmp_path / "src.js").write_text("// a source change")
    assert _lock_hash(tmp_path, _NODE_LOCKS) == before, "source must not bust the cache"

    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3,"x":1}')
    assert _lock_hash(tmp_path, _NODE_LOCKS) != before, "a lock change must bust it"


def test_no_lockfiles_means_no_cache_key(tmp_path):
    from src.qatest.provision import _lock_hash, _PY_LOCKS
    assert _lock_hash(tmp_path, _PY_LOCKS) == ""


def test_appserver_prefers_a_project_venv(tmp_path):
    """provision.py installs the project's requirements into <dir>/.venv. Without this
    the app would start on AURA's interpreter and fail at import on any dependency
    AURA happens not to have."""
    import sys

    from src.qatest.appserver import _interpreter

    assert _interpreter(tmp_path) == sys.executable, "fall back when there is no venv"

    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh")
    assert _interpreter(tmp_path) == str(venv / "python")


def test_prepare_is_a_no_op_without_a_shipped_workspace():
    """An app_url run needs no working copy, so the claim carries none and provisioning
    must do nothing rather than fail."""
    from src.qatest.provision import prepare
    assert prepare("p1", None) == []
    assert prepare("p1", {}) == []


# ── Artifacts ─────────────────────────────────────────────────────────────────

def test_artifacts_fall_back_to_the_run_s_s3_evidence(fake_dynamo, monkeypatch):
    """The Artifacts tab said "No artifacts for this run" for every real test run.

    It reads `run["artifacts"]` — a list the legacy agents write and `src/qatest` does
    not. A qatest run's evidence goes to S3 under {projectId}/{runId}/, so the field
    was empty and the tab reported emptiness while the screenshots sat in the bucket.
    """
    from src.database import dynamo_client as db

    db.put_item("test-results", {"testRunId": "r1", "projectId": "p1",
                                 "type": "qatest", "status": "passed"})
    monkeypatch.setattr("src.storage.s3_client.list_objects", lambda b, prefix: [
        {"key": f"{prefix}report.json", "size": 600},
        {"key": f"{prefix}screenshots/step-0001.png", "size": 137000},
        {"key": f"{prefix}steps.jsonl", "size": 333},
        # The shipped working copy is an implementation detail, not evidence.
        {"key": "p1/_workspace/abc.tar.gz", "size": 21000},
    ])
    monkeypatch.setattr("src.routers.qa.presigned_url",
                        lambda b, k, expires=0: f"https://signed/{k}")
    _as(QA)

    body = client.get(f"{BASE}/runs/r1/artifacts").json()
    names = [a["filename"] for a in body]
    assert "step-0001.png" in names, "the screenshot must be listed"
    assert "report.json" in names
    assert not any("tar.gz" in n for n in names), "the shipped code is not evidence"


def test_artifacts_prefer_an_explicit_list_when_present(fake_dynamo, monkeypatch):
    """The legacy agents do record `artifacts`. Those must still win, so the fallback
    cannot change behaviour for runs that already worked."""
    from src.database import dynamo_client as db

    db.put_item("test-results", {"testRunId": "r2", "projectId": "p1",
                                 "artifacts": ["s3://bucket/p1/r2/legacy.png"]})
    monkeypatch.setattr("src.storage.s3_client.list_objects",
                        lambda b, prefix: [{"key": "p1/r2/other.png", "size": 1}])
    monkeypatch.setattr("src.routers.qa.presigned_url",
                        lambda b, k, expires=0: f"https://signed/{k}")
    _as(QA)

    names = [a["filename"] for a in client.get(f"{BASE}/runs/r2/artifacts").json()]
    assert names == ["legacy.png"]


def test_heartbeat_counts_reach_the_active_list(fake_dynamo):
    """Live counts are the difference between "running" and "4 of 12" — a twelve-case
    run otherwise looks identical to one that has hung."""
    row = queue.enqueue("p1")
    queue.heartbeat(row["testRunId"], "p1", "running", "laptop",
                    {"totalPassed": 4, "totalFailed": 1, "totalSkipped": 2,
                     "totalCases": 12})
    _as(QA)

    active = client.get(f"{BASE}/active/p1").json()["active"][0]
    assert (active["totalPassed"], active["totalFailed"], active["totalSkipped"],
            active["totalCases"]) == (4, 1, 2, 12)


def test_finish_overwrites_live_counts_with_the_report_s(fake_dynamo):
    """The heartbeat's tally is provisional; the report is authoritative."""
    row = queue.enqueue("p1")
    queue.heartbeat(row["testRunId"], "p1", "running", "laptop",
                    {"totalPassed": 4, "totalCases": 12})
    queue.finish(row["testRunId"], "p1",
                 {"status": "passed", "totalPassed": 5, "totalFailed": 0,
                  "totalSkipped": 7})
    stored = fake_dynamo.tables[queue.TABLE][0]
    assert stored["totalPassed"] == 5 and stored["totalSkipped"] == 7


def test_the_heartbeat_actually_accepts_a_body():
    """Declaring the body as `HeartbeatRequest | None = None` looked optional and was
    not: FastAPI omitted the request body from the route, so the endpoint answered 200
    and discarded every count. Zero counts are indistinguishable from a run that has
    not started, so nothing surfaced. Assert on the schema, not the status code."""
    from src.main import app

    post = app.openapi()["paths"]["/api/qa/runner/{run_id}/heartbeat"]["post"]
    assert post.get("requestBody"), "the counts body must be registered on the route"


def test_a_countless_heartbeat_does_not_zero_real_counts(fake_dynamo):
    """The body model defaults all four counts to 0, so a heartbeat carrying none
    would overwrite good ones — a progress bar that advances and then snaps back to
    "0 of 0" mid-run, which reads as the run restarting."""
    row = queue.enqueue("p1")
    queue.heartbeat(row["testRunId"], "p1", "step", "laptop",
                    {"totalPassed": 4, "totalFailed": 0, "totalSkipped": 1,
                     "totalCases": 12})
    queue.heartbeat(row["testRunId"], "p1", "evidence", "laptop",
                    {"totalPassed": 0, "totalFailed": 0, "totalSkipped": 0,
                     "totalCases": 0})

    stored = fake_dynamo.tables[queue.TABLE][0]
    assert stored["totalPassed"] == 4, "real counts must survive a countless heartbeat"
    assert stored["totalCases"] == 12
    assert stored["phase"] == "evidence", "the phase must still advance"


# ── The generation flow is gone ───────────────────────────────────────────────
# Removed, not hidden. It wrote test-case FILES with an LLM and then ran pytest in the
# container — a second, unrelated test suite that shared the word "execute" with the
# run button and was routinely mistaken for it.

@pytest.mark.parametrize("method,path", [
    ("post", "/generate"),
    ("post", "/run"),
])
def test_generation_endpoints_are_gone(method, path):
    from src.main import app

    routes = {(r.path, m) for r in app.routes
              for m in getattr(r, "methods", set()) or set()}
    assert (f"/api/qa{path}", method.upper()) not in routes


def test_the_generation_websocket_is_gone():
    from src.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/qa/ws/generate" not in paths


def test_the_local_run_websocket_survives():
    """Removing the generation flow must not take the local synchronous run with it —
    it is still the right experience when the backend and the runner are one machine."""
    from src.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/qa/ws/local-run" in paths


def test_s3_key_helper_survived_the_removal():
    """It lived inside the removed block but the artifacts endpoint still uses it —
    deleting it broke test collection with an ImportError."""
    from src.routers.qa import _s3_key

    assert _s3_key("s3://aura-123-test-artifacts/p1/r1/a.png") == "p1/r1/a.png"
    assert _s3_key("p1/r1/a.png") == "p1/r1/a.png"


# ── Progress, the plan size, and the old-agent cliff ─────────────────────────

def test_progress_reports_unknown_rather_than_zero_when_the_plan_size_is_not_known(
        fake_dynamo):
    """A queued run has not been claimed, so nothing knows how many cases it has. 0%
    would say something false about a run that has not started."""
    row = queue.enqueue("p1", "", "qa")
    out = queue.progress(row["testRunId"], "p1")
    assert out["totalCases"] == 0
    assert out["pct"] is None, "an unknown plan size was reported as 0%"


def test_progress_is_a_getitem_not_a_scan(fake_dynamo):
    """This is what a 2-second poll hits. `list_for_project` scans the table, and a
    polled endpoint backed by a scan is how a table gets hot."""
    row = queue.enqueue("p1", "", "qa")
    before = fake_dynamo.scan_calls
    queue.progress(row["testRunId"], "p1")
    assert fake_dynamo.scan_calls == before


def test_progress_never_exceeds_one_hundred_percent(fake_dynamo):
    """A heartbeat racing the final report can report more done than planned, and
    113% destroys trust in every other number on the panel."""
    row = queue.enqueue("p1", "", "qa")
    queue.heartbeat(row["testRunId"], "p1", "step", "laptop",
                    {"totalCases": 3, "totalPassed": 5})
    out = queue.progress(row["testRunId"], "p1")
    assert out["pct"] == 100 and out["done"] == 3


def test_finishing_keeps_the_plan_size(fake_dynamo):
    """`/runs/{id}/progress` reads this row directly and would otherwise report a
    completed run as 0 of 0."""
    row = queue.enqueue("p1", "", "qa")
    queue.heartbeat(row["testRunId"], "p1", "step", "laptop", {"totalCases": 4})
    queue.finish(row["testRunId"], "p1",
                 {"status": "passed", "totalPassed": 4, "planTotal": 4})
    assert queue.progress(row["testRunId"], "p1")["totalCases"] == 4


def test_a_heartbeat_carries_the_running_emulators_through_to_active(fake_dynamo):
    row = queue.enqueue("p1", "", "qa")
    queue.heartbeat(row["testRunId"], "p1", "emulator", "laptop", {
        "totalCases": 2,
        "emulators": [{"cloud": "aws", "port": 4566, "container": "aura-qa-aws-x",
                       "image": "floci", "started": True, "error": ""}]})
    out = queue.progress(row["testRunId"], "p1")
    assert out["emulators"][0]["cloud"] == "aws"
    assert out["emulators"][0]["port"] == 4566


def test_reaping_marks_the_emulators_stale(fake_dynamo):
    """Otherwise an abandoned run's panel keeps showing containers that are gone —
    a phantom that reads as "still working"."""
    row = queue.enqueue("p1", "", "qa")
    queue.heartbeat(row["testRunId"], "p1", "step", "laptop",
                    {"totalCases": 1,
                     "emulators": [{"cloud": "aws", "started": True}]})
    old = (datetime.now(timezone.utc) - timedelta(seconds=5000)).isoformat()
    for r in fake_dynamo.tables[queue.TABLE]:
        if r.get("testRunId") == row["testRunId"]:
            r["updatedAt"] = old
    assert queue.reap(stale_after_s=900) == 1
    assert queue.progress(row["testRunId"], "p1")["emulatorsStale"] is True


def test_an_old_agent_never_receives_a_case_field_it_cannot_construct():
    """`service.execute` does `Case(**c)`, so a new field kills every agent already
    running — uncaught, inside its poll loop. This is the one real cliff."""
    from src.routers.qa import _LEGACY_CASE_FIELDS, _cases_for_protocol
    from src.qatest.types import Case as C

    case = C(case_id="c1", kind="api", name="GET /x", skip_reason="because")
    legacy = _cases_for_protocol([case], 1)[0]
    assert set(legacy) == set(_LEGACY_CASE_FIELDS)
    assert "skip_reason" not in legacy
    # And the constructor an old agent uses must accept exactly that dict.
    assert C(**legacy).case_id == "c1"

    current = _cases_for_protocol([case], 2)[0]
    assert current["skip_reason"] == "because"


def test_from_wire_drops_an_unknown_key_instead_of_raising():
    """The other direction — a new agent against an older server."""
    from src.qatest.types import Case as C
    assert C.from_wire({"case_id": "c", "kind": "api", "name": "n",
                        "invented_later": True}).case_id == "c"


# ── A busy table must not starve the queue ──────────────────────────────────
#
# What happened on a real dev machine: `test-results` accumulated 262 rows — 196 of
# them from the old execution agent — and `claim` scanned with an UNFILTERED
# limit=200. The queued run fell outside that window, so the runner polled every five
# seconds against a queue that looked empty, `/active` never listed the run, and the
# launcher sat there forever. Nothing errored anywhere.

def _clutter(fake_dynamo, n: int = 260) -> None:
    """Rows from other producers, which is what this table really looks like."""
    for i in range(n):
        fake_dynamo.put_item(queue.TABLE, {
            "testRunId": f"old-{i:04d}", "projectId": "someone-else",
            "type": "execution", "status": "completed",
            "createdAt": f"2026-01-01T00:{i % 60:02d}:00+00:00"})


def test_a_queued_run_is_claimable_on_a_table_full_of_other_rows(fake_dynamo):
    _clutter(fake_dynamo)
    row = queue.enqueue("p1", "", "qa")

    claimed = queue.claim("laptop")
    assert claimed is not None, "the queue went blind once the table grew"
    assert claimed["testRunId"] == row["testRunId"]


def test_an_in_flight_run_is_listed_on_a_table_full_of_other_rows(fake_dynamo):
    """The same starvation hit `/active`, so the launcher polled for a run the API
    insisted did not exist and fell through to a 404 on its report."""
    _clutter(fake_dynamo)
    row = queue.enqueue("p1", "", "qa")

    live = [r["testRunId"] for r in queue.list_for_project("p1")]
    assert row["testRunId"] in live


def test_the_reaper_still_sees_runs_on_a_busy_table(fake_dynamo):
    _clutter(fake_dynamo)
    row = queue.enqueue("p1", "", "qa")
    queue.claim("laptop")
    old = (datetime.now(timezone.utc) - timedelta(seconds=5000)).isoformat()
    for r in fake_dynamo.tables[queue.TABLE]:
        if r.get("testRunId") == row["testRunId"]:
            r["updatedAt"] = old

    assert queue.reap(stale_after_s=900) == 1


def test_a_runner_is_still_found_on_a_busy_table(fake_dynamo):
    _clutter(fake_dynamo)
    queue.touch_runner("laptop")
    # Drop the index so the scan fallback is what answers — the path that starves.
    fake_dynamo.tables[queue.TABLE] = [
        r for r in fake_dynamo.tables[queue.TABLE]
        if r.get("testRunId") != queue.RUNNER_INDEX_ID]

    assert [r["name"] for r in queue.online_runners()] == ["laptop"]


# ── Queue scoping: local development must not claim a deployed run ────────────
#
# The queue is a DynamoDB table addressed by NAME, so every Aura process pointed at one
# AWS account shares it. A developer running the API on their laptop therefore shared
# dev's queue: a run started from the dev UI was claimed by localhost, executed against
# a laptop filesystem, and wrote its report into dev's S3 — so the browser showed a
# macOS path it could not possibly explain.

def test_a_run_is_stamped_with_the_scope_that_queued_it(fake_dynamo):
    row = queue.enqueue("p1")
    assert row["scope"] == queue._scope()


def test_another_environment_cannot_claim_this_environments_run(fake_dynamo, monkeypatch):
    """The whole point. A localhost backend polling the shared table must leave a
    deployed environment's work alone."""
    # Queued by a deployed environment...
    monkeypatch.setattr(queue, "_scope", lambda: "ecs/prod")
    queue.enqueue("p1", run_id="dev-run")

    # ...and polled for by a laptop sharing the same table.
    monkeypatch.setattr(queue, "_scope", lambda: "local/development")
    assert queue.claim("laptop") is None, "a foreign scope claimed the run"

    # Still there, untouched, for its own environment to pick up.
    monkeypatch.setattr(queue, "_scope", lambda: "ecs/prod")
    won = queue.claim("dev-runner")
    assert won and won["testRunId"] == "dev-run"


def test_a_run_with_no_scope_is_never_claimed(fake_dynamo):
    """Rows predating scoping. Matching `scope.not_exists()` would reinstate exactly the
    cross-environment claim this prevents, so they are left for the reaper instead."""
    from src.database import dynamo_client as db

    db.put_item(queue.TABLE, {"testRunId": "legacy", "projectId": "p1",
                              "type": queue.KIND, "status": queue.QUEUED,
                              "createdAt": "2026-01-01T00:00:00+00:00"})
    assert queue.claim("laptop") is None


# ── A refused poll is anonymous, but it is not invisible ─────────────────────

def test_a_rejected_poll_is_counted_so_the_ui_can_explain_it(fake_dynamo, monkeypatch):
    """"No runner connected" and "a runner is connected and its key is refused" looked
    identical for four days. A rejected poll carries no identity, so the only thing that
    can be recorded is that one happened."""
    from fastapi import HTTPException
    from src.routers import qa as qa_router

    monkeypatch.setattr(qa_router, "_runner_identity",
                        qa_router._runner_identity)  # the real one
    monkeypatch.setattr("src.services.gateway_service.resolve_credential",
                        lambda _t: (_ for _ in ()).throw(HTTPException(401, "revoked")))
    monkeypatch.setattr("src.services.gateway_service.extract_credential",
                        lambda _r: "gw-dead-key")

    res = client.get(f"{BASE}/runner/next")
    assert res.status_code == 401

    seen = queue.unauthorized_recently()
    assert seen, "a refused poll left no trace"
    # Enough to tell two runners apart, never enough to rebuild the credential.
    assert seen["hint"] == "-key"


def test_an_old_rejection_stops_being_reported(fake_dynamo):
    """The banner explains an absent runner NOW. A rejection from last week would leave
    it accusing a key that has since been fixed."""
    from src.database import dynamo_client as db

    db.put_item(queue.TABLE, {"testRunId": queue.UNAUTHORIZED_ID,
                              "projectId": queue.RUNNER_SK,
                              "type": queue.RUNNER_KIND, "runner": queue.UNAUTHORIZED_ID,
                              "lastUnauthorizedAt": "2026-01-01T00:00:00+00:00"})
    assert queue.unauthorized_recently() == {}


# ── Cost attribution ─────────────────────────────────────────────────────────
#
# A QA run spends nothing today: the plan comes from the knowledge graph by design and
# execution is Playwright plus HTTP. These pin the PIPE — that a run's spend would be
# attributable the moment something in a run calls a model — and that the honest answer
# meanwhile is "no calls", not "$0.00".

_usage_seq = itertools.count()


def _usage_row(run_id: str, project_id: str = "p1", **over):
    """One usage row. The sort key must be unique — it is `<timestamp>#<id>` in
    production, and reusing it here would silently overwrite instead of appending."""
    from src.database import dynamo_client as db
    row = {"userId": "u1",
           "sortKey": f"2026-09-14T10:00:0{next(_usage_seq)}#{run_id}",
           "projectId": project_id, "testRunId": run_id,
           "model": "claude-sonnet-5", "inputTokens": 100, "outputTokens": 50,
           "cacheReadTokens": 0, "cacheCreationTokens": 0,
           "cost": "0.001050", "timestamp": "2026-09-14T10:00:00", "source": "gateway"}
    row.update(over)
    db.put_item("token-usage", row)
    return row


def test_a_run_that_called_no_model_says_so_rather_than_zero(fake_dynamo):
    _as(QA)
    res = client.get(f"{BASE}/runs/run-x/cost", params={"projectId": "p1"})
    assert res.status_code == 200
    body = res.json()
    assert body["calls"] == 0 and body["totalTokens"] == 0
    assert body["byModel"] == []


def test_run_cost_sums_only_that_runs_rows(fake_dynamo):
    """Attribution is the point. Another run's spend in the same project must not be
    folded in."""
    _usage_row("run-a")
    _usage_row("run-a", inputTokens=200, outputTokens=100, cost="0.002100")
    _usage_row("run-b", inputTokens=999, outputTokens=999, cost="9.999999")

    _as(QA)
    body = client.get(f"{BASE}/runs/run-a/cost", params={"projectId": "p1"}).json()

    assert body["calls"] == 2
    assert body["inputTokens"] == 300 and body["outputTokens"] == 150
    assert body["costUsd"] == pytest.approx(0.00315)
    assert [m["model"] for m in body["byModel"]] == ["claude-sonnet-5"]


def test_recorded_cost_is_used_rather_than_recomputed(fake_dynamo):
    """Prices change. Re-pricing an old row would quietly restate history."""
    _usage_row("run-c", cost="0.777000")
    _as(QA)
    body = client.get(f"{BASE}/runs/run-c/cost", params={"projectId": "p1"}).json()
    assert body["costUsd"] == pytest.approx(0.777)

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
    invite the UI to treat a queued run as finished."""
    _as(QA)
    res = client.post(f"{BASE}/runs", json={"project_id": "p1", "app_url": ""})
    assert res.status_code == 202
    assert res.json()["status"] == queue.QUEUED


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

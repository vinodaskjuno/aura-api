"""What the server knows about a developer's machine, and how it says so.

The API runs on Fargate and can never see podman, so every claim the Floci panel makes
about someone's laptop is second-hand. These tests pin the two properties that keeps
honest: a report that has gone quiet is marked STALE rather than presented as current,
and a command is dispatched exactly once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.qatest import queue
from src.routers import qa as qa_router
from src.routers.auth import get_current_user
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)

QA = {"userId": "u-qa", "username": "qa", "role": "user_qa",
      "permissions": ROLE_PERMISSIONS["user_qa"]}
BASE = "/api/qa"
RUNNER = "qa/qa-runner"
#: What the API derives from the gateway key and the agent's own headers.
IDENTITY = {"owner": "qa", "ownerId": "u1", "machine": "my-laptop"}


@pytest.fixture(autouse=True)
def _auth_and_runner(monkeypatch):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: QA
    # The runner authenticates with a gateway key rather than a JWT, so the identity
    # helper is what has to be stubbed — it is called directly, not via Depends.
    # It returns the label AND who owns the machine, both server-derived.
    monkeypatch.setattr(qa_router, "_runner_identity",
                        lambda _request: (RUNNER, dict(IDENTITY)))
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


def _state(**over):
    body = {"protocol": 2, "podman": True, "browser": True, "busyRunId": "",
            "containers": [{"id": "9f3a", "name": "aura-qa-aws-run1",
                            "image": "docker.io/floci/floci:latest",
                            "status": "Up 4 minutes", "ports": "4566->4566",
                            "cloud": "aws"}]}
    body.update(over)
    return body


# ── Reporting state ─────────────────────────────────────────────────────────

def test_a_runner_can_report_its_containers_and_read_them_back(fake_dynamo):
    assert client.post(f"{BASE}/runner/state", json=_state()).status_code == 200

    runners = client.get(f"{BASE}/runners").json()["runners"]
    assert len(runners) == 1
    me = runners[0]
    assert me["name"] == RUNNER and me["online"] is True and me["podman"] is True
    assert me["containers"][0]["name"] == "aura-qa-aws-run1"
    assert me["containers"][0]["managed"] is True
    assert me["reportsState"] is True


def test_reporting_state_does_not_destroy_the_liveness_stamp(fake_dynamo):
    """`touch_runner` used to `put_item`, which REPLACES the row — so a poll five
    seconds after a state report erased every container it had just reported."""
    client.post(f"{BASE}/runner/state", json=_state())
    queue.touch_runner(RUNNER)

    row = queue.runner_state(RUNNER)
    assert row["containers"], "the poll wiped the reported state"
    assert row["podman"] is True


def test_the_body_is_actually_received(fake_dynamo):
    """A `Body | None = None` spelling silently removes the body from the route and
    discards everything in it — which already happened once, to the heartbeat."""
    schema = app.openapi()["paths"][f"{BASE}/runner/state"]["post"]
    assert "requestBody" in schema

    client.post(f"{BASE}/runner/state", json=_state(podman=False))
    assert queue.runner_state(RUNNER)["podman"] is False


# ── Staleness ───────────────────────────────────────────────────────────────

def test_a_quiet_runner_is_stale_and_its_containers_are_last_known(fake_dynamo):
    """A sleeping laptop must not leave a panel claiming four emulators are running."""
    client.post(f"{BASE}/runner/state", json=_state())
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    for row in fake_dynamo.tables[queue.TABLE]:
        if row.get("type") == queue.RUNNER_KIND:
            row["updatedAt"] = old
            for key, value in list(row.items()):
                if key.startswith("r_") and isinstance(value, dict):
                    value["updatedAt"] = old

    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["online"] is False and me["stale"] is True
    # Still returned — the panel needs something to grey out, not an empty table.
    assert me["containers"]


def test_two_runners_are_independent(fake_dynamo, monkeypatch):
    client.post(f"{BASE}/runner/state", json=_state())
    monkeypatch.setattr(qa_router, "_runner_identity",
                        lambda _r: ("other/qa-runner",
                                    {"owner": "other", "ownerId": "u2",
                                     "machine": "build-box"}))
    client.post(f"{BASE}/runner/state", json=_state(podman=False, containers=[]))

    names = {r["name"]: r for r in client.get(f"{BASE}/runners").json()["runners"]}
    assert set(names) == {RUNNER, "other/qa-runner"}
    assert names[RUNNER]["podman"] is True
    assert names["other/qa-runner"]["podman"] is False


def test_online_runners_reads_the_index_without_scanning(fake_dynamo):
    """`/capabilities` calls this on every page load. It used to scan 500 rows."""
    client.post(f"{BASE}/runner/state", json=_state())
    before = fake_dynamo.scan_calls
    assert [r["name"] for r in queue.online_runners()] == [RUNNER]
    assert fake_dynamo.scan_calls == before, "online_runners scanned the table"


def test_online_runners_falls_back_to_the_scan_when_the_index_is_missing(fake_dynamo):
    """First deploy, or a lost row. The index is a cache and must never be the reason
    the panel is empty."""
    queue.touch_runner(RUNNER)
    fake_dynamo.tables[queue.TABLE] = [
        r for r in fake_dynamo.tables[queue.TABLE]
        if r.get("testRunId") != queue.RUNNER_INDEX_ID]
    assert [r["name"] for r in queue.online_runners()] == [RUNNER]


# ── Log commands ────────────────────────────────────────────────────────────

def test_a_log_request_is_handed_over_exactly_once(fake_dynamo):
    """Two polls racing must not both run the command."""
    client.post(f"{BASE}/runner/state", json=_state())
    requested = client.post(f"{BASE}/runners/logs",
                            json={"runner": RUNNER,
                                  "container": "aura-qa-aws-run1"}).json()
    assert requested["status"] == "pending"

    first = client.post(f"{BASE}/runner/state", json=_state()).json()
    second = client.post(f"{BASE}/runner/state", json=_state()).json()
    assert len(first["commands"]) == 1
    assert first["commands"][0]["id"] == requested["commandId"]
    assert second["commands"] == []


def test_a_repeat_request_returns_the_one_already_in_flight(fake_dynamo):
    client.post(f"{BASE}/runner/state", json=_state())
    first = client.post(f"{BASE}/runners/logs",
                        json={"runner": RUNNER, "container": "aura-qa-aws-run1"}).json()
    again = client.post(f"{BASE}/runners/logs",
                        json={"runner": RUNNER, "container": "aura-qa-aws-run1"}).json()
    assert again["commandId"] == first["commandId"]
    assert again.get("deduped") is True


def test_logs_are_refused_for_a_container_aura_did_not_start(fake_dynamo):
    """Enforced here as well as on the runner. Without it a buggy or compromised
    server could read arbitrary container output off a developer's laptop."""
    client.post(f"{BASE}/runner/state", json=_state())
    r = client.post(f"{BASE}/runners/logs",
                    json={"runner": RUNNER, "container": "my-employers-database"})
    assert r.status_code == 400
    assert "Aura started" in r.json()["detail"]


def test_a_log_body_is_capped_and_keeps_the_tail(fake_dynamo, fake_s3):
    """The recent lines are the ones being asked about, so the HEAD is what goes."""
    client.post(f"{BASE}/runner/state", json=_state())
    cmd = client.post(f"{BASE}/runners/logs",
                      json={"runner": RUNNER, "container": "aura-qa-aws-run1"}).json()
    client.post(f"{BASE}/runner/state", json=_state())

    huge = ("x" * 1000 + "\n") * 300 + "THE-LAST-LINE\n"
    client.post(f"{BASE}/runner/state", json=_state(
        commandResults=[{"id": cmd["commandId"], "ok": True, "output": huge}]))

    out = client.get(f"{BASE}/runners/logs/{cmd['commandId']}",
                     params={"runner": RUNNER}).json()
    assert out["status"] == "ready"
    assert out["lines"][-1] == "THE-LAST-LINE"
    assert out["truncated"] is True


def test_a_pending_log_request_says_pending_rather_than_failing(fake_dynamo):
    """The runner answers on its next poll, so the first read is always pending. The
    UI has to be able to say "asking…" instead of showing an error."""
    client.post(f"{BASE}/runner/state", json=_state())
    cmd = client.post(f"{BASE}/runners/logs",
                      json={"runner": RUNNER, "container": "aura-qa-aws-run1"}).json()
    out = client.get(f"{BASE}/runners/logs/{cmd['commandId']}",
                     params={"runner": RUNNER}).json()
    assert out["status"] == "pending"


def test_a_failed_command_reports_its_error(fake_dynamo):
    client.post(f"{BASE}/runner/state", json=_state())
    cmd = client.post(f"{BASE}/runners/logs",
                      json={"runner": RUNNER, "container": "aura-qa-aws-run1"}).json()
    client.post(f"{BASE}/runner/state", json=_state(
        commandResults=[{"id": cmd["commandId"], "ok": False,
                         "error": "podman not found on PATH"}]))
    out = client.get(f"{BASE}/runners/logs/{cmd['commandId']}",
                     params={"runner": RUNNER}).json()
    assert out["status"] == "failed" and "podman" in out["error"]


# ── An unmanaged container is never even reported ───────────────────────────

def test_only_auras_own_containers_are_marked_managed(fake_dynamo):
    """A developer's machine runs their employer's containers. The agent filters them
    out; if one arrives anyway it must not be presented as ours."""
    client.post(f"{BASE}/runner/state", json=_state(containers=[
        {"id": "1", "name": "aura-qa-aws-run1", "image": "floci"},
        {"id": "2", "name": "customer-postgres", "image": "postgres"}]))
    containers = client.get(f"{BASE}/runners").json()["runners"][0]["containers"]
    flags = {c["name"]: c["managed"] for c in containers}
    assert flags == {"aura-qa-aws-run1": True, "customer-postgres": False}


# ── Every new read endpoint, called for real ────────────────────────────────
#
# These exist because a signature mismatch shipped: `/coverage` called
# `evidence.list_runs(project_id, limit=10)`, which takes no `limit` and returns run
# IDs rather than dicts. Both mistakes are invisible to a unit test of the module and
# fatal the first time a browser asks. Anything reachable over HTTP gets called over
# HTTP here, even if the assertion is only "it did not 500".

REPORT = {
    "runId": "run-1", "projectId": "p1", "appUrl": "http://x",
    "status": "passed", "startedAt": "2026-09-10T10:00:00+00:00",
    "totalPassed": 1, "totalFailed": 0, "totalSkipped": 1, "totalUnemulated": 0,
    "durationMs": 1200,
    "cases": [
        {"case_id": "root-001", "kind": "ui", "name": "application loads",
         "verifies_label": "", "verifies_eid": "", "method": "GET", "path": "/",
         "source_file": "", "skip_reason": ""},
        {"case_id": "api-001", "kind": "api", "name": "POST /items",
         "verifies_label": "API", "verifies_eid": "a1", "method": "POST",
         "path": "/items", "source_file": "app.py",
         "skip_reason": "POST needs a request body the graph does not describe"},
    ],
    "emulators": [], "covered": [], "exploratory": False,
}


@pytest.fixture
def stored_run(monkeypatch):
    """One finished run in S3, and a graph that knows what the project has."""
    monkeypatch.setattr("src.qatest.evidence.list_runs", lambda pid: ["run-1"])
    monkeypatch.setattr("src.qatest.evidence.read_report", lambda pid, rid: dict(REPORT))
    monkeypatch.setattr("src.qatest.evidence.read_steps", lambda pid, rid: [
        {"index": 1, "action": "application loads", "target": "/", "status": "passed",
         "caseId": "root-001", "screenshotKey": ""},
        {"index": 2, "action": "POST /items", "target": "/items", "status": "skipped",
         "caseId": "api-001", "screenshotKey": ""},
    ])
    monkeypatch.setattr("src.qatest.evidence.screenshot_urls", lambda pid, rid: {})
    monkeypatch.setattr("src.qatest.plan.fetch_facts", lambda pid: {
        "apis": [{"eid": "a1", "method": "POST", "path": "/items"},
                 {"eid": "a2", "method": "GET", "path": "/health"}],
        "services": [], "dependencies": [{"name": "boto3"}]})


def test_project_coverage_endpoint_answers(fake_dynamo, stored_run):
    r = client.get(f"{BASE}/projects/p1/coverage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runId"] == "run-1"
    cov = body["coverage"]
    # One API node verified out of two, and the uncovered one carries its reason.
    assert cov["nodeTotal"] == 2
    assert cov["uncovered"][0]["reason"].startswith("POST needs a request body")


def test_project_coverage_is_not_an_error_when_nothing_has_run(fake_dynamo, monkeypatch):
    monkeypatch.setattr("src.qatest.evidence.list_runs", lambda pid: [])
    r = client.get(f"{BASE}/projects/p1/coverage")
    assert r.status_code == 200
    assert r.json()["coverage"] is None


def test_plan_preview_endpoint_answers(fake_dynamo, stored_run):
    from src.qatest import plan as plan_mod
    plan_mod._preview_cache.clear()

    r = client.get(f"{BASE}/projects/p1/plan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["graphReady"] is True
    assert body["counts"]["api"] == 2
    assert body["totalCases"] == 3          # two APIs plus the application root
    assert body["clouds"] == ["aws"]        # boto3 implies exactly one emulator


def test_plan_preview_says_why_when_the_graph_is_empty(fake_dynamo, monkeypatch):
    """"0 cases" and "never analysed" look identical otherwise, and only one of them
    is something the user can act on."""
    from src.qatest import plan as plan_mod
    plan_mod._preview_cache.clear()
    monkeypatch.setattr("src.qatest.plan.fetch_facts",
                        lambda pid: {"apis": [], "services": [], "dependencies": []})

    body = client.get(f"{BASE}/projects/empty/plan").json()
    assert body["graphReady"] is False
    assert "Analyse it in Dev Workspace" in body["reason"]


def test_get_result_carries_coverage(fake_dynamo, stored_run):
    r = client.get(f"{BASE}/results/p1/run-1")
    assert r.status_code == 200, r.text
    assert r.json()["coverage"]["nodeTotal"] == 2
    assert len(r.json()["steps"]) == 2


def test_run_progress_endpoint_answers(fake_dynamo):
    row = queue.enqueue("p1", "", "qa")
    r = client.get(f"{BASE}/runs/{row['testRunId']}/progress", params={"projectId": "p1"})
    assert r.status_code == 200, r.text
    assert r.json()["pct"] is None


def test_run_progress_404s_for_a_run_that_does_not_exist(fake_dynamo):
    assert client.get(f"{BASE}/runs/nope/progress",
                      params={"projectId": "p1"}).status_code == 404


def test_console_endpoint_answers_even_with_no_log(fake_dynamo, fake_s3):
    r = client.get(f"{BASE}/results/p1/run-1/console")
    assert r.status_code == 200
    assert r.json()["lines"] == []


def test_the_index_carries_everything_the_panel_renders(fake_dynamo):
    """Readers go through the aggregate index, so a field stored only on the
    per-runner row is invisible in the UI even though it was reported correctly.
    That happened to `os` and both version strings."""
    client.post(f"{BASE}/runner/state", json=_state(
        os="darwin/arm64", podmanVersion="5.2.1", browserVersion="131"))

    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["os"] == "darwin/arm64"
    assert me["podmanVersion"] == "5.2.1"
    assert me["browserVersion"] == "131"


# ── Job progress ────────────────────────────────────────────────────────────
#
# The same shape as `setup`, for the same reason, and with the same failure mode if a
# field is stored but not indexed. These are the tests that would have caught it.

def _job(**over):
    job = {"kind": "populate", "projectId": "p1", "commandId": "cmd-abc",
           "active": True, "step": "Locating the app on this machine",
           "index": 1, "total": 7, "ok": False, "error": "",
           "startedAt": "2026-09-18T10:00:00Z", "endedAt": "",
           "log": [{"at": "t1", "text": "working copy ready"}]}
    job.update(over)
    return job


def test_job_progress_reaches_the_panel(fake_dynamo):
    """A populate used to be a single spinner for up to fifteen minutes."""
    client.post(f"{BASE}/runner/state", json=_state(jobs=[_job()]))

    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["jobs"][0]["kind"] == "populate"
    assert me["jobs"][0]["index"] == 1 and me["jobs"][0]["total"] == 7
    assert me["jobs"][0]["commandId"] == "cmd-abc"
    assert me["jobsAt"]


def test_the_index_carries_jobs(fake_dynamo):
    """The sibling of the test above this block. `list_runner_state` reads the index
    FIRST, so a job stored only on the per-runner row renders nowhere."""
    client.post(f"{BASE}/runner/state", json=_state(jobs=[_job(step="Reading what it "
                                                                   "created", index=5)]))
    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["jobs"] and me["jobs"][0]["index"] == 5
    assert me["jobs"][0]["step"] == "Reading what it created"


def test_the_index_does_not_carry_the_job_log(fake_dynamo):
    """One 400 KB item is shared by EVERY runner. The log lives on the per-runner row,
    which `project_jobs` reads with a GetItem — exactly what `apps`/`logTail` does."""
    from src.qatest import queue
    client.post(f"{BASE}/runner/state", json=_state(jobs=[_job()]))

    index = queue.db.get_item(queue.TABLE, {"testRunId": queue.RUNNER_INDEX_ID,
                                            "projectId": queue.RUNNER_SK}) or {}
    entry = next(v for k, v in index.items()
                 if k.startswith("r_") and isinstance(v, dict) and v.get("runner"))
    assert entry["jobs"] and "log" not in entry["jobs"][0]
    # …and it is still on the row the log is read from.
    assert queue.project_jobs("p1")[0]["log"][-1]["text"] == "working copy ready"


def test_a_job_index_can_never_exceed_its_total(fake_dynamo):
    """The agent computes it, so the server is the side that must clamp: an index past
    the end renders a bar over 100%."""
    client.post(f"{BASE}/runner/state", json=_state(jobs=[_job(index=99, total=7)]))
    assert client.get(f"{BASE}/runners").json()["runners"][0]["jobs"][0]["index"] == 7


def test_an_unknown_job_kind_is_dropped(fake_dynamo):
    """The panel branches on `kind` to choose its wording. A kind nobody renders would
    occupy one of only four slots while saying nothing."""
    client.post(f"{BASE}/runner/state", json=_state(
        jobs=[_job(kind="rm -rf"), _job(kind="emulator-start")]))
    kinds = [j["kind"] for j in client.get(f"{BASE}/runners").json()["runners"][0]["jobs"]]
    assert kinds == ["emulator-start"]


def test_the_job_log_is_bounded_server_side(fake_dynamo):
    """Read through `project_jobs`, not `/runners`: the index drops the log on purpose
    (see the test above), so this is the only surface that carries it."""
    from src.qatest import queue as q
    client.post(f"{BASE}/runner/state", json=_state(jobs=[_job(
        log=[{"at": f"t{i}", "text": f"line {i}"} for i in range(400)])]))
    log = q.project_jobs("p1")[0]["log"]
    assert len(log) <= 12
    assert log[-1]["text"] == "line 399"          # the tail survives


def test_an_idle_claim_hints_that_a_command_is_waiting(fake_dynamo):
    """Commands ride the STATE post, which is three times slower than the claim poll,
    so a pressed Start sat unseen for up to 15s. The claim now carries the hint."""
    client.post(f"{BASE}/runner/state", json=_state())

    idle = client.get(f"{BASE}/runner/next")
    assert idle.status_code == 204
    assert "x-aura-command-waiting" not in idle.headers

    client.post(f"{BASE}/runners/logs",
                json={"runner": RUNNER, "container": "aura-qa-aws-run1"})
    waiting = client.get(f"{BASE}/runner/next")
    assert waiting.status_code == 204
    assert waiting.headers.get("x-aura-command-waiting") == "1"


def test_the_claim_hint_is_a_header_and_never_a_body(fake_dynamo):
    """THE constraint. A protocol-1 agent does `if 204: return None` and treats any 200
    as a job, so a body here raises KeyError inside every deployed runner's poll loop at
    once — which is why `/runner/state` exists as a separate endpoint at all."""
    client.post(f"{BASE}/runner/state", json=_state())
    client.post(f"{BASE}/runners/logs",
                json={"runner": RUNNER, "container": "aura-qa-aws-run1"})

    r = client.get(f"{BASE}/runner/next")
    assert r.status_code == 204
    assert r.content == b""


def test_the_hint_stops_once_the_runner_has_taken_the_command(fake_dynamo):
    """Otherwise it would nudge a redundant state report every five seconds for as long
    as the job runs."""
    client.post(f"{BASE}/runner/state", json=_state())
    client.post(f"{BASE}/runners/logs",
                json={"runner": RUNNER, "container": "aura-qa-aws-run1"})
    assert client.get(f"{BASE}/runner/next").headers.get("x-aura-command-waiting") == "1"

    # The state POST is what hands the command over.
    client.post(f"{BASE}/runner/state", json=_state())
    assert "x-aura-command-waiting" not in client.get(f"{BASE}/runner/next").headers


def test_a_runner_that_cannot_report_jobs_still_works(fake_dynamo):
    """Every OTHER test in this file omits `jobs`, so they all double as this case —
    but the distinction the UI draws deserves naming: no `jobsAt` means "this agent
    cannot tell us", which is not the same as "nothing is running"."""
    client.post(f"{BASE}/runner/state", json=_state())
    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["jobs"] == [] and me["jobsAt"] == ""


def test_a_newer_agent_clears_a_finished_job_by_sending_an_empty_list(fake_dynamo):
    client.post(f"{BASE}/runner/state", json=_state(jobs=[_job()]))
    client.post(f"{BASE}/runner/state", json=_state(jobs=[]))
    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["jobs"] == [] and me["jobsAt"]      # reported, and empty


# ── The emulator endpoint ───────────────────────────────────────────────────
#
# DevMate's, not QA's: this file's user is `user_qa`, which deliberately does NOT hold
# `dev_workspace`, so these three swap in a developer rather than widening the default.

DEV = {"userId": "u-dev", "username": "dev", "role": "user_dev",
       "permissions": ROLE_PERMISSIONS["user_dev"]}


@pytest.fixture
def as_dev():
    app.dependency_overrides[get_current_user] = lambda: DEV
    yield
    app.dependency_overrides[get_current_user] = lambda: QA


def test_the_emulator_endpoint_names_the_project_account(fake_dynamo, as_dev, monkeypatch):
    """The whole point. A console on Floci's default namespace reports every page
    empty for a project whose resources are up, and nothing used to say which account
    Aura actually wrote to."""
    from src.qatest import emulators, plan
    monkeypatch.setattr(plan, "fetch_facts",
                        lambda pid: {"dependencies": [{"name": "boto3"}]})

    body = client.get(f"{BASE}/emulators/p1").json()
    assert body["account"] == emulators.account_for("p1")
    assert len(body["account"]) == 12 and body["account"].isdigit()
    assert body["clouds"] == ["aws"]


def test_a_project_with_no_cloud_has_no_account(fake_dynamo, as_dev, monkeypatch):
    """ABSENT IS NOT ZERO. `000000000000` is Floci's DEFAULT namespace — naming it
    here would point the reader at the exact wrong account."""
    from src.qatest import plan
    monkeypatch.setattr(plan, "fetch_facts", lambda pid: {"dependencies": []})

    body = client.get(f"{BASE}/emulators/p1").json()
    assert body["account"] == ""
    assert body["clouds"] == []


def test_the_emulator_endpoint_returns_only_this_projects_jobs(fake_dynamo, as_dev, monkeypatch):
    from src.qatest import plan
    monkeypatch.setattr(plan, "fetch_facts",
                        lambda pid: {"dependencies": [{"name": "boto3"}]})
    client.post(f"{BASE}/runner/state", json=_state(
        jobs=[_job(projectId="p1"), _job(projectId="p2", commandId="cmd-other")]))

    jobs = client.get(f"{BASE}/emulators/p1").json()["jobs"]
    assert [j["commandId"] for j in jobs] == ["cmd-abc"]
    assert jobs[0]["runner"] and jobs[0]["stale"] is False


# ── The heartbeat wire contract ─────────────────────────────────────────────
#
# `HeartbeatRequest` is where live emulator state enters the system, and pydantic
# DROPS any field the model does not declare — silently, with a 200 OK. A test that
# calls `queue.heartbeat()` directly cannot see that: it passes while the real path
# throws the payload away. This one goes over HTTP, which is the only way the model
# is exercised at all.

def _heartbeat(run_id, project_id, **body):
    return client.post(f"{BASE}/runner/{run_id}/heartbeat",
                       params={"projectId": project_id, "phase": body.get("phase", "")},
                       json=body)


def test_the_heartbeat_model_accepts_every_field_the_agent_sends(fake_dynamo):
    """The agent's on_event builds this payload. Anything missing from the model is
    dropped before the queue sees it, and the panel it feeds stays empty."""
    schema = app.openapi()["components"]["schemas"]["HeartbeatRequest"]["properties"]
    for field in ("phase", "totalPassed", "totalFailed", "totalSkipped",
                  "totalUnemulated", "totalCases", "stepIndex", "phaseDetail",
                  "emulators"):
        assert field in schema, f"the heartbeat model drops {field!r}"


def test_live_emulators_survive_the_round_trip_over_http(fake_dynamo):
    row = queue.enqueue("p1", "", "qa")
    r = _heartbeat(row["testRunId"], "p1", phase="emulator", totalCases=3,
                   phaseDetail="aws emulator ready on :4566",
                   emulators=[{"cloud": "aws", "port": 4566, "started": True,
                               "container": "aura-qa-aws-x",
                               "image": "docker.io/floci/floci:latest"}])
    assert r.status_code == 200, r.text

    active = client.get(f"{BASE}/active/p1").json()["active"][0]
    assert active["emulators"], "the emulator never reached /active"
    assert active["emulators"][0]["cloud"] == "aws"
    assert active["emulators"][0]["port"] == 4566
    assert active["phaseDetail"] == "aws emulator ready on :4566"


def test_a_stopped_emulator_replaces_the_started_one(fake_dynamo):
    """Last write wins per cloud, so the panel shows the CURRENT set rather than an
    append-only history of every transition."""
    row = queue.enqueue("p1", "", "qa")
    _heartbeat(row["testRunId"], "p1", phase="emulator", totalCases=3,
               emulators=[{"cloud": "aws", "started": True}])
    _heartbeat(row["testRunId"], "p1", phase="emulator", totalCases=3,
               emulators=[{"cloud": "aws", "started": False, "stopped": True}])

    emus = client.get(f"{BASE}/active/p1").json()["active"][0]["emulators"]
    assert len(emus) == 1 and emus[0]["stopped"] is True


def test_unemulated_is_carried_separately_over_http(fake_dynamo):
    row = queue.enqueue("p1", "", "qa")
    _heartbeat(row["testRunId"], "p1", phase="step", totalCases=4,
               totalPassed=1, totalUnemulated=2)
    active = client.get(f"{BASE}/active/p1").json()["active"][0]
    assert active["totalUnemulated"] == 2
    assert queue.progress(row["testRunId"], "p1")["done"] == 3


# ── The run's own console ───────────────────────────────────────────────────
#
# `phaseDetail` carries only the CURRENT line. Without a history a remote run shows
# one sentence that keeps changing, which is not "what is happening" — it is a
# glimpse of one moment, and the busy stretches worth watching are exactly the ones
# that flash past between two polls.

def test_the_console_is_stored_as_the_runner_sends_it(fake_dynamo):
    """The runner holds the history and sends all of it; the server stores it.

    This was a read-modify-write append, and it lost almost everything — a step event
    beats immediately, so several heartbeats are in flight at once, each reads the row
    before the previous landed, and each write clobbers the last. A 12-case run ended
    with a single line in the console.
    """
    row = queue.enqueue("p1", "", "qa")
    console = [
        {"at": "t1", "phase": "emulator", "text": "starting the aws emulator on :4566"},
        {"at": "t2", "phase": "emulator", "text": "aws emulator ready on :4566"},
    ]
    _heartbeat(row["testRunId"], "p1", phase="emulator", totalCases=2, events=console)
    console.append({"at": "t3", "phase": "step", "text": "1. application loads — passed"})
    _heartbeat(row["testRunId"], "p1", phase="step", totalCases=2, events=console)

    activity = client.get(f"{BASE}/active/p1").json()["active"][0]["activity"]
    assert [a["text"] for a in activity] == [c["text"] for c in console]


def test_concurrent_heartbeats_cannot_lose_console_lines(fake_dynamo):
    """The failure that shipped: interleaved beats, each carrying the full history.
    Whichever lands last must still hold every line."""
    row = queue.enqueue("p1", "", "qa")
    console = []
    for i in range(6):
        console.append({"at": f"t{i}", "phase": "step", "text": f"step {i}"})
        _heartbeat(row["testRunId"], "p1", phase="step", totalCases=6,
                   events=list(console))
    # And a late beat carrying an EARLIER snapshot, as a slow request would.
    _heartbeat(row["testRunId"], "p1", phase="step", totalCases=6,
               events=console[:3])

    activity = queue.progress(row["testRunId"], "p1")["activity"]
    assert len(activity) >= 3, "the console was emptied by a stale beat"


def test_the_console_is_capped_so_the_row_cannot_grow_without_bound(fake_dynamo):
    """The row is rewritten on every beat and DynamoDB items cap at 400 KB."""
    row = queue.enqueue("p1", "", "qa")
    console = [{"at": f"t{i}", "phase": "step", "text": f"step {i}"} for i in range(200)]
    _heartbeat(row["testRunId"], "p1", phase="step", totalCases=200, events=console)

    activity = queue.progress(row["testRunId"], "p1")["activity"]
    assert len(activity) == queue.ACTIVITY_MAX
    # The TAIL survives — the recent lines are the ones being watched.
    assert activity[-1]["text"] == "step 199"


def test_a_heartbeat_without_events_leaves_the_console_alone(fake_dynamo):
    """Most beats carry no new events. They must not wipe the history."""
    row = queue.enqueue("p1", "", "qa")
    _heartbeat(row["testRunId"], "p1", phase="step", totalCases=2,
               events=[{"at": "t1", "phase": "plan", "text": "12 cases"}])
    _heartbeat(row["testRunId"], "p1", phase="step", totalCases=2, totalPassed=1)

    assert len(queue.progress(row["testRunId"], "p1")["activity"]) == 1


# ── Health and setup on the wire ────────────────────────────────────────────
#
# Pydantic drops what a model does not declare, silently, with a 200 OK — which is how
# live emulator state was thrown away for a whole release. These go over HTTP for that
# reason: a test that calls queue.record_runner_state directly cannot see it.

HEALTH = {"ok": False, "platform": "darwin/arm64", "checkedAt": "2026-09-11T00:00:00Z",
          "findings": [{"check": "podman.working", "ok": False, "severity": "blocks",
                        "title": "the podman machine is not running",
                        "detail": "podman is installed but its VM is stopped.",
                        "remedy": ["podman machine start"], "fixable": True}]}


def test_a_runners_problems_reach_the_panel(fake_dynamo):
    client.post(f"{BASE}/runner/state", json=_state(health=HEALTH,
                                                    podmanVersion="5.7.0",
                                                    browserVersion="1234"))
    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["health"]["ok"] is False
    assert me["health"]["findings"][0]["remedy"] == ["podman machine start"]
    # The version labels the panel already renders, finally populated.
    assert me["podmanVersion"] == "5.7.0"
    assert me["browserVersion"] == "1234"


def test_findings_are_capped_and_stripped_server_side(fake_dynamo):
    """A runner writes this and every colleague reads it, and every runner's state
    shares ONE DynamoDB item. The agent's own limit is not something to trust."""
    from src.qatest import queue as q

    noisy = {"ok": False, "findings": [
        {"check": f"c{i}", "severity": "blocks",
         "title": "t" * 500, "detail": "d" * 5000,
         "remedy": ["r" * 500, "r2", "r3", "r4"]} for i in range(30)]}
    client.post(f"{BASE}/runner/state", json=_state(health=noisy))

    health = client.get(f"{BASE}/runners").json()["runners"][0]["health"]
    assert len(health["findings"]) == q.HEALTH_MAX_FINDINGS
    first = health["findings"][0]
    assert len(first["title"]) <= 120
    assert len(first["detail"]) <= q.HEALTH_MAX_TEXT
    assert len(first["remedy"]) <= 2


def test_control_characters_are_stripped(fake_dynamo):
    """Runner-authored text rendered in a shared panel."""
    client.post(f"{BASE}/runner/state", json=_state(health={
        "ok": False, "findings": [{"check": "x", "severity": "blocks",
                                   "title": "bad\x07\x00title", "remedy": []}]}))
    title = client.get(f"{BASE}/runners").json()["runners"][0]["health"]["findings"][0]["title"]
    assert "\x07" not in title and "\x00" not in title


def test_a_runner_without_health_still_works(fake_dynamo):
    """An older agent sends none. That is "we did not ask", not "everything is broken"."""
    client.post(f"{BASE}/runner/state", json=_state())
    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["health"] == {}
    assert me["online"] is True


def test_setup_progress_reaches_the_panel(fake_dynamo):
    """The install the USER started, visible in Aura rather than only in a terminal."""
    client.post(f"{BASE}/runner/state", json=_state(setup={
        "active": True, "step": "podman machine init", "index": 2, "total": 4,
        "log": [{"at": "t1", "text": "Downloading machine image…"}]}))

    setup = client.get(f"{BASE}/runners").json()["runners"][0]["setup"]
    assert setup["active"] is True
    assert setup["index"] == 2 and setup["total"] == 4
    assert setup["log"][-1]["text"].startswith("Downloading")


def test_the_setup_log_is_bounded_server_side(fake_dynamo):
    client.post(f"{BASE}/runner/state", json=_state(setup={
        "active": True, "step": "x", "index": 1, "total": 1,
        "log": [{"at": f"t{i}", "text": f"line {i}"} for i in range(400)]}))
    log = client.get(f"{BASE}/runners").json()["runners"][0]["setup"]["log"]
    assert len(log) <= 60
    assert log[-1]["text"] == "line 399"          # the tail survives


def test_an_unhealthy_runner_does_not_disable_the_run_button(fake_dynamo, monkeypatch):
    """Disabling it recreates the deadlock /capabilities was written to fix: the
    button is off, so nothing is queued, so nothing is ever claimed.

    The local branch is suppressed to model a DEPLOYED backend, which is the only
    place this matters — Fargate has neither podman nor a browser, so a connected
    runner is the whole answer to "can anything run".
    """
    from src.qatest import emulators, runner as runner_mod
    monkeypatch.setattr(emulators, "podman_available", lambda: False)
    monkeypatch.setattr(runner_mod, "_playwright_available", lambda: (False, "no browser"))

    client.post(f"{BASE}/runner/state", json=_state(health=HEALTH))
    caps = client.get(f"{BASE}/capabilities").json()

    assert caps["canRun"] is True
    assert "not ready" in caps["reason"]
    assert "podman machine start" in caps["commands"]


def test_capabilities_offers_the_doctor_when_nothing_is_connected(fake_dynamo):
    """Structured, because the panel used to scrape the prose for a `python -m` line."""
    caps = client.get(f"{BASE}/capabilities").json()
    if not caps["runners"] and not caps["local"]:
        assert any("--doctor" in c for c in caps["commands"])
        assert any("--setup" in c for c in caps["commands"])


# ── Cancelling a run ────────────────────────────────────────────────────────
#
# The reaper handles a run that goes QUIET. A run whose runner is alive and wedged
# never goes stale, so before this there was no way to stop one at all — and a stuck
# run blocks deleting its project for ever. Two were found in Dev, running for nine
# days.

def test_a_running_run_can_be_cancelled(fake_dynamo):
    row = queue.enqueue("p1", "", "qa")
    queue.heartbeat(row["testRunId"], "p1", "step", "laptop", {"totalCases": 3})

    r = client.post(f"{BASE}/runs/{row['testRunId']}/cancel", params={"projectId": "p1"})
    assert r.status_code == 200 and r.json()["status"] == queue.CANCELLED


def test_a_cancelled_run_leaves_the_live_set(fake_dynamo):
    """That is the whole point: the Results tab stops showing a run that will never
    finish, and a project delete is no longer blocked by it."""
    row = queue.enqueue("p1", "", "qa")
    client.post(f"{BASE}/runs/{row['testRunId']}/cancel", params={"projectId": "p1"})

    live = [r for r in queue.list_for_project("p1") if r.get("status") in queue.LIVE]
    assert live == []
    assert client.get(f"{BASE}/active/p1").json()["active"] == []


def test_a_queued_run_can_be_cancelled_too(fake_dynamo):
    """It was never claimed, so it is the likeliest thing to be stuck."""
    row = queue.enqueue("p1", "", "qa")
    r = client.post(f"{BASE}/runs/{row['testRunId']}/cancel", params={"projectId": "p1"})
    assert r.status_code == 200


def test_cancelling_a_finished_run_is_refused(fake_dynamo):
    """A run that finished between the click and the write must keep its real result
    rather than have it overwritten with "cancelled"."""
    row = queue.enqueue("p1", "", "qa")
    queue.finish(row["testRunId"], "p1", {"status": "passed", "totalPassed": 3})

    r = client.post(f"{BASE}/runs/{row['testRunId']}/cancel", params={"projectId": "p1"})
    assert r.status_code == 409
    stored = queue.progress(row["testRunId"], "p1")
    assert stored["status"] == "passed", "a finished result was overwritten"


def test_cancelling_records_who_did_it(fake_dynamo):
    row = queue.enqueue("p1", "", "qa")
    client.post(f"{BASE}/runs/{row['testRunId']}/cancel", params={"projectId": "p1"})
    assert "qa" in queue.progress(row["testRunId"], "p1").get("reason", "")


def test_a_cancelled_run_no_longer_blocks_deleting_its_project(fake_dynamo):
    """The reason this exists. A stuck run kept the project undeletable for ever."""
    row = queue.enqueue("p1", "", "qa")
    queue.heartbeat(row["testRunId"], "p1", "step", "laptop", {"totalCases": 3})

    from src.routers.projects import _busy_blockers
    assert _busy_blockers("p1", {"status": "analyzed"}), "expected a blocker first"

    client.post(f"{BASE}/runs/{row['testRunId']}/cancel", params={"projectId": "p1"})
    assert _busy_blockers("p1", {"status": "analyzed"}) == []


# ── Whose machine, and what they call it ────────────────────────────────────
#
# The panel's whole job is to say that Floci is running on a LOCAL machine. It could
# not: the agent's own name for its machine was sent on every request and discarded by
# the server, and the owner was buried inside the `username/tool_label` label. These pin
# both, and pin that neither can be forged by the runner.

def test_the_machine_name_reaches_the_panel(fake_dynamo):
    """The one field that lets the UI say "my-laptop" instead of "qa/qa-runner"."""
    client.post(f"{BASE}/runner/state", json=_state())

    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["machine"] == "my-laptop"
    assert me["owner"] == "qa"
    assert me["ownerId"] == "u1"


def test_the_viewer_is_named_so_the_ui_can_say_your_machine(fake_dynamo):
    """Ownership is decided from one server-stated fact, not inferred in the browser."""
    client.post(f"{BASE}/runner/state", json=_state())
    assert client.get(f"{BASE}/runners").json()["you"] == "qa"


def test_a_runner_cannot_claim_to_be_owned_by_someone_else(fake_dynamo):
    """`owner` is derived from the gateway key. A runner putting it in its own state
    report must not be able to relabel itself as another user's machine — the panel is
    shared, and "your machine" has to mean something."""
    client.post(f"{BASE}/runner/state",
                json=_state(owner="admin", ownerId="u-admin", machine="not-mine"))

    me = client.get(f"{BASE}/runners").json()["runners"][0]
    assert me["owner"] == "qa"
    assert me["ownerId"] == "u1"
    # The machine name IS runner-authored, but it arrives in a header the API sanitises
    # rather than in the body, so the body value is ignored here too.
    assert me["machine"] == "my-laptop"


def test_a_polling_runner_is_labelled_before_it_ever_reports_state(fake_dynamo):
    """A protocol-1 agent never calls /runner/state at all, so labelling only there
    would leave it permanently anonymous."""
    queue.touch_runner(RUNNER, IDENTITY)

    row = queue.runner_state(RUNNER)
    assert row["machine"] == "my-laptop"
    assert row["owner"] == "qa"


def test_a_blank_machine_name_never_overwrites_a_good_one(fake_dynamo):
    """These attributes ride on a call that fires every few seconds. Writing "" over a
    real name would make the label flicker for anyone polling at the same moment."""
    queue.touch_runner(RUNNER, IDENTITY)
    queue.touch_runner(RUNNER, {"owner": "qa", "ownerId": "u1", "machine": ""})

    assert queue.runner_state(RUNNER)["machine"] == "my-laptop"


def test_the_machine_name_is_stripped_and_capped(fake_dynamo, monkeypatch):
    """It is written by whoever holds the gateway key and rendered in front of every
    other user of the deployment, so it is sanitised rather than trusted."""
    from starlette.datastructures import Headers

    class _Req:
        headers = Headers({"X-Aura-Runner-Name": "bad\x07\x00name" + "x" * 200})

    assert qa_router._machine_name(_Req()) == "badname" + "x" * (
        qa_router._MACHINE_MAX - len("bad\x07\x00name"))
    assert len(qa_router._machine_name(_Req())) <= qa_router._MACHINE_MAX


def test_a_run_records_the_machine_it_executed_on(fake_dynamo):
    """Stamped on the run row rather than joined against the runner list: a finished run
    has to keep saying where it ran long after that laptop went offline."""
    queue.enqueue("proj-1", ran_by="u-qa", run_id="run-loc")
    assert queue.claim(RUNNER, IDENTITY)

    live = queue.progress("run-loc", "proj-1")
    assert live["runnerMachine"] == "my-laptop"
    assert live["runnerOwner"] == "qa"


# ── Adoption: Aura must not stop what it did not start ───────────────────────
#
# Floci's ports are fixed and are Floci's own, so a developer running `floci start`, or
# starting an emulator from DevMate, holds exactly the port a run wants. Until adoption
# existed the run simply died on "proxy already running" — reproduced by hand before
# this was written. These pin the behaviour that replaced it.

def test_an_already_running_emulator_is_adopted_not_restarted(monkeypatch):
    from src.qatest import emulators

    monkeypatch.setattr(emulators, "podman_ready", lambda: (True, ""))
    monkeypatch.setattr(emulators, "_ready", lambda port, timeout=None: True)
    monkeypatch.setattr(emulators, "_container_on_port",
                        lambda port: {"name": "floci", "image": "floci/floci:1.7.0"})
    monkeypatch.setattr(emulators, "image_digest", lambda image: "sha256:abc")
    ran = []
    monkeypatch.setattr(emulators, "_run", lambda args, **kw: ran.append(args) or (0, ""))

    rec = emulators.EmulatorSet([], "run1")._start(emulators._BY_NAME["aws"])

    assert rec.adopted is True and rec.started is True
    assert rec.container == "floci"
    # The image REALLY there, not the one this run would have used.
    assert rec.image == "floci/floci:1.7.0"
    assert not any("run" in a for a in ran), "it started a container anyway"


def test_an_adopted_emulator_is_never_removed(monkeypatch):
    """The single assertion that makes running your own emulator safe."""
    from src.qatest import emulators
    from src.qatest.types import EmulatorRecord

    removed = []
    monkeypatch.setattr(emulators, "_run",
                        lambda args, **kw: removed.append(args) or (0, ""))

    es = emulators.EmulatorSet([], "run1")
    es.records = [EmulatorRecord(cloud="aws", image="i", digest="d", port=4566,
                                 container="floci", started=True, adopted=True)]
    es.stop()

    assert removed == [], "it removed a container it did not start"


def test_a_container_aura_started_is_still_removed(monkeypatch):
    """Adoption must not turn into a leak for the normal path."""
    from src.qatest import emulators
    from src.qatest.types import EmulatorRecord

    removed = []
    monkeypatch.setattr(emulators, "_run",
                        lambda args, **kw: removed.append(args) or (0, ""))

    es = emulators.EmulatorSet([], "run1")
    es.records = [EmulatorRecord(cloud="aws", image="i", digest="d", port=4566,
                                 container="aura-qa-aws-run1", started=True)]
    es.stop()

    assert removed == [["rm", "-f", "aura-qa-aws-run1"]]


def test_a_failed_start_never_removes_the_shared_emulator(monkeypatch):
    """`_start` fills `container` from `dev_container()` BEFORE it knows whether
    anything came up, so a record for a start that failed still names the shared
    container. Removing it deleted an emulator this run never created — reproduced by
    running this file's own suite against a live `aura-dev-aws`, which it destroyed."""
    from src.qatest import emulators
    from src.qatest.types import EmulatorRecord

    removed = []
    monkeypatch.setattr(emulators, "_run",
                        lambda args, **kw: removed.append(args) or (0, ""))

    es = emulators.EmulatorSet([], "run1")
    es.records = [EmulatorRecord(cloud="aws", image="i", digest="d", port=4566,
                                 container="aura-dev-aws", started=False,
                                 error="podman is not installed")]
    es.stop()
    assert removed == []


def test_the_shared_emulator_is_left_running_even_when_this_run_started_it(monkeypatch):
    """What `stop()`'s own note has always claimed — "true whether this run found it up
    or started it" — and what the code did not do. One container per cloud serves every
    project on the machine."""
    from src.qatest import emulators
    from src.qatest.types import EmulatorRecord

    removed = []
    monkeypatch.setattr(emulators, "_run",
                        lambda args, **kw: removed.append(args) or (0, ""))

    es = emulators.EmulatorSet([], "run1")
    es.records = [EmulatorRecord(cloud="aws", image="i", digest="d", port=4566,
                                 container="aura-dev-aws", started=True)]
    es.stop()
    assert removed == []


# ── The managed-prefix widening ──────────────────────────────────────────────

def test_a_devmate_container_counts_as_aura_managed(fake_dynamo):
    """`aura-dev-` is the project-scoped lifetime. Three independent checks decide
    "is this ours"; missing one makes such a container invisible rather than broken."""
    client.post(f"{BASE}/runner/state", json=_state(containers=[
        {"id": "1", "name": "aura-dev-aws-proj1", "image": "floci/floci",
         "status": "Up", "ports": "4566", "cloud": "aws"}]))

    row = client.get(f"{BASE}/runners").json()["runners"][0]["containers"][0]
    assert row["managed"] is True


def test_logs_are_accepted_for_a_devmate_container(fake_dynamo):
    res = client.post(f"{BASE}/runners/logs",
                      json={"runner": RUNNER, "container": "aura-dev-aws-proj1"})
    assert res.status_code == 200


def test_logs_are_still_refused_for_a_foreign_container(fake_dynamo):
    """Widening the prefix must not widen it to everything."""
    res = client.post(f"{BASE}/runners/logs",
                      json={"runner": RUNNER, "container": "someones-postgres"})
    assert res.status_code == 400


# ── The inventory command ────────────────────────────────────────────────────

def test_inventory_is_refused_for_an_unknown_cloud(fake_dynamo):
    """The value reaches podman and boto3 on someone's machine."""
    res = client.post(f"{BASE}/runners/inventory",
                      json={"runner": RUNNER, "cloud": "../etc/passwd"})
    assert res.status_code == 400


def test_inventory_is_requested_and_answered(fake_dynamo):
    res = client.post(f"{BASE}/runners/inventory",
                      json={"runner": RUNNER, "cloud": "aws"})
    assert res.status_code == 200
    command_id = res.json()["commandId"]

    # Before the runner answers, it is pending — not an error and not empty.
    pending = client.get(f"{BASE}/runners/inventory/{command_id}",
                         params={"runner": RUNNER}).json()
    assert pending["status"] == "pending"


# ── Container-backed services (Lambda) ───────────────────────────────────────
#
# Mounting the container runtime socket into Floci lets anything inside it start
# containers on the host. That is a real privilege escalation, so it is opt-in and the
# demo must never depend on it.

def test_the_runtime_socket_is_not_mounted_when_the_flag_is_off(monkeypatch):
    """Explicitly OFF, not "unset".

    This read the developer's own src/.env, so it asserted a default rather than a
    behaviour — and failed on any machine that had legitimately turned Lambda on. What
    matters is that the flag being off means no socket, whatever the machine believes.
    """
    from src.config_settings import get_settings
    from src.qatest import emulators

    monkeypatch.setenv("QATEST_CONTAINER_BACKED_SERVICES", "false")
    get_settings.cache_clear()
    try:
        assert emulators._socket_args(emulators._BY_NAME["aws"]) == []
    finally:
        get_settings.cache_clear()


def test_enabling_it_mounts_the_socket_and_a_named_network(monkeypatch):
    from src.config_settings import get_settings
    from src.qatest import emulators

    monkeypatch.setenv("QATEST_CONTAINER_BACKED_SERVICES", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(emulators, "_runtime_socket", lambda: "/run/user/1000/x.sock")
    monkeypatch.setattr(emulators, "_run", lambda args, **kw: (0, ""))

    args = emulators._socket_args(emulators._BY_NAME["aws"])
    get_settings.cache_clear()

    # Lowercase :z — :Z is a PRIVATE relabel and breaks the mount for a second container.
    assert "/run/user/1000/x.sock:/var/run/docker.sock:z" in args
    # The named network: rootless podman's default bridge gives no reachable
    # inter-container IPs, so the Lambda Runtime API callback never arrives without it.
    assert emulators.CONTAINER_NETWORK in args
    assert "FLOCI_HOSTNAME=floci" in args


def test_no_socket_degrades_rather_than_failing(monkeypatch):
    """A run without Lambda is a run with one `unemulated` case. A run that will not
    start is worse."""
    from src.config_settings import get_settings
    from src.qatest import emulators

    monkeypatch.setenv("QATEST_CONTAINER_BACKED_SERVICES", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(emulators, "_runtime_socket", lambda: "")

    args = emulators._socket_args(emulators._BY_NAME["aws"])
    get_settings.cache_clear()
    assert args == []


def test_the_socket_path_is_never_hardcoded(monkeypatch):
    """It is /run/user/501/... on a Mac and /run/user/1000/... on a typical Linux box.
    A hardcoded path fails on whichever one you did not test."""
    from src.qatest import emulators
    monkeypatch.setattr(emulators, "_run",
                        lambda args, **kw: (0, "/run/user/4242/podman/podman.sock"))
    assert emulators._runtime_socket() == "/run/user/4242/podman/podman.sock"


def test_a_second_command_is_actually_dispatched(fake_dynamo):
    """`take_command` refuses while cmdTakenAt is set — that is what makes dispatch
    exactly-once. `request_command` therefore has to clear it, or only the FIRST command
    in a runner's life is ever collected and every later one times out as "the runner
    did not answer", which reads as a dead runner rather than a stuck row.

    Found when an emulator-start sat untouched behind an inventory command that had
    completed minutes earlier."""
    first = queue.request_command(RUNNER, "logs", "aura-qa-aws-1")["commandId"]
    assert queue.take_command(RUNNER)["id"] == first
    queue.record_command_result(RUNNER, first, key="k")

    second = queue.request_command(RUNNER, "inventory", "aws")["commandId"]
    taken = queue.take_command(RUNNER)

    assert taken is not None, "the second command was never dispatched"
    assert taken["id"] == second and taken["kind"] == "inventory"


def test_the_clouds_list_reaches_the_runner(fake_dynamo):
    """emulator-start needs to know WHICH emulators, and the list is derived server-side
    from the project's dependencies."""
    queue.request_command(RUNNER, "emulator-start", "proj-1", clouds="aws,gcp")
    assert queue.take_command(RUNNER)["clouds"] == "aws,gcp"


# ── A directory user's gateway key must actually work ───────────────────────
#
# `authenticate` resolves a directory user's role from their LDAP groups on every login
# and stores nothing, so their userId is "ldap:<name>" and `users` holds no row for them.
# resolve_credential looked the role up there and defaulted to "user_dev", which carries
# no qa_workspace — so a key minted by anyone signed in through the directory was dead on
# arrival and stayed dead however many times it was rotated. A dev QA runner sat 403-ing
# for four days on exactly this, and rotating the key (twice) could not have helped.

def test_a_directory_users_key_keeps_the_role_it_was_minted_with(monkeypatch):
    from src.services import gateway_service as gw

    minted = gw.generate_api_key("ldap:admin", tool_label="qa-runner",
                                 role_id="super_admin")
    stored = {}

    def fake_get_item(table, key):
        if table == "users":
            return None          # a directory user has no row here. That is the point.
        return stored.get(key.get("keyId"))

    from src.database import dynamo_client as db
    stored[minted["keyId"]] = {
        "keyId": minted["keyId"], "userId": "ldap:admin", "active": True,
        "toolLabel": "qa-runner", "roleId": "super_admin"}
    monkeypatch.setattr(db, "get_item", fake_get_item)
    monkeypatch.setattr(db, "update_item", lambda *a, **k: None)

    user = gw.resolve_credential(minted["key"])

    assert user.role == "super_admin"
    assert "qa_workspace" in user.permissions, (
        "a directory user's runner key cannot authenticate — this is the bug that made "
        "the dev runner unfixable by rotation")


def test_a_key_minted_before_this_still_resolves(monkeypatch):
    """Old rows carry no roleId. They must keep behaving exactly as they did."""
    from src.database import dynamo_client as db
    from src.services import gateway_service as gw

    monkeypatch.setattr(db, "get_item", lambda table, key: (
        None if table == "users" else
        {"keyId": key.get("keyId"), "userId": "ldap:someone", "active": True,
         "toolLabel": "qa-runner"}))          # no roleId
    monkeypatch.setattr(db, "update_item", lambda *a, **k: None)

    assert gw.resolve_credential("gw-anything").role == "user_dev"


def test_a_local_users_row_still_wins(monkeypatch):
    """A user WITH a row keeps getting their current role, so a later promotion or
    demotion takes effect without re-minting every key."""
    from src.database import dynamo_client as db
    from src.services import gateway_service as gw

    monkeypatch.setattr(db, "get_item", lambda table, key: (
        {"userId": "u1", "username": "admin", "roleId": "super_admin"}
        if table == "users" else
        {"keyId": key.get("keyId"), "userId": "u1", "active": True,
         "toolLabel": "qa-runner", "roleId": "user_dev"}))   # stale, must lose
    monkeypatch.setattr(db, "update_item", lambda *a, **k: None)

    assert gw.resolve_credential("gw-anything").role == "super_admin"


# ── Runner liveness must be scoped too ───────────────────────────────────────
#
# Scoping the QUEUE stopped one environment claiming another's runs. It did not stop them
# sharing runner rows — a runner is named `<username>/<toolLabel>`, identical everywhere,
# so two Auras on one AWS account wrote the SAME row. A laptop agent polling localhost
# kept a row that made DEV report a runner it did not have: canRun stayed true, the Start
# button stayed enabled, and the queued run waited 884 seconds for a machine that was
# never listening to it.

def test_a_runners_row_is_scoped_to_the_environment_it_polls(fake_dynamo, monkeypatch):
    from src.qatest import queue as q

    monkeypatch.setattr(q, "_scope", lambda: "ecs/prod")
    assert "ecs/prod" in q._runner_key("admin/qa-runner")["testRunId"]
    monkeypatch.setattr(q, "_scope", lambda: "local/development")
    assert "local/development" in q._runner_key("admin/qa-runner")["testRunId"]


def test_one_environments_runner_is_invisible_to_another(fake_dynamo, monkeypatch):
    """The assertion that matters. An identically-named runner on a laptop must not make
    a deployed environment believe it can execute anything."""
    from src.qatest import queue as q

    monkeypatch.setattr(q, "_scope", lambda: "local/development")
    q.touch_runner("admin/qa-runner", {"owner": "admin", "machine": "laptop"})
    assert [r["name"] for r in q.list_runner_state()] == ["admin/qa-runner"]

    monkeypatch.setattr(q, "_scope", lambda: "ecs/prod")
    assert q.list_runner_state() == [], "dev can see a runner attached to localhost"
    assert q.online_runners() == [], "canRun would be true with no runner listening"


def test_the_two_do_not_overwrite_each_others_state(fake_dynamo, monkeypatch):
    """They shared a row, so this was not only a display problem — each poll clobbered
    the other's reported podman state."""
    from src.qatest import queue as q

    monkeypatch.setattr(q, "_scope", lambda: "local/development")
    q.record_runner_state("admin/qa-runner", _state(podman=True),
                          {"machine": "laptop"})
    monkeypatch.setattr(q, "_scope", lambda: "ecs/prod")
    q.record_runner_state("admin/qa-runner", _state(podman=False),
                          {"machine": "ec2-box"})

    monkeypatch.setattr(q, "_scope", lambda: "local/development")
    mine = q.list_runner_state()[0]
    assert mine["machine"] == "laptop" and mine["podman"] is True


def test_a_directory_users_key_reports_the_name_everything_else_uses(monkeypatch):
    """`owner` is compared against the JWT's username to decide "is this my machine".
    A directory userId is "ldap:admin" while the JWT says "admin", so a runner the
    operator started appeared to belong to someone else and DevMate refused to offer
    Start on it."""
    from src.database import dynamo_client as db
    from src.services import gateway_service as gw

    monkeypatch.setattr(db, "get_item", lambda table, key: (
        None if table == "users" else
        {"keyId": key.get("keyId"), "userId": "ldap:admin", "active": True,
         "toolLabel": "qa-runner", "roleId": "super_admin"}))
    monkeypatch.setattr(db, "update_item", lambda *a, **k: None)

    user = gw.resolve_credential("gw-anything")
    assert user.username == "admin", "the runner would look like someone else's machine"
    assert user.user_id == "ldap:admin", "the id itself must not be rewritten"


def test_a_local_users_row_still_supplies_the_name(monkeypatch):
    from src.database import dynamo_client as db
    from src.services import gateway_service as gw

    monkeypatch.setattr(db, "get_item", lambda table, key: (
        {"userId": "u1", "username": "someone", "roleId": "user_qa"} if table == "users"
        else {"keyId": key.get("keyId"), "userId": "u1", "active": True,
              "toolLabel": "qa-runner"}))
    monkeypatch.setattr(db, "update_item", lambda *a, **k: None)
    assert gw.resolve_credential("gw-anything").username == "someone"


def test_a_superseded_command_says_so_instead_of_vanishing(fake_dynamo):
    """The runner row holds ONE command slot, so a later request overwrites the previous
    one. The caller still polling the old id used to get a bare 404 forever, time out,
    and report "the runner did not answer" — about a runner that had answered, to a
    question that had been replaced. That sends the reader to inspect a healthy machine.
    """
    first = queue.request_command(RUNNER, "inventory", "aws")["commandId"]
    # Past the dedupe window, so this genuinely replaces rather than returning `first`.
    queue.record_command_result(RUNNER, first, key="k")
    second = queue.request_command(RUNNER, "inventory", "aws")["commandId"]
    assert second != first

    stale = queue.command_result(RUNNER, first)
    assert stale is not None, "a superseded command must not look like an unknown runner"
    assert stale["superseded"] is True

    live = queue.command_result(RUNNER, second)
    assert not live.get("superseded")


def test_an_unknown_runner_is_still_a_miss(fake_dynamo):
    """`superseded` must not swallow the genuine not-found case."""
    assert queue.command_result("nobody/qa-runner", "cmd-x") is None


def test_the_endpoint_reports_superseded_rather_than_404(fake_dynamo):
    first = queue.request_command(RUNNER, "inventory", "aws")["commandId"]
    queue.record_command_result(RUNNER, first, key="k")
    queue.request_command(RUNNER, "inventory", "aws")

    res = client.get(f"{BASE}/runners/inventory/{first}", params={"runner": RUNNER})
    assert res.status_code == 200
    assert res.json()["status"] == "superseded"


def test_a_late_result_does_not_land_on_a_newer_command(fake_dynamo, monkeypatch):
    """One slot per runner, so a slow command can finish after a newer one replaced it.

    `record_command_result` used to write unconditionally, so a populate finishing after
    someone pressed Stop reported ITS outcome against Stop's id — telling the reader that
    Stop had succeeded, and quoting work Stop never did.
    """
    first = queue.request_command(RUNNER, "app-populate", "proj-1")["commandId"]
    queue.take_command(RUNNER)

    # A newer command takes the slot while the first is still running.
    # Restored automatically; a bare assignment here leaked into every later test.
    monkeypatch.setattr(queue, "_age_seconds", lambda _at: 999)  # past COMMAND_DEDUPE_S
    second = queue.request_command(RUNNER, "emulator-stop", "proj-1")["commandId"]
    assert second != first

    # The slow one finishes and tries to record against its own, now-stale, id.
    queue.record_command_result(RUNNER, first, key="s3://somewhere", error="")

    newer = queue.command_result(RUNNER, second)
    assert not newer.get("resultAt"), "the stale result overwrote the newer command"
    assert queue.command_result(RUNNER, first).get("superseded")


def test_a_command_payload_round_trips_to_the_runner(fake_dynamo):
    """`app-populate` needs a workspace handle it cannot derive for itself."""
    handle = {"url": "https://example/x.tgz", "sha256": "abc", "bytes": 12}
    queue.request_command(RUNNER, "app-populate", "proj-1", clouds="aws", payload=handle)
    taken = queue.take_command(RUNNER)
    assert taken["kind"] == "app-populate"
    assert taken["payload"] == handle


def test_a_command_without_a_payload_still_reads_back_cleanly(fake_dynamo):
    """Every other kind sends none, and must not get a None the agent would trip on."""
    queue.request_command(RUNNER, "inventory", "aws")
    assert queue.take_command(RUNNER)["payload"] == {}

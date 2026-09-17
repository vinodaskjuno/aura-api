"""Running a developer's own project locally, and reporting it honestly.

Every test here guards something that was found by reading the code rather than by
watching it fail, and each one protects a specific way this feature could be quietly
wrong rather than visibly broken:

  - a dev server killed because a pid looked familiar
  - a compose stack's volumes deleted by a teardown written for disposable test stacks
  - an app session reported by the runner and dropped by three separate whitelists
  - a developer's own OTEL endpoint silently repointed at Aura
  - "no traces yet" and "your key was refused" rendering identically
"""
from __future__ import annotations

import json

import pytest

from src.qatest import appserver, appsession, queue


# ── Ownership: never kill what you cannot prove is yours ─────────────────────

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from src.qatest import provision
    monkeypatch.setattr(provision, "workspace_root", lambda pid: tmp_path / pid)
    return tmp_path


def test_a_session_file_survives_a_working_copy_refresh(workspace, monkeypatch):
    """The `.aura-` prefix is load-bearing, not cosmetic.

    `provision.fetch` deletes every child of the working copy except node_modules,
    .venv and `.aura-*`. Named anything else, the file describing a RUNNING process
    would be deleted by the next refresh.
    """
    assert appsession.SESSION_FILE.startswith(".aura-")


def test_sweep_deletes_a_file_whose_process_is_gone(workspace):
    root = workspace / "p1"
    root.mkdir(parents=True)
    (root / appsession.SESSION_FILE).write_text(json.dumps({
        "sessionId": "abc", "projectId": "p1",
        "apps": [{"pid": 999999, "port": 59999, "kind": "api"}],
    }))
    notes = appsession.sweep(["p1"])
    assert notes == []
    assert not (root / appsession.SESSION_FILE).exists()


def test_sweep_refuses_a_port_it_cannot_prove_is_ours(workspace, monkeypatch):
    """A pid is not an ownership token. `emulators` can ask podman whose container
    holds a port; nothing equivalent exists for a plain process, so an unproven one
    is REPORTED and left alone."""
    root = workspace / "p2"
    root.mkdir(parents=True)
    (root / appsession.SESSION_FILE).write_text(json.dumps({
        "sessionId": "mine", "projectId": "p2",
        "apps": [{"pid": 4711, "port": 8123, "kind": "api"}],
    }))
    monkeypatch.setattr(appsession, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(appsession, "_port_answers", lambda port, timeout=0.3: True)
    monkeypatch.setattr(appsession, "_marker_of", lambda pid: "someone-elses")

    notes = appsession.sweep(["p2"])

    assert len(notes) == 1
    assert "cannot prove it started" in notes[0]
    assert "left alone" in notes[0]
    # And above all: the file is kept, because deleting it would lose the only record
    # that something is on that port.
    assert (root / appsession.SESSION_FILE).exists()


def test_sweep_recognises_our_own_orphan(workspace, monkeypatch):
    root = workspace / "p3"
    root.mkdir(parents=True)
    (root / appsession.SESSION_FILE).write_text(json.dumps({
        "sessionId": "mine", "projectId": "p3",
        "apps": [{"pid": 4711, "port": 8123, "kind": "api"}],
    }))
    monkeypatch.setattr(appsession, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(appsession, "_port_answers", lambda port, timeout=0.3: True)
    monkeypatch.setattr(appsession, "_marker_of", lambda pid: "mine")

    notes = appsession.sweep(["p3"])
    assert "still running on port 8123" in notes[0]


# ── Compose teardown: a dev session is not a disposable test stack ───────────

def _compose_spec(tmp_path):
    return appserver.AppSpec(
        kind="api", name="stack", directory=tmp_path,
        command=["podman", "compose", "up", "-d"], port=8080, env={}, compose=True)


def test_a_dev_session_keeps_its_volumes(tmp_path, monkeypatch):
    """`down -v` removes NAMED VOLUMES — the developer's own database.

    Correct for a QA run, whose stack is disposable and whose next run must start
    from nothing. Catastrophic for "run my project locally".
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(appserver.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)))

    apps = appserver.RunningApps([], destroy_volumes=False,
                                 compose_project="aura-dev-app-p1")
    apps._composed = [_compose_spec(tmp_path)]
    apps._stop_compose()

    assert calls, "teardown ran no command at all"
    assert "-v" not in calls[0]
    # And an explicit project name, so `down` cannot reach a stack the developer
    # started by hand in the same directory.
    assert "-p" in calls[0] and "aura-dev-app-p1" in calls[0]


def test_a_test_run_still_destroys_its_volumes(tmp_path, monkeypatch):
    """The default is unchanged, so every existing caller behaves exactly as before."""
    calls: list[list[str]] = []
    monkeypatch.setattr(appserver.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)))

    apps = appserver.RunningApps([])
    apps._composed = [_compose_spec(tmp_path)]
    apps._stop_compose()

    assert "-v" in calls[0]


# ── The runner's report survives every whitelist between it and the screen ───

def test_apps_reach_the_panel(monkeypatch):
    """Three separate places drop an unknown field, and a miss in any one of them
    shows as an empty panel with the data sitting correctly in DynamoDB."""
    stored: dict = {}
    monkeypatch.setattr(queue.db, "update_item",
                        lambda table, key, payload: stored.update(payload))
    monkeypatch.setattr(queue, "runner_state", lambda runner: dict(stored))
    monkeypatch.setattr(queue, "_index_runner", lambda runner, payload: stored.update(
        {"_indexed": payload}))

    queue.record_runner_state("laptop", {
        "protocol": 3,
        "apps": [{"projectId": "p1", "kind": "api", "url": "http://127.0.0.1:8000",
                  "port": 8000, "pid": 1, "healthy": True,
                  "logTail": ["boot", "ready"]}],
    })

    assert stored["apps"][0]["projectId"] == "p1"
    assert stored["apps"][0]["url"] == "http://127.0.0.1:8000"
    # Its own timestamp: `updatedAt` staleness cannot distinguish "the runner went
    # offline" from "the runner is fine and the app died".
    assert stored["appsAt"]


def test_the_console_never_enters_the_shared_index_row():
    """Every runner's state shares ONE 400 KB item. A console tail is the largest
    thing an app row can carry, so it lives on the per-runner row only."""
    rows = queue._without_logs([{"projectId": "p1", "logTail": ["a"] * 40}])
    assert "logTail" not in rows[0]
    assert rows[0]["projectId"] == "p1"


# ── Mutual exclusion ─────────────────────────────────────────────────────────

def test_populate_and_app_start_cannot_run_together():
    """They contend for the same ports, the same emulator env and the same working
    copy. In particular `_command_populate` re-runs `detect`, whose `port_free` check
    would come back blocked and fail the populate BLAMING THE READER'S OWN SESSION."""
    from src.qatest import agent

    agent._JOBS.clear()
    assert agent._claim_job("app-start", "p1") == ""
    busy = agent._claim_job("populate", "p1")
    assert "already running app-start" in busy
    # A different project is unaffected.
    assert agent._claim_job("populate", "p2") == ""
    agent._release_job("app-start", "p1")
    assert agent._claim_job("populate", "p1") == ""
    agent._JOBS.clear()


# ── A QA run meets a live session ────────────────────────────────────────────

def test_a_run_adopts_a_live_session_without_losing_file_checks(monkeypatch):
    """Adoption must NOT go through `app_url`.

    `execute` sets `root = None` whenever a URL is supplied, which silently drops every
    structural and file-check case — a run that reports success while a whole case kind
    never executed. So adoption yields an object wearing `RunningApps`'s surface and
    leaves `root` alone.
    """
    from src.qatest import service

    class Spec:
        kind, name, url, port, compose, blocked, env = "api", "app", "http://127.0.0.1:9", 9, False, "", {}

    class Session:
        env_fingerprint = ""
        apps = type("A", (), {"started": [Spec()], "procs": {}, "failures": []})()

    monkeypatch.setattr(appsession, "get", lambda pid: Session())
    events: list = []

    with service._maybe_apps("", [], {}, lambda e, **k: events.append((e, k)), "p1") as apps:
        assert apps is not None
        assert apps.url_for("api") == "http://127.0.0.1:9"

    assert any(k.get("adopted") for _, k in events), "adoption was not announced"


def test_adoption_refuses_a_different_emulator_set(monkeypatch):
    """The session was started against some set of endpoints. If the run wants others,
    the app under test is pointed at the wrong account and a green result is a lie."""
    from src.qatest import service

    class Session:
        env_fingerprint = "aaaaaaaaaaaaaaaa"
        apps = type("A", (), {"started": [], "procs": {}, "failures": []})()

    monkeypatch.setattr(appsession, "get", lambda pid: Session())
    monkeypatch.setattr(appsession, "env_fingerprint", lambda env: "bbbbbbbbbbbbbbbb")
    events: list = []

    with service._maybe_apps("", [], {"AWS_ENDPOINT_URL": "x"},
                             lambda e, **k: events.append((e, k)), "p1"):
        pass

    assert any("different set of emulators" in str(k.get("message")) for _, k in events)


# ── Instrumentation refuses what it cannot honestly do ───────────────────────

def test_a_compose_stack_is_never_instrumented(tmp_path):
    """`spec.command` is `[compose…, up, -d]`, so the process is compose, not the app.
    Instrumenting would mean editing the user's docker-compose.yml — and since
    `detect_compose` wins outright, this is exactly the case most people mean by
    "run my project locally", so it has to be SAID rather than silently skipped."""
    spec = appserver.AppSpec(kind="api", name="stack", directory=tmp_path,
                             command=["podman", "compose", "up", "-d"], port=8080,
                             env={}, compose=True)
    assert appserver.instrumented(spec, tmp_path / ".aura-otel") is spec


def test_a_node_dev_server_is_never_instrumented(tmp_path):
    """`npm run dev` is a bundler; a frontend's model calls happen in the browser."""
    spec = appserver.AppSpec(kind="ui", name="web", directory=tmp_path,
                             command=["npm", "run", "dev"], port=5173, env={})
    assert appserver.instrumented(spec, tmp_path / ".aura-otel") is spec


def test_python_is_wrapped_on_its_own_interpreter(tmp_path):
    """The MODULE, not the console script: a script installed by `pip --target` carries
    an absolute shebang pointing at whichever interpreter did the install."""
    venv_python = str(tmp_path / ".venv" / "bin" / "python")
    spec = appserver.AppSpec(kind="api", name="api", directory=tmp_path,
                             command=[venv_python, "-m", "uvicorn", "app:app"],
                             port=8000, env={})
    sidecar = tmp_path / ".aura-otel"

    wrapped = appserver.instrumented(spec, sidecar)

    assert wrapped.command[0] == venv_python
    assert wrapped.command[1:3] == ["-m", "opentelemetry.instrumentation.auto_instrumentation"]
    assert wrapped.command[3:] == spec.command
    # The sidecar rides on PYTHONPATH for exactly one process — the project's own
    # environment is never touched.
    assert str(sidecar) in wrapped.env["PYTHONPATH"]


def test_the_sidecar_directory_survives_a_working_copy_refresh():
    """Same `.aura-` rule as the session file: `provision.fetch` would otherwise delete
    the installed sidecar on the next refresh, and the stamp would still read fresh."""
    from src.qatest import provision
    assert provision.SIDECAR_DIR.startswith(".aura-")


# ── Version skew is reported, not just enforced ─────────────────────────────

def test_the_poll_reply_states_what_the_server_expects():
    """The server cannot reach the runner — that is the whole point of a poll loop —
    so the only way it can tell an agent it is stale is to say so in the reply the
    agent is already reading. Without this the skew is discoverable ONLY by pressing
    a button in a browser and being refused."""
    import inspect
    from src.routers import qa

    source = inspect.getsource(qa.post_runner_state)
    assert "expectedProtocol" in source
    assert "_APP_SESSION_PROTOCOL" in source


def test_a_stale_runner_warns_once_in_its_own_terminal(caplog):
    from src.qatest import agent

    agent._STALE_WARNED[0] = False
    with caplog.at_level("WARNING"):
        agent._warn_if_stale({"expectedProtocol": agent.PROTOCOL + 1})
    assert "OUT OF DATE" in caplog.text
    assert "Restart this agent" in caplog.text

    # Once, not every 15 seconds: a warning that repeats forever becomes wallpaper.
    caplog.clear()
    with caplog.at_level("WARNING"):
        agent._warn_if_stale({"expectedProtocol": agent.PROTOCOL + 1})
    assert caplog.text == ""
    agent._STALE_WARNED[0] = False


def test_a_current_runner_says_nothing(caplog):
    from src.qatest import agent

    agent._STALE_WARNED[0] = False
    with caplog.at_level("WARNING"):
        agent._warn_if_stale({"expectedProtocol": agent.PROTOCOL})
        # An older server sends no opinion at all. Absent is not zero.
        agent._warn_if_stale({})
        agent._warn_if_stale(None)
        agent._warn_if_stale({"expectedProtocol": "nonsense"})
    assert caplog.text == ""


def test_the_refusal_names_the_machine_and_leads_with_restart(monkeypatch):
    """Two runners can share one label — it is `username/tool_label`, derived from the
    gateway KEY rather than from `--name`, so two machines using one key both render
    as `admin/qa-runner`. A message naming only the label sends the reader to whichever
    one they thought of first, which is exactly what happened.

    And it must not say "git pull" first: the constant is bound at import, so a daemon
    started before the change is stale no matter what is on disk.
    """
    from fastapi import HTTPException
    from src.qatest import queue
    from src.routers import qa

    monkeypatch.setattr(queue, "list_runner_state", lambda *a, **k: [
        {"name": "admin/qa-runner", "protocol": 2, "machine": "local-verify"}])

    with pytest.raises(HTTPException) as raised:
        qa._require_app_capable("admin/qa-runner")

    detail = str(raised.value.detail)
    assert raised.value.status_code == 409
    assert "local-verify" in detail, "the message must name the machine, not just the label"
    restart_at, pull_at = detail.index("restart it"), detail.index("git pull")
    assert restart_at < pull_at, "restart is the likely fix and must come first"

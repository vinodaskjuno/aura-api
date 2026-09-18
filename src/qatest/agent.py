"""The self-hosted QA runner.

Run this on a machine that has podman, Chromium and the project's cloned working copy —
typically a developer laptop. It claims runs queued from the Aura UI, executes them
locally, and uploads evidence to the same S3 bucket the Results tab reads. The button in
a deployed environment goes live without any AWS compute existing.

    python -m src.qatest.agent --api https://aura-dev-….elb.amazonaws.com --key gw-…

Why a poll loop and not a push: polling needs no inbound port, no public hostname and no
NAT traversal on the machine running it, and it survives a laptop sleeping — it simply
stops claiming. The alternative would have required dispatch permissions
(`ecs:RunTask` / `lambda:InvokeFunction`) that nothing in this deployment has.

What this deliberately does NOT do:

  * talk to Neo4j — it cannot; the plan arrives with the claim
  * write to the knowledge graph — the API does that from the stored report
  * hold long-lived AWS credentials — each claim carries scoped, 1-hour ones
  * run two runs at once — the floci emulators publish FIXED host ports
    (-p 4566:4566), so two concurrent runs needing the same cloud would collide
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import platform
import re
import sys
import threading
import time
from datetime import datetime, timezone

from src.qatest import tracing

log = logging.getLogger("qa-runner")

POLL_SECONDS = 5
#: Heartbeat at most this often. Every step would be a request per navigation.
HEARTBEAT_MIN_INTERVAL_S = 10

#: What this agent understands. Sent on every request so the server knows whether it
#: may include newer fields inside the `cases` it ships — an older agent splats those
#: straight into `Case(**c)` and dies on an unknown key, uncaught, inside its poll loop.
#:
#: 3 adds the app-session commands (`app-start`/`app-stop`/`app-status`). The server
#: MUST NOT park those on a runner reporting less: `_run_command` answers an unknown
#: kind with "unknown command 'app-start'", which reaches the reader as a bare failure
#: with nothing to act on. Deploying Aura updates the server; this file lives on a
#: laptop and updates when someone gets round to it, so the two are always skewed.
PROTOCOL = 3

#: How many console lines the runner keeps and resends. Matches the server's own cap;
#: at ~200 chars a line that is a payload of tens of KB at worst, on a local runner.
ACTIVITY_KEEP = 80

#: Hard cap on a log body. A chatty emulator must not be able to push megabytes
#: through the API on someone else's behalf.
LOG_MAX_BYTES = 128 * 1024

#: Which env var carries each cloud's endpoint. Taken from the Cloud's own env template
#: rather than rebuilt from the port, so a cloud that changes its variable cannot leave
#: the inventory quietly reading the wrong address.
_ENDPOINT_VAR = {"aws": "AWS_ENDPOINT_URL", "gcp": "STORAGE_EMULATOR_HOST",
                 "azure": "AZURE_ENDPOINT_URL", "oci": "OCI_ENDPOINT_URL"}

#: Report what podman is running every Nth poll. The machine's state changes far more
#: slowly than the queue does, and this is a request per interval per runner.
STATE_EVERY_N_POLLS = 3


def _preflight() -> list[str]:
    """Everything that must be true before claiming anything. Returns problems.

    Kept as a name and a shape because `QA_RUNNER.md` documents calling it directly.
    The checks themselves live in `doctor`, so the agent, `--doctor` and the server's
    `/capabilities` cannot disagree about whether this machine is ready.
    """
    from src.qatest import doctor

    return [f"{f.title} — try: {f.remedy[0]}" if f.remedy else f.title
            for f in doctor.diagnose(deep=True).blocking]


def _print_diagnosis(diag, caveat: str = "") -> None:
    print(f"\nQualityMind runner — {diag.platform}/{diag.arch}\n")
    for line in diag.lines():
        print(line)
    if caveat:
        print(f"\n  NOTE: {caveat}")

    for finding in diag.blocking + diag.degraded:
        print(f"\n  {'BLOCKS' if finding.severity == 'blocks' else 'DEGRADES'}: "
              f"{finding.title}")
        if finding.detail:
            print(f"    {finding.detail}")
        for command in finding.remedy:
            print(f"    $ {command}")

    if diag.ok:
        print("\n  ready to run tests"
              + (" (some runs will be limited — see above)" if diag.degraded else ""))
    else:
        print(f"\n  {len(diag.blocking)} problem(s) must be fixed before a run can "
              f"execute.\n  `python -m src.qatest.agent --setup` walks through them.")
    print()


def _doctor_command(as_json: bool) -> int:
    from src.qatest import doctor

    diag = doctor.diagnose(deep=True)
    if as_json:
        print(json.dumps({**diag.as_dict(failures_only=False, limit=100),
                          "caveat": doctor.platform_caveat()}, indent=2))
    else:
        _print_diagnosis(diag, doctor.platform_caveat())
    return 0 if diag.ok else 1



class AuthRejected(Exception):
    """The server refused this runner's gateway key.

    Its own class because it is the one failure that retrying cannot fix. The key is
    read from argv at startup, so a rotated key cannot be picked up by a running
    process — an agent that keeps polling on a rejected credential is not resilient,
    it is silent. One did exactly that against dev for four days: a 401 every five
    seconds, no runner ever registered, and the UI could only say "no runner
    connected" because an unauthenticated poll cannot be attributed to anyone.
    """

    def __init__(self, api: str, status: int) -> None:
        super().__init__(f"{api} rejected this runner's key ({status})")
        self.api = api
        self.status = status

    def advice(self) -> str:
        """Exactly what to do, with the host already filled in."""
        return (
            f"\nThe API at {self.api} rejected this runner's key ({self.status}).\n"
            "The key was most likely rotated or revoked. A running agent cannot pick up\n"
            "a new one, so this is fatal rather than something to retry.\n\n"
            "  1. Mint a replacement (the role must carry qa_workspace —\n"
            "     admin, super_admin and user_qa do; user_dev gets 403):\n"
            f"       curl -H \"Authorization: Bearer <your-jwt>\" \\\n"
            f"            {self.api}/gateway/keys/me/qa-runner\n"
            "     or use the Onboard tab in the Aura UI.\n\n"
            "  2. Restart this agent with the new key:\n"
            "       python -m src.qatest.agent --api <api> --key gw-… --name <machine>\n")


class Client:
    """The three calls the runner makes. Thin on purpose."""

    def __init__(self, api: str, key: str, name: str) -> None:
        import httpx

        self.name = name
        self._http = httpx.Client(
            base_url=api.rstrip("/"),
            headers={"x-api-key": key,
                     "X-Aura-Runner-Protocol": str(PROTOCOL),
                     "X-Aura-Runner-Name": name},
            timeout=30.0,
        )
        # An older server has no /runner/state. One 404 is information; one every 15
        # seconds for the life of the process is noise, so stop asking.
        self._state_supported = True

    def claim(self) -> dict | None:
        response = self._http.get("/api/qa/runner/next")
        if response.status_code == 204:
            return None
        if response.status_code in (401, 403):
            raise AuthRejected(str(self._http.base_url), response.status_code)
        response.raise_for_status()
        return response.json()

    def heartbeat(self, run_id: str, project_id: str, phase: str,
                  counts: dict | None = None) -> None:
        try:
            self._http.post(f"/api/qa/runner/{run_id}/heartbeat",
                            params={"projectId": project_id, "phase": phase},
                            json={"phase": phase, **(counts or {})})
        except Exception as exc:                              # noqa: BLE001
            # A dropped heartbeat must not abort a run that is otherwise fine. The
            # reaper's window is minutes, so a few failures are survivable.
            log.debug("heartbeat failed: %s", exc)

    def report_state(self, state: dict) -> dict:
        """Tell the server what this machine is running. Returns any pending command.

        Deliberately a SEPARATE endpoint rather than a richer response from
        `/runner/next`: that call answers 204 when idle, and today's agents do
        `if 204: return None` then treat any 200 as a job. A 200 without a `runId`
        would raise KeyError inside the poll loop and kill every deployed runner at
        once.
        """
        if not self._state_supported:
            return {}
        try:
            response = self._http.post("/api/qa/runner/state", json=state)
            if response.status_code in (401, 403):
                raise AuthRejected(str(self._http.base_url), response.status_code)
            if response.status_code == 404:
                self._state_supported = False
                log.info("this Aura does not accept runner state — "
                         "the Floci panel will stay empty (upgrade the server)")
                return {}
            response.raise_for_status()
            return response.json() or {}
        except AuthRejected:
            # Deliberately NOT swallowed with everything else. This is the startup call,
            # so it is the earliest point a bad key can be reported at all.
            raise
        except Exception as exc:                              # noqa: BLE001
            log.debug("state report failed: %s", exc)
            return {}

    def finish(self, run_id: str, project_id: str, report: dict) -> None:
        response = self._http.post(f"/api/qa/runner/{run_id}/finish",
                                   params={"projectId": project_id},
                                   json={"report": report})
        response.raise_for_status()


def _apply_credentials(creds: dict | None) -> None:
    """Put the claim's scoped credentials where boto3 will find them.

    This is the whole reason evidence.py, s3_client.py and the DynamoDB index need no
    changes: they keep using boto3's default credential chain, and the process env is
    part of that chain.

    None means the API minted nothing — normal for local development, where the
    developer's own credentials are already configured.
    """
    if not creds or not creds.get("accessKeyId"):
        # CLEAR anything a previous run left behind. Inheriting them would be both
        # wrong and quietly dangerous: the last run's session policy is scoped to the
        # last run's S3 prefix, so this run would fail with AccessDenied naming a
        # session id that is not its own — which is exactly how confusing that is to
        # debug.
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            os.environ.pop(var, None)
        _reset_aws_clients()
        log.info("no scoped credentials in the claim; using the ambient AWS config")
        return

    os.environ["AWS_ACCESS_KEY_ID"] = creds["accessKeyId"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = creds["secretAccessKey"]
    os.environ["AWS_SESSION_TOKEN"] = creds["sessionToken"]
    if creds.get("region"):
        os.environ["AWS_DEFAULT_REGION"] = creds["region"]

    # Setting the env is NOT enough, and this was found the hard way on dev: the run
    # executed, then every upload failed with InvalidAccessKeyId.
    #
    # Two reasons, both in src/storage/s3_client.py:
    #   1. _get_client() passes settings.aws_access_key_id EXPLICITLY when it is set,
    #      which overrides the environment entirely. A developer machine with stale keys
    #      in .env therefore ignores the scoped credentials it was just handed.
    #   2. _client and _account_id_cache are module-level caches, so a client built
    #      before this point would keep the old identity for the life of the process.
    from src.config_settings import get_settings

    settings = get_settings()
    settings.aws_access_key_id = ""
    settings.aws_secret_access_key = ""
    if creds.get("region"):
        settings.s3_region = creds["region"]
    _reset_aws_clients()

    log.info("using scoped credentials for this run, expiring %s",
             creds.get("expiresAt", "?"))


def _reset_aws_clients() -> None:
    """Force every AWS client to resolve credentials again.

    Setting the environment is not sufficient, and this took a second dev run to find.
    `boto3.client()` uses the module-level DEFAULT_SESSION when one exists, and a
    session CACHES the credentials it resolved — so the second run kept writing with
    the first run's assumed-role session, whose policy is scoped to the first run's S3
    prefix. The symptom was an AccessDenied naming `qa-runner-<previous run id>` while
    uploading the current run's report.

    Aura's own module-level caches have to go too: s3_client._client,
    s3_client._account_id_cache and dynamo_client._resource all outlive a run.
    """
    import boto3

    from src.database import dynamo_client
    from src.storage import s3_client

    boto3.DEFAULT_SESSION = None
    s3_client._client = None
    s3_client._account_id_cache = ""
    dynamo_client._resource = None


def _apply_run_context(run_id: str, project_id: str) -> None:
    """Publish which run this is, so anything it invokes can attribute its own spend.

    The Aura gateway proxies opaque model traffic: a request from a test run is
    indistinguishable from one typed into a chat box, so attribution has to be declared
    by the caller through the X-Aura-Project-Id / X-Aura-Test-Run-Id headers
    (routers/gateway.py). Putting the values in the environment means a subprocess or a
    library picks them up without this module having to know it exists.

    Nothing reads these yet, and that is correct: a run's plan comes from the knowledge
    graph by design, so executing it costs no tokens at all. This is the pipe, laid
    before the traffic — the alternative is discovering later that exploratory runs have
    been spending into an untraceable pool.
    """
    os.environ["AURA_TEST_RUN_ID"] = run_id
    os.environ["AURA_PROJECT_ID"] = project_id


def run_one(client: Client, job: dict) -> str:
    """Execute one claimed run. Returns its final status."""
    from src.qatest.service import execute

    run_id, project_id = job["runId"], job["projectId"]
    _apply_credentials(job.get("credentials"))
    _apply_run_context(run_id, project_id)

    last_beat = [0.0]
    tally = {"totalPassed": 0, "totalFailed": 0, "totalSkipped": 0,
             "totalUnemulated": 0, "stepIndex": 0,
             "totalCases": len(job.get("cases") or [])}
    # Keyed by cloud, last write wins, so a stop overwrites the start it replaces and
    # the server always holds the CURRENT set rather than an append-only history.
    emulators_seen: dict[str, dict] = {}
    # The run's whole console, kept here and sent in full on every beat. The server
    # stores it verbatim rather than appending, because several heartbeats are in
    # flight at once during the step phase and a read-modify-write append loses all
    # but the last. Bounded so a long run cannot grow the payload without limit.
    activity: list[dict] = []

    def on_event(event: dict) -> None:
        phase = event.get("type", "")

        # Count as they happen, so the UI can show 4/12 rather than just "running".
        if phase == "step":
            # `unemulated` is counted on its own rather than folded into skipped. They
            # mean different things to a reader: skipped is "we declined to ask", and
            # unemulated is "we asked and the harness could not answer" — which is not
            # a statement about the application at all.
            key = {"passed": "totalPassed", "failed": "totalFailed",
                   "skipped": "totalSkipped", "unemulated": "totalUnemulated"}.get(
                       event.get("status", ""))
            if key:
                tally[key] += 1
            tally["stepIndex"] = int(event.get("index") or tally["stepIndex"])
            if event.get("total"):
                tally["totalCases"] = int(event["total"])

        # Which Floci containers are serving this run, so the UI can show them coming
        # up and going away. Only the server-relevant fields; `error` is truncated
        # because this rides every heartbeat.
        if phase == "emulator" and event.get("cloud"):
            emulators_seen[event["cloud"]] = {
                "cloud": event.get("cloud", ""),
                "image": event.get("image", ""),
                "digest": event.get("digest", ""),
                "port": event.get("port", 0),
                "container": event.get("container", ""),
                "started": bool(event.get("started")),
                "stopped": bool(event.get("stopped")),
                "error": str(event.get("error") or "")[:400],
            }

        # Log every event — this is the operator's only view of a remote run — but rate
        # limit the network call. A step always beats: the counts are the point, and a
        # 10-second stale count on a 5-second run would never be seen at all.
        log.info("  %-9s %s", phase, str(event.get("message") or "")[:110])
        message = str(event.get("message") or "")
        if phase == "step":
            # Steps carry no message; the action IS the line worth showing.
            message = (f"{event.get('index', '')}. {event.get('action', '')}"
                       f" — {event.get('status', '')}")
        if message:
            activity.append({"at": datetime.now(timezone.utc).isoformat(),
                             "phase": phase, "text": message[:200]})
            del activity[:-ACTIVITY_KEEP]

        now = time.monotonic()
        if phase in ("step", "emulator", "done", "error") or \
                now - last_beat[0] >= HEARTBEAT_MIN_INTERVAL_S:
            last_beat[0] = now
            counts = dict(tally)
            counts["phaseDetail"] = str(event.get("message") or "")[:300]
            if emulators_seen:
                counts["emulators"] = list(emulators_seen.values())
            if activity:
                counts["events"] = list(activity)
            client.heartbeat(run_id, project_id, phase, counts)

    log.info("running %s for project %s (%d cases)",
             run_id, project_id, len(job.get("cases") or []))

    # Fetch Aura's copy of the code and install its dependencies BEFORE the run. A
    # project analysed inside a deployed container exists nowhere on this machine, and
    # `appserver` starts the app under test from the local filesystem — so without this
    # every run of such a project ends as "No working copy found".
    #
    # Reported through the same event stream as the run itself, so a first run that
    # spends three minutes in `npm ci` looks busy rather than hung.
    from src.qatest import provision

    problems = provision.prepare(project_id, job.get("workspace"), on_event)
    for problem in problems:
        log.warning("  provisioning: %s", problem)

    try:
        report = execute(
            project_id,
            app_url=job.get("appUrl", ""),
            run_id=run_id,
            ran_by=job.get("ranBy", "") or client.name,
            exploratory=bool(job.get("exploratory")),
            # The API does the write-back; graph_writeback needs Neo4j, which is not
            # reachable from here.
            write_graph=False,
            on_event=on_event,
            cases=job.get("cases"),
            clouds=job.get("clouds"),
            kinds=job.get("kinds"),
        )
    except Exception as exc:                                  # noqa: BLE001
        # Report the failure rather than dying silently — otherwise the run sits at
        # `running` until the reaper gets it, minutes later, with no explanation.
        log.exception("run %s failed", run_id)
        report = {"runId": run_id, "projectId": project_id, "status": "unavailable",
                  "reason": f"the runner failed: {type(exc).__name__}: {exc}"[:400]}

    client.finish(run_id, project_id, report)
    status = report.get("status", "?")

    # A run launches Chromium anyway, so its outcome is free information — it keeps
    # the cheap every-15s check from going stale about the one thing that is expensive
    # to verify.
    from src.qatest import doctor as _doctor
    _doctor.record_launch(status != "unavailable",
                          str(report.get("reason") or "")[:200])

    log.info("finished %s: %s (%s passed, %s failed, %s skipped)", run_id, status,
             report.get("totalPassed"), report.get("totalFailed"),
             report.get("totalSkipped"))
    return status


#: Where Floci's own console listens. Fixed, like the emulator ports.
FLOCI_UI_PORT = 4500


def _floci_ui() -> dict:
    """Whether Floci's dashboard is reachable on this machine.

    Cheap and short: this runs on every state report, and an absent dashboard is the
    normal case — it must cost a refused connection, not a timeout.
    """
    import socket

    try:
        with socket.create_connection(("127.0.0.1", FLOCI_UI_PORT), timeout=0.3):
            return {"running": True, "port": FLOCI_UI_PORT}
    except OSError:
        return {"running": False, "port": FLOCI_UI_PORT}


def _machine_state(busy_run_id: str = "", include_unmanaged: bool = False,
                   allow_logs: bool = True) -> dict:
    """What this machine looks like right now, for the Floci panel.

    The API runs on Fargate and can never see podman, so everything the panel shows
    about a developer's machine is reported from here.
    """
    from src.qatest import appsession, doctor, emulators

    # Shallow: this runs every ~15s, and the deep check launches a real browser.
    diag = doctor.diagnose(deep=False)

    return {
        "protocol": PROTOCOL,
        # From the doctor, not from `import playwright`. The old flag could report
        # "Chromium ✓" on a machine where Chromium would not start, because the check
        # behind it only proved the PACKAGE was installed.
        "podman": bool(diag.find("podman.working") and diag.find("podman.working").ok),
        "browser": bool(diag.find("browser.binary") and diag.find("browser.binary").ok),
        "podmanVersion": diag.version_of("podman.working"),
        "browserVersion": diag.version_of("browser.binary"),
        "health": diag.as_dict(),
        "os": f"{platform.system().lower()}/{platform.machine()}",
        # Floci's own dashboard, if the operator runs it. Probed HERE rather than from
        # the browser: a page served over HTTPS cannot fetch http://localhost without
        # tripping mixed-content, and this process is already on the machine.
        "flociUi": _floci_ui(),
        "busyRunId": busy_run_id,
        "acceptsLogCommands": allow_logs,
        "containers": emulators.list_containers(include_unmanaged),
        # Long-lived app sessions on this machine. Probed per report, with the same
        # bounded connect `_floci_ui` uses — the server cannot see a laptop's ports,
        # so if this does not say the app is up, nothing else can.
        "apps": appsession.describe_all(),
        # Sent on EVERY report, empty list included — that is what tells the server this
        # agent can report jobs at all, and it is how a finished job gets cleared rather
        # than left pinned on the row.
        "jobs": _job_snapshots(),
    }


def _command_inventory(command: dict, result: dict) -> dict:
    """What is inside a running emulator, right now.

    Uses the SAME collector the end-of-run snapshot uses, so the live drawer and the
    stored report can never disagree about what a resource looks like. Output is JSON
    because the server parks it in S3 verbatim and the UI renders it structured.
    """
    from src.qatest import emulators, inventory

    cloud = str(command.get("container") or "")      # the command's free-text slot
    # The emulator is shared by every project on the machine, so an inventory has to say
    # WHOSE resources it wants. Without this it reads the default account and reports
    # either nothing or another project's — the panel would be confidently wrong.
    project_id = str(command.get("projectId") or "")
    known = emulators._BY_NAME.get(cloud)
    if not known:
        result["error"] = f"unknown cloud {cloud!r}"
        return result
    if not emulators._ready(known.port, timeout=2):
        result["error"] = (f"nothing is answering on :{known.port} — the {cloud} "
                           f"emulator is not running")
        return result

    found = inventory.collect({cloud: known.env(project_id)[_ENDPOINT_VAR[cloud]]},
                              account=emulators.account_for(project_id))
    result["ok"] = True
    result["output"] = json.dumps(found)
    return result


#: Long-running work on this machine, keyed by (kind, projectId). Was a single-slot
#: list holding only the populate thread; app-start needs the same treatment and the
#: two MUST be mutually exclusive per project — they contend for the same ports, the
#: same emulator env and the same working copy. A real lock now, because "is anything
#: running" and "claim the slot" are no longer the same question.
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()

#: Work that must not run at the same time for one project. Populate boots the app on
#: its detected port; a live session is already holding it, and `detect` would come
#: back `blocked` — failing the populate with a message blaming the reader's own session.
_EXCLUSIVE = ("populate", "app-start")


#: Work that contends for the MACHINE, not for one project. `dev_container()` ignores the
#: project id — there is one `aura-dev-<cloud>` per machine — and `start_container` does
#: `podman rm -f` before it runs. Two projects starting at once would have the second
#: delete the first's container mid-readiness. That was impossible only while these ran
#: inline on the poll loop, which serialised them; threading them makes it reachable, so
#: they share one slot and exclude each other.
_MACHINE_SCOPED = ("emulator-start", "emulator-stop")

#: Results from finished background threads, waiting for the next state POST to carry them.
_PENDING_RESULTS: list = []

#: Per-stage progress for the work above, keyed the same way, read by `_machine_state`
#: and reported on every state POST. Separate from `_JOBS` because it OUTLIVES the slot:
#: a job that has finished — above all one that failed — must stay readable long enough
#: for the panel to show why, and the slot has to be free the instant the thread ends.
_JOB_PROGRESS: dict = {}
_PROGRESS_LOCK = threading.Lock()

#: How long a finished job stays visible. Long enough to read a failure after switching
#: tabs, short enough that it cannot be mistaken for something still running.
JOB_KEEP_S = 120

#: Stage counts, so the server and the panel never have to guess a denominator.
#: Emulator jobs are per cloud and computed at the call site.
_POPULATE_STAGES = 7


def _job_key(kind: str, project_id: str) -> tuple:
    """The slot a job contends for. Machine-scoped kinds collapse onto one key."""
    if kind in _MACHINE_SCOPED:
        return ("emulator", "")
    return (kind, project_id)


class _Progress:
    """What a background job is doing, as it does it.

    Everything here is in memory under a short lock and NOTHING touches HTTP: a job
    thread that blocked on the network to report progress would be back to the problem
    threading solved. The poll loop picks the snapshot up on its next state POST.

    `index` counts stages COMPLETED, never started, so the bar is `index/total` and can
    never be an interpolation. A failure leaves it where it stopped.
    """

    def __init__(self, kind: str, project_id: str, command_id: str, total: int):
        self.key = (kind, project_id, command_id)
        #: Stages ENTERED. `index` (completed) is always this minus one, which is the
        #: arithmetic that keeps a bar from showing work that has not happened yet.
        self._entered = 0
        self.record = {"kind": kind, "projectId": project_id, "commandId": command_id,
                       "active": True, "step": "", "index": 0, "total": max(0, total),
                       "ok": False, "error": "", "startedAt": _stamp(), "endedAt": "",
                       "log": [], "_at": time.time()}
        with _PROGRESS_LOCK:
            _JOB_PROGRESS[self.key] = self.record

    def step(self, label: str) -> None:
        """Enter a stage. Everything before it is now complete."""
        with _PROGRESS_LOCK:
            self._entered += 1
            self.record["index"] = min(self._entered - 1, self.record["total"])
            self.record["step"] = str(label)[:120]
            self.record["_at"] = time.time()

    def note(self, text: str) -> None:
        """A line from inside the current stage. Does not advance anything."""
        if not text:
            return
        with _PROGRESS_LOCK:
            self.record["log"].append({"at": _stamp(), "text": str(text)[:200]})
            del self.record["log"][:-12]
            self.record["_at"] = time.time()

    def event(self, ev: dict) -> None:
        """Adapter for the `{"type": ..., "message": ...}` shape `provision` emits."""
        if isinstance(ev, dict):
            self.note(_redact(str(ev.get("message") or "")))

    def finish(self, ok: bool, error: str = "") -> None:
        """Terminal. Called from ONE place — `_run_threaded`'s `finally` — so every
        early return and every exception in a job body lands here."""
        with _PROGRESS_LOCK:
            self.record["active"] = False
            self.record["ok"] = bool(ok)
            self.record["error"] = _redact(str(error or ""))[:400]
            self.record["endedAt"] = _stamp()
            if ok:
                self.record["index"] = self.record["total"]
            self.record["_at"] = time.time()


class _NullProgress:
    """What a job gets when nobody is watching — the inline `--once` path and the
    existing two-argument tests. Every method is a no-op so job bodies need no guards."""

    def step(self, label: str) -> None: ...
    def note(self, text: str) -> None: ...
    def event(self, ev: dict) -> None: ...
    def finish(self, ok: bool, error: str = "") -> None: ...


_NULL_PROGRESS = _NullProgress()

def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


#: The QUERY STRING of any URL, which is where a presigned credential lives.
#:
#: `provision` reports a fetch failure by interpolating the exception (provision.py:93),
#: and an httpx error carries the full request URL — for a working copy that is an S3
#: link complete with `AWSAccessKeyId`, `Signature` and `x-amz-security-token`. That is
#: fine in a runner's local log and must never reach the server, the runner row or a
#: browser; `request_command` forbids exactly this for `cmdPayload`, and a job log is a
#: new way to leak the same thing.
#:
#: The QUERY only, not the whole URL. Redacting every URL also destroyed
#: `http://localhost:4566` in "port 4566 is held by …, start your app against
#: http://localhost:4566 directly" — which is the one actionable part of that message.
#: Credentials live in the query; a host and path are diagnostics worth keeping.
_URL_QUERY_RE = re.compile(r"(https?://[^\s?]+)\?[^\s'\"]*")


def _redact(text: str) -> str:
    """Strip credentials out of anything a job reports outward."""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", text or "")


def _job_snapshots() -> list[dict]:
    """Jobs worth reporting: everything running, plus recently finished ones.

    Pruned here rather than on a timer — this is called on every state report, which is
    the only moment the answer is used.
    """
    now = time.time()
    with _PROGRESS_LOCK:
        for key, rec in list(_JOB_PROGRESS.items()):
            if not rec["active"] and now - rec["_at"] > JOB_KEEP_S:
                _JOB_PROGRESS.pop(key, None)
        out = [{k: v for k, v in rec.items() if k != "_at"}
               for rec in _JOB_PROGRESS.values()]
    out.sort(key=lambda r: r.get("startedAt") or "", reverse=True)
    out.sort(key=lambda r: not r.get("active"))
    return out[:4]


def _claim_job(kind: str, project_id: str) -> str:
    """Take the slot for this (kind, project), or say what is already holding it."""
    want = _job_key(kind, project_id)
    with _JOBS_LOCK:
        if kind in _MACHINE_SCOPED:
            # One emulator per machine, so the contention is machine-wide and the
            # message must not name a project as though another one were fine.
            if want in _JOBS:
                return ("already starting or stopping emulators on this machine — "
                        "they are shared, so this waits for that to finish")
            _JOBS[want] = None
            return ""
        for (held_kind, held_project) in _JOBS:
            if held_project != project_id:
                continue
            if kind in _EXCLUSIVE and held_kind in _EXCLUSIVE:
                return f"already running {held_kind} for {held_project} on this machine"
            if held_kind == kind:
                return f"already running {kind} for {held_project} on this machine"
        _JOBS[want] = None
        return ""


def _release_job(kind: str, project_id: str) -> None:
    with _JOBS_LOCK:
        _JOBS.pop(_job_key(kind, project_id), None)


def populate_in_flight() -> str:
    """The project id of a populate currently running, or "" — read by the poll loop."""
    with _JOBS_LOCK:
        for (kind, project_id) in _JOBS:
            if kind == "populate":
                return project_id
    return ""


def blocking_job() -> str:
    """Work that must stand the poll loop off from claiming a RUN, as `kind:projectId`.

    Populate was the only one, because it was the only threaded thing that touched the
    emulator. Emulator start and stop now run off the loop too, and a run claimed while
    a container is being rebuilt under it would adopt an emulator that is about to go
    away. `app-start`/`app-stop` stay out deliberately: a live session is exactly what a
    run is meant to adopt.
    """
    with _JOBS_LOCK:
        for (kind, project_id) in _JOBS:
            if kind == "populate" or kind in _MACHINE_SCOPED or kind == "emulator":
                return f"{kind}:{project_id}"
    return ""


def _run_threaded(kind: str, project_id: str, command: dict, body,
                  stages: int = 0) -> None:
    """Run `body(command, result, progress)` off the poll loop, parking its result.

    Threaded for the reason `_start_populate` documents: work measured in minutes on
    the loop means no state is reported, the row goes stale after RUNNER_STALE_S, and
    the panel swaps itself for "No runner is connected" while the work is still going.

    Owns the whole progress lifecycle, which is what makes the failure story short: a
    body reports the stage it is entering and nothing else, and `finish` is called from
    the `finally` below — so every early `return result`, every raised exception and
    every refused claim ends up recorded in exactly one place.
    """
    command_id = str(command.get("id", "") or "")
    busy = _claim_job(kind, project_id)
    if busy:
        # Recorded, not just returned. The command result says why to whoever polls it,
        # but a reader who has already closed the popup has nothing — and "refused
        # because something else holds it" is the single most confusing outcome to meet
        # as a silent no-op.
        _Progress(kind, project_id, command_id, stages).finish(False, busy)
        _PENDING_RESULTS.append({"id": command_id, "output": "",
                                 "ok": False, "error": busy})
        return

    progress = _Progress(kind, project_id, command_id, stages)

    def wrapper() -> None:
        result = {"id": command_id, "output": "", "ok": False, "error": ""}
        try:
            result = body(command, result, progress)
        except Exception as exc:                              # noqa: BLE001
            # Never let a thread die silently: the reader is watching a spinner that
            # would otherwise run to its timeout and blame the runner for going quiet.
            result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        finally:
            progress.finish(bool(result.get("ok")), str(result.get("error") or ""))
            _PENDING_RESULTS.append(result)
            _release_job(kind, project_id)

    threading.Thread(target=wrapper, name=f"{kind}-{project_id}", daemon=True).start()


def _start_populate(command: dict) -> None:
    """Run a populate on a worker thread, and return at once.

    Commands are normally executed INLINE on the poll loop. That is fine for the others,
    which take milliseconds, and fatal for this one: a first populate installs the
    project's dependencies, and while the loop is blocked nothing reports state. After
    RUNNER_STALE_S (90s) the row goes stale and the panel stops finding an online runner
    of its own — so it swaps itself for "No runner is connected" and unmounts the poller
    watching this very command. The work is still running; the screen says nothing is.

    So: thread it, let the loop keep heartbeating, and hand the result to whichever state
    POST comes after it finishes. `_run_threaded` is that mechanism, shared with
    app-start, which also enforces that the two never run together for one project.
    """
    _run_threaded("populate", str(command.get("container") or ""), command,
                  _command_populate, stages=_POPULATE_STAGES)


def _start_emulator(command: dict) -> None:
    """Start or stop this machine's emulators on a worker thread.

    Threaded for the same reason as populate, and it is not a theoretical one: a cold
    `start_container` is a `podman run` bounded at 120s — which on a first use includes
    pulling the image — followed by a readiness wait bounded at `_ready_timeout()`, 60s
    by default. That is comfortably past RUNNER_STALE_S (90s), so a first Start used to
    take the runner offline in the panel, from the reader's point of view, while doing
    exactly what it was asked.
    """
    clouds = [c for c in str(command.get("clouds") or "").split(",") if c]
    # Start: check podman, then pull / run / wait per cloud. Stop: check podman, then
    # remove per cloud. Computed here because only the caller knows how many clouds.
    stages = (1 + 3 * len(clouds) if command.get("kind") == "emulator-start"
              else 1 + len(clouds))
    _run_threaded(str(command.get("kind") or ""), str(command.get("container") or ""),
                  command, _command_emulator, stages=stages)


def _start_app(command: dict) -> None:
    """Start a project's app and keep it running. Threaded, for the reason above —
    more so: a compose stack's `up` alone is bounded at 600s, and readiness can add
    another 180s, both far past RUNNER_STALE_S."""
    _run_threaded("app-start", str(command.get("container") or ""), command,
                  _command_app_start)


def _stop_app(command: dict) -> None:
    """Stop a project's app. Threaded too: a compose `down` is bounded at 180s, which
    is already past the point where the panel would call this runner stale."""
    _run_threaded("app-stop", str(command.get("container") or ""), command,
                  _command_app_stop)


def _command_populate(command: dict, result: dict, progress=_NULL_PROGRESS) -> dict:
    """Boot the app under test once so it creates its cloud resources, then stop it.

    Pressing Start in DevMate brings up an EMPTY emulator, and readers reasonably expect
    to see their buckets and functions in it. Nothing in Aura creates those: the only
    thing that ever does is the application's own startup code — the demo's FastAPI
    lifespan calling `cloud.ensure()`. Until now that ran solely inside a test run, so an
    emulator started from DevMate stayed empty with no way to fill it.

    This drives the app's own code rather than provisioning anything itself. Aura must
    not become a second, competing declaration of what a project's resources are — one
    that could disagree with what a real deploy produces.

    ATTACHES, NEVER STARTS. `EmulatorSet` is deliberately not used: for a cloud whose port
    happens to be free it would start an `aura-qa-*` container with a run's lifetime,
    which would then be torn down and take the resources with it. The env comes straight
    from `cloud.env()`, which is the same source `EmulatorSet.env` reads.
    """
    from src.qatest import appserver, emulators, inventory, provision

    project_id = str(command.get("container") or "")
    clouds = [c for c in str(command.get("clouds") or "").split(",") if c]

    # Kept apart from `problems` deliberately. A fetch that fails when a working copy is
    # ALREADY on disk is not a failed populate: the copy may simply be a few minutes old.
    # Treating it as fatal made a populate that deployed both Lambdas and every other
    # resource report "the working copy could not be fetched" — the reader is told it
    # failed while the Resources panel fills up behind them, which is worse than either
    # a clean success or a clean failure. Whether it is fatal is decided below, by
    # whether there is anything to run.
    # The emit was `None` here while the run path passed a real callback, which made the
    # single longest part of a populate — installing the project's dependencies — the one
    # part nothing could see. `progress.event` REDACTS, and that is not decoration: a
    # fetch failure is reported by interpolating the exception, and an httpx error string
    # carries the full presigned S3 URL, signature and session token included.
    progress.step("Fetching the working copy and installing dependencies")
    stale = provision.prepare(project_id, command.get("payload") or {}, progress.event)
    problems: list[str] = []

    progress.step("Locating the app on this machine")
    root, checked = appserver.locate(project_id)
    if root is None:
        result["error"] = ("no working copy on this machine for this project"
                           + (f" ({'; '.join(stale)})" if stale else "")
                           + ". Looked in: " + ", ".join(str(c) for c in checked))
        return result
    warnings: list[str] = []
    if stale:
        # Recorded, never silent — but not a failure. The populate below either works or
        # does not, and that is what `ok` reports.
        warnings.append("could not refresh the working copy, so the copy already on this "
                        "machine was used: " + "; ".join(stale))

    # Only the API half. Starting the frontend dev server creates no cloud resources and
    # costs a minute of the reader's time. A compose stack is returned alone by `detect`,
    # so it survives this filter by having no "ui" sibling to drop.
    progress.step("Detecting how the app starts")
    specs = [sp for sp in appserver.detect(root) if sp.kind == "api" or sp.compose]
    if not specs:
        result["error"] = (f"no runnable application found in {root} — there is nothing "
                           f"here whose startup could create cloud resources")
        return result

    # The emulator is SHARED — one per cloud for every project on the machine — so what
    # matters is that it is Aura's, not whose it is. Projects are kept apart inside it by
    # AWS account (`emulators.account_for`), which is why populating an emulator another
    # project is also using is now safe rather than the worst failure available.
    progress.step("Checking the emulator is up")
    env: dict[str, str] = {}
    endpoints: dict[str, str] = {}
    for cloud in clouds:
        known = emulators._BY_NAME.get(cloud)
        if not known:
            problems.append(f"unknown cloud {cloud}")
            continue
        progress.note(f"{cloud} on :{known.port}")
        if not emulators._ready(known.port, timeout=2):
            problems.append(f"the {cloud} emulator is not running — press Start first")
            continue
        holder = emulators._container_on_port(known.port).get("name", "")
        if holder and not holder.startswith(emulators.MANAGED_PREFIXES):
            # Something Aura did not start is on the port — a hand-run `floci start`, or
            # an unrelated service. Refuse rather than provision into it.
            problems.append(
                f"port {known.port} is held by {holder}, which Aura did not start. "
                f"Stop it, or start the emulator from DevMate.")
            continue
        # Scoped to THIS project's account, so its resources are invisible to the others
        # sharing the container.
        env.update(known.env(project_id))
        endpoints[cloud] = known.env(project_id)[_ENDPOINT_VAR[cloud]]

    if not endpoints:
        result["error"] = "; ".join(problems)[:400] or "no usable emulator for this project"
        return result

    progress.step("Starting the app so it creates its resources")
    try:
        with appserver.RunningApps(specs, extra_env=env) as apps:
            # Nothing to do in the body. `__enter__` has already waited for the app to
            # answer, which means its startup — and therefore its provisioning — is done.
            problems.extend(f"{spec.name}: {why}" for spec, why in apps.failures)
    except Exception as exc:                                  # noqa: BLE001
        result["error"] = f"the app did not start: {type(exc).__name__}: {exc}"[:400]
        return result

    progress.step("Reading what it created")
    found = inventory.collect(endpoints, account=emulators.account_for(project_id))
    # `collect` nests per cloud: {"aws": {"s3": [...], "lambda": [...]}}. Counting the
    # outer level finds dicts, not lists, and silently reports zero — which turned a
    # perfectly good populate into "the app created no resources".
    total = sum(len(items)
                for services in (found or {}).values() if isinstance(services, dict)
                for items in services.values() if isinstance(items, list))
    if not total:
        # Started but created nothing. Saying so beats a green tick the Resources panel
        # is about to contradict.
        problems.append("the app started but created no resources — this project may "
                        "create them on first use rather than at startup")

    progress.step("Done")
    result["ok"] = not problems
    result["output"] = json.dumps({"resources": found, "created": total,
                                   "problems": problems, "warnings": warnings})
    if problems:
        result["error"] = "; ".join(problems)[:400]
    return result


def _command_app_start(command: dict, result: dict, progress=_NULL_PROGRESS) -> dict:
    """Start this project's app and leave it running until someone stops it.

    The sibling of `_command_populate`, and deliberately almost all of the same code
    up to the point where populate enters a `with` block and this one does not. What
    populate answers is "did your app create its resources"; what this answers is
    "your app is at http://127.0.0.1:5173, go and use it".

    UI AND API BOTH, unlike populate. Populate filters to `kind == "api"` because only
    a backend's startup creates cloud resources and a frontend would cost the reader a
    minute for nothing. Here the frontend is frequently the whole point.
    """
    from src.qatest import appserver, appsession, emulators, provision

    project_id = str(command.get("container") or "")
    if not project_id:
        result["error"] = "no project"
        return result
    clouds = [c for c in str(command.get("clouds") or "").split(",") if c]
    instrument = bool(command.get("instrument"))

    existing = appsession.get(project_id)
    if existing:
        result["ok"] = True
        result["output"] = json.dumps({"apps": existing.describe(),
                                       "already": True,
                                       "sessionId": existing.session_id})
        return result

    stale = provision.prepare(project_id, command.get("payload") or {}, None)
    warnings: list[str] = []
    if stale:
        warnings.append("could not refresh the working copy, so the copy already on "
                        "this machine was used: " + "; ".join(stale))

    root, checked = appserver.locate(project_id)
    if root is None:
        result["error"] = ("no working copy on this machine for this project"
                           + (f" ({'; '.join(stale)})" if stale else "")
                           + ". Looked in: " + ", ".join(str(c) for c in checked))
        return result

    specs = appserver.detect(root)
    if not specs:
        result["error"] = f"no runnable application found in {root}"
        return result

    # Emulator env, exactly as populate builds it — an app started here should talk to
    # the same Floci containers a test run would give it, scoped to this project's
    # account. Missing emulators are a warning, not a refusal: plenty of projects have
    # no cloud dependencies at all and should still start.
    env: dict[str, str] = {}
    for cloud in clouds:
        known = emulators._BY_NAME.get(cloud)
        if not known:
            warnings.append(f"unknown cloud {cloud}")
            continue
        if not emulators._ready(known.port, timeout=2):
            warnings.append(f"the {cloud} emulator is not running — press Start first")
            continue
        env.update(known.env(project_id))

    env.update(_telemetry_env(command, project_id))

    instrumented, instrumentation_error = False, ""
    if instrument:
        specs, instrumented, instrumentation_error = _instrument_specs(
            root, specs, env, project_id)

    session = appsession.start(
        project_id, specs, env,
        instrumented=instrumented, instrumentation_error=instrumentation_error,
        env_fingerprint=appsession.env_fingerprint(env))

    failures = [f"{spec.name}: {why}" for spec, why in session.apps.failures]

    # If instrumentation is what broke the boot, start again without it rather than
    # failing the whole feature over a tracing checkbox. The 400-char log tail a
    # failure carries usually does not contain the word "opentelemetry", so a reader
    # left with it alone would be debugging their own application for no reason.
    if instrumented and not session.apps.started and failures:
        appsession.stop(project_id)
        plain = appserver.detect(root)
        session = appsession.start(
            project_id, plain, env, instrumented=False,
            instrumentation_error="auto-instrumentation prevented the app from booting; "
                                  "started without tracing. " + "; ".join(failures)[:300],
            env_fingerprint=appsession.env_fingerprint(env))
        warnings.append(session.instrumentation_error)
        failures = [f"{spec.name}: {why}" for spec, why in session.apps.failures]
        _remember_instrumentation_failure(project_id)

    apps = session.describe()
    result["ok"] = bool(apps)
    result["output"] = json.dumps({
        "apps": apps, "sessionId": session.session_id,
        "instrumented": session.instrumented,
        "problems": failures, "warnings": warnings})
    if not apps:
        result["error"] = ("; ".join(failures) or "the app did not start")[:400]
        appsession.stop(project_id)
    return result


def _command_app_stop(command: dict, result: dict, progress=_NULL_PROGRESS) -> dict:
    """Stop this project's app session."""
    from src.qatest import appsession

    project_id = str(command.get("container") or "")
    if not project_id:
        result["error"] = "no project"
        return result

    stopped = appsession.stop(project_id)
    notes = appsession.sweep([project_id]) if not stopped else []
    result["ok"] = True
    result["output"] = json.dumps({"stopped": stopped, "notes": notes})
    if not stopped and notes:
        # Honest rather than reassuring: something IS still on the port, and we
        # declined to kill what we could not prove was ours.
        result["error"] = "; ".join(notes)[:400]
    return result


def _command_app_status(command: dict, result: dict) -> dict:
    """What this project's app session looks like right now."""
    from src.qatest import appsession

    project_id = str(command.get("container") or "")
    session = appsession.get(project_id)
    result["ok"] = True
    result["output"] = json.dumps({
        "apps": session.describe() if session else [],
        "sessionId": session.session_id if session else "",
        "instrumented": bool(session and session.instrumented),
    })
    return result


#: Whether we have already said we are behind. Said ONCE, loudly, rather than every
#: 15 seconds: a warning that repeats forever in a terminal becomes wallpaper, and the
#: operator has to be able to see the poll lines around it.
_STALE_WARNED = [False]


def _warn_if_stale(reply: dict | None) -> None:
    """Say so, here, when this process is older than the server expects.

    THE ONLY CHANNEL THAT EXISTS. The server cannot reach this machine — that is the
    whole point of a poll loop ("no inbound port, no public hostname and no NAT
    traversal") — so it cannot restart us and cannot pop a dialog. What it CAN do is
    state its expectation on every poll, which it now does, and what we can do is
    notice and tell whoever is watching this terminal.

    Without this the skew is discoverable only by pressing a button in a browser and
    being refused, minutes or hours later, by someone who may not be the person who
    started this process.

    An older server sends no `expectedProtocol` at all; absent means "no opinion", not
    "zero", so we say nothing.
    """
    if _STALE_WARNED[0]:
        return
    expected = (reply or {}).get("expectedProtocol")
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        return
    if expected <= PROTOCOL:
        return

    _STALE_WARNED[0] = True
    log.warning(
        "THIS RUNNER IS OUT OF DATE: it speaks protocol %s and the server expects %s. "
        "Features added since protocol %s will be refused. This is almost certainly a "
        "process started before the change — the constant is bound at import, so "
        "editing the file is not enough. Restart this agent. If it still reports %s "
        "afterwards, git pull here first.",
        PROTOCOL, expected, PROTOCOL, PROTOCOL)


def _telemetry_env(command: dict, project_id: str) -> dict:
    """Gateway + OTLP environment for the app under test, WITHOUT clobbering.

    `RunningApps._start` merges `{**toolpath.env(), **extra_env, **spec.env}`, and
    `toolpath.env()` is `{**os.environ, ...}` — so extra_env WINS over anything the
    developer exported. Dict-ordering intuition says the user's own value should win
    and it does not, which means injecting blindly would silently redirect a developer's
    existing collector to Aura. So: subtract. Anything already set is left alone, and
    the skipped keys are reported rather than swallowed.

    The credential comes from the SERVER, in the command payload, never from this
    runner's own `--key`: that key carries `qa_workspace`, and it would be sitting in
    the user's application process where one exception page dumping os.environ leaks it.
    """
    wanted = dict((command.get("telemetry") or {}).get("env") or {})
    if not wanted:
        return {}
    out, skipped = {}, []
    for key, value in wanted.items():
        if os.environ.get(key):
            skipped.append(key)
            continue
        out[str(key)] = str(value)
    if skipped:
        log.info("telemetry: left %s alone — already set in this environment",
                 ", ".join(sorted(skipped)))
    return out


def _instrument_specs(root, specs: list, env: dict, project_id: str):
    """(specs, instrumented, why-not). Never raises; falls back to plain specs."""
    from src.qatest import appserver, provision

    if _instrumentation_refused(project_id):
        return specs, False, ("auto-instrumentation is disabled for this project after "
                              "it prevented a previous start")
    try:
        sidecar, why_not = provision.instrument(root, env)
        if not sidecar:
            return specs, False, why_not
        wrapped = [appserver.instrumented(spec, sidecar) for spec in specs]
        changed = any(a.command != b.command for a, b in zip(specs, wrapped))
        if not changed:
            return specs, False, ("nothing here can be auto-instrumented — a compose "
                                  "stack runs the app inside a container, and a Node "
                                  "dev server is not where the model calls happen")
        return wrapped, True, ""
    except Exception as exc:                                  # noqa: BLE001
        return specs, False, f"{type(exc).__name__}: {exc}"[:300]


def _instrumentation_flag(project_id: str):
    from src.qatest import provision
    return provision.workspace_root(project_id) / ".aura-otel-refused"


def _instrumentation_refused(project_id: str) -> bool:
    try:
        return _instrumentation_flag(project_id).is_file()
    except Exception:                                         # noqa: BLE001
        return False


def _remember_instrumentation_failure(project_id: str) -> None:
    """Sticky, so the next start does not repeat a doubled startup that already failed."""
    try:
        path = _instrumentation_flag(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat())
    except Exception as exc:                                  # noqa: BLE001
        log.debug("could not record the instrumentation opt-out: %s", exc)


def _command_emulator(command: dict, result: dict, progress=_NULL_PROGRESS) -> dict:
    """Start or stop this project's emulators, on request from DevMate.

    Project-scoped: named `aura-dev-<cloud>-<projectId>` so they are told apart from a
    run's own, and so a run that finds them adopts and leaves them rather than tearing
    down someone's working environment.
    """
    from src.qatest import emulators

    kind = command.get("kind")
    project_id = str(command.get("container") or "")
    clouds = [c for c in str(command.get("clouds") or "").split(",") if c]
    if not project_id:
        result["error"] = "no project"
        return result

    # Checked once, up front, and reported as its own stage. It is the single most
    # common reason a Start fails on a laptop — podman installed but its VM not started —
    # and finding out per cloud repeats the same message once per emulator.
    progress.step("Checking podman")
    ready, why = emulators.podman_ready()
    if not ready:
        result["error"] = why
        return result

    done, problems = [], []
    for cloud in clouds:
        known = emulators._BY_NAME.get(cloud)
        if not known:
            problems.append(f"unknown cloud {cloud}")
            continue
        name = emulators.dev_container(cloud)
        if kind == "emulator-stop":
            progress.step(f"Stopping {name}")
            # Shared: this stops the emulator for EVERY project on the machine, and
            # Floci keeps state in memory, so their resources go with it. The UI says so
            # before asking; this is the other half of that contract.
            ok, why = emulators.remove_container(name)
            (done if ok else problems).append(name if ok else f"{name}: {why}")
            continue

        # Start. The emulator is SHARED — one per cloud for the whole machine — so an
        # emulator already up is the answer, not a collision. This used to refuse,
        # because a per-project container could never get the fixed port a moment after
        # another project took it; projects are now separated by AWS account inside one
        # container instead, so attaching is correct.
        if emulators._ready(known.port, timeout=2):
            # An emulator already up skips all three of this cloud's stages at once, so
            # the bar still reaches `total` rather than stopping short of it and reading
            # as an unfinished job.
            progress.step(f"Fetching the {cloud} emulator image")
            progress.step(f"Starting {name}")
            progress.step(f"Waiting for {cloud} to answer on :{known.port}")
            holder = emulators._container_on_port(known.port).get("name", "")
            if not holder or holder.startswith(emulators.MANAGED_PREFIXES):
                progress.note(f"{holder or name}: already running — shared")
                done.append(f"{holder or name} (already running — shared)")
            else:
                # Something Aura did not start. Still refuse: Aura has no idea what it
                # is and must not hand a project's tests to it.
                problems.append(
                    f"port {known.port} is held by {holder}, which Aura did not start. "
                    f"Stop it first, or use it as-is by starting your app against "
                    f"http://localhost:{known.port} directly.")
            continue

        def _stage(label: str, _p=progress) -> None:
            """`start_container`'s way of saying which of its three phases it reached."""
            _p.step(label)

        ok, why = emulators.start_container(name, known, on_stage=_stage)
        (done if ok else problems).append(name if ok else f"{name}: {why}")

    result["ok"] = not problems
    result["output"] = json.dumps({"started" if kind == "emulator-start" else "stopped":
                                   done, "problems": problems})
    if problems:
        result["error"] = "; ".join(problems)[:400]
    return result


def _run_command(command: dict, allow_logs: bool) -> dict:
    """Carry out one command the server left for this runner.

    The server cannot touch this machine — it is on Fargate and this is a laptop with no
    inbound port — so anything it wants done here arrives as a parked command and is
    collected on the next poll. Four kinds:

      logs            `podman logs` for one container
      inventory       what is inside a running emulator, right now
      emulator-start  bring up this project's emulators (from DevMate)
      emulator-stop   take them down again
      app-populate    boot the app once so it creates its resources

    `app-populate` is the only one that takes minutes rather than seconds, and it is run
    on a worker thread by the caller for that reason — see `report_state_strict`.

    Every kind that names a container checks it against MANAGED_PREFIXES here as well as
    on the server. Both sides check because the failure mode — reading or killing
    arbitrary containers on a developer's machine — is severe and the check costs
    nothing.
    """
    from src.qatest import emulators

    kind = command.get("kind")
    result = {"id": command.get("id", ""), "output": "", "ok": False, "error": ""}

    if kind == "inventory":
        return _command_inventory(command, result)
    if kind in ("emulator-start", "emulator-stop"):
        return _command_emulator(command, result)
    if kind == "app-populate":
        return _command_populate(command, result)
    if kind == "app-start":
        return _command_app_start(command, result)
    if kind == "app-stop":
        return _command_app_stop(command, result)
    if kind == "app-status":
        return _command_app_status(command, result)
    if kind != "logs":
        result["error"] = f"unknown command {kind!r}"
        return result
    if not allow_logs:
        result["error"] = "this runner was started with --no-container-logs"
        return result

    ok, out = emulators.container_logs(command.get("container", ""),
                                       int(command.get("tail") or 200))
    result["ok"] = ok
    if ok:
        # Keep the TAIL. The recent lines are the ones being asked about.
        result["output"] = out[-LOG_MAX_BYTES:]
        result["truncated"] = len(out) > LOG_MAX_BYTES
    else:
        result["error"] = out
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.qatest.agent",
        description="Execute QualityMind runs queued from an Aura environment.")
    # NOT required: --doctor and --setup must work before a key or a URL exists, and
    # that is also the structural guarantee — the code path that installs things takes
    # no server input at all.
    parser.add_argument("--api", default="",
                        help="Aura base URL, e.g. https://aura-dev-….elb.amazonaws.com")
    parser.add_argument("--key", default=os.getenv("AURA_QA_RUNNER_KEY", ""),
                        help="gateway key (gw-…); defaults to $AURA_QA_RUNNER_KEY")
    # platform.node(), not os.uname(): os.uname does not exist on Windows, so every
    # Windows runner reported the literal name "runner" and they collided in an index
    # keyed by runner name.
    parser.add_argument("--name", default=platform.node() or "runner",
                        help="label shown in the UI")
    parser.add_argument("--once", action="store_true",
                        help="claim at most one run, then exit (useful in CI)")
    parser.add_argument("--poll", type=int, default=POLL_SECONDS,
                        help=f"seconds between polls (default {POLL_SECONDS})")
    parser.add_argument("--no-container-logs", action="store_true",
                        help="never send container output to Aura")
    parser.add_argument("--doctor", action="store_true",
                        help="check this machine can run tests, print what is wrong, "
                             "and exit. Needs no --api or --key.")
    parser.add_argument("--json", action="store_true",
                        help="with --doctor, print machine-readable output")
    parser.add_argument("--setup", action="store_true",
                        help="walk through fixing what --doctor found, one step at a "
                             "time, confirming each before it runs")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --setup, print every command and run nothing")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="start even when a prerequisite is missing; runs that "
                             "need it will report why")
    parser.add_argument("--report-all-containers", action="store_true",
                        help="report every podman container, not just Aura's own "
                             "(off by default: this machine's other containers are "
                             "nobody else's business)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    # Before the key check: diagnosing a machine must not require credentials.
    if args.doctor:
        return _doctor_command(args.json)
    if args.setup:
        from src.qatest.setup import run_setup
        return run_setup(dry_run=args.dry_run, api=args.api, key=args.key,
                         name=args.name)

    if not args.api:
        parser.error("--api is required to run the agent "
                     "(not for --doctor or --setup)")

    if not args.key:
        print("ERROR: no key. Pass --key gw-… or set AURA_QA_RUNNER_KEY.\n"
              "Mint one from the Aura UI with the tool label 'qa-runner'.",
              file=sys.stderr)
        return 2

    problems = _preflight()
    if problems:
        if not args.skip_preflight:
            print("ERROR: this machine cannot execute runs:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("\n  python -m src.qatest.agent --doctor    # full report",
                  file=sys.stderr)
            print("  python -m src.qatest.agent --setup     # fix them\n",
                  file=sys.stderr)
            return 2
        # Deliberately still refuses to be silent about it.
        for problem in problems:
            log.warning("starting anyway with a known problem: %s", problem)

    # A run's own telemetry goes back to the same Aura with the same credential, so
    # the runner needs no second endpoint and no second key. Published in the
    # environment rather than threaded as parameters for the reason `_apply_run_context`
    # gives: a subprocess or a library picks it up without every layer in between
    # having to know it exists.
    os.environ.setdefault(tracing.ENDPOINT_ENV, args.api)
    os.environ.setdefault(tracing.KEY_ENV, args.key)

    # Reconcile anything a previous life of this process left running, BEFORE the
    # first state report — so the panel's first sight of this machine is the truth.
    # `sweep` never kills what it cannot prove is ours; it reports and leaves it.
    try:
        from src.qatest import appsession
        for note in appsession.sweep():
            log.warning("app session: %s", note)
    except Exception as exc:                                  # noqa: BLE001
        log.debug("app session sweep skipped: %s", exc)

    # `appserver` starts children with start_new_session=True — deliberately, so that
    # stopping a dev server also stops the node processes npm leaves behind. The cost
    # is that the child is detached from this process group, so Ctrl-C here does NOT
    # reach it. That was invisible while every session lived inside a `with`; with
    # sessions that outlive the loop it is the default leak.
    import atexit
    import signal as _signal

    from src.qatest import appsession as _appsession
    atexit.register(_appsession.stop_all)

    def _bye(signum, _frame):
        log.info("stopping app sessions on signal %s", signum)
        _appsession.stop_all()
        raise SystemExit(0)

    for _sig in (_signal.SIGTERM, _signal.SIGINT):
        with contextlib.suppress(Exception):
            _signal.signal(_sig, _bye)

    client = Client(args.api, args.key, args.name)
    log.info("runner %r ready, polling %s every %ss", args.name, args.api, args.poll)
    allow_logs = not args.no_container_logs
    if not allow_logs:
        log.info("container logs disabled — Aura will show the podman command instead")

    polls = 0

    def report_state_strict(busy: str = "") -> None:
        """Send this machine's state and carry out anything the server asked for.

        Lets AuthRejected out. Only the startup call wants that — see `report_state`.
        """
        # Anything a populate thread finished since the last report rides out now.
        finished, _PENDING_RESULTS[:] = list(_PENDING_RESULTS), []
        reply = client.report_state({
            **_machine_state(busy, args.report_all_containers, allow_logs),
            **({"commandResults": finished} if finished else {})})
        _warn_if_stale(reply)
        for command in (reply or {}).get("commands") or []:
            log.info("  command  %s %s", command.get("kind"),
                     command.get("container", ""))
            if command.get("kind") == "app-populate":
                # Returns immediately; its result arrives on a later state POST.
                _start_populate(command)
                continue
            if command.get("kind") == "app-start":
                _start_app(command)
                continue
            if command.get("kind") == "app-stop":
                _stop_app(command)
                continue
            if command.get("kind") in ("emulator-start", "emulator-stop"):
                # Threaded like the rest, and for the same reason: a cold start pulls an
                # image and then waits for readiness, which together run past
                # RUNNER_STALE_S and used to take this machine offline in the panel while
                # it was doing exactly what it was asked.
                _start_emulator(command)
                continue
            client.report_state({**_machine_state(busy, args.report_all_containers,
                                                  allow_logs),
                                 "commandResults": [_run_command(command, allow_logs)]})

    def report_state(busy: str = "") -> None:
        """The in-loop form. A key revoked mid-life must not end the process here with
        a traceback: `claim` runs at the top of every iteration and reports it properly.
        """
        try:
            report_state_strict(busy)
        except AuthRejected as rejected:
            log.debug("state report rejected: %s", rejected)

    # Once at startup, so the panel fills immediately — and so a rejected key is found
    # here, before a single poll, rather than never.
    try:
        report_state_strict()
    except AuthRejected as rejected:
        print(rejected.advice(), file=sys.stderr)
        return 3

    while True:
        polls += 1
        # A populate has the app under test running on its port and is attached to this
        # project's emulator. A run claimed now would collide on both, so stand off and
        # say why — `busyRunId` is what makes the panel show the machine as occupied
        # rather than idle-but-unresponsive.
        # Emulator work counts too, now that it runs off the loop: a run claimed while a
        # container is being rebuilt under it would adopt an emulator about to go away.
        # Reporting every poll rather than every third is also what gives a progress bar
        # its ~5s granularity, so this is the same mechanism serving both.
        blocking = blocking_job()
        if blocking:
            report_state(blocking)
            time.sleep(args.poll)
            continue
        try:
            job = client.claim()
        except AuthRejected as rejected:
            # Terminal, unlike every other claim failure. Retrying a credential the
            # server has already refused cannot succeed, and doing it quietly every few
            # seconds is how a rotated key went unnoticed for four days.
            print(rejected.advice(), file=sys.stderr)
            return 3
        except Exception as exc:                              # noqa: BLE001
            # A redeploy, a dropped connection, a 5xx — all genuinely transient, and the
            # runner should ride them out rather than needing a restart.
            log.warning("claim failed: %s", exc)
            job = None

        if job:
            report_state(job.get("runId", ""))
            run_one(client, job)
            # Immediately after a run, so the panel loses the containers it just
            # stopped rather than showing them for another 15 seconds.
            report_state()
            if args.once:
                return 0
        elif args.once:
            log.info("nothing queued")
            return 0
        else:
            if polls % STATE_EVERY_N_POLLS == 0:
                report_state()
            time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())

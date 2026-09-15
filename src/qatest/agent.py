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
import json
import logging
import os
import platform
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger("qa-runner")

POLL_SECONDS = 5
#: Heartbeat at most this often. Every step would be a request per navigation.
HEARTBEAT_MIN_INTERVAL_S = 10

#: What this agent understands. Sent on every request so the server knows whether it
#: may include newer fields inside the `cases` it ships — an older agent splats those
#: straight into `Case(**c)` and dies on an unknown key, uncaught, inside its poll loop.
PROTOCOL = 2

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
    from src.qatest import doctor, emulators

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
    }


def _command_inventory(command: dict, result: dict) -> dict:
    """What is inside a running emulator, right now.

    Uses the SAME collector the end-of-run snapshot uses, so the live drawer and the
    stored report can never disagree about what a resource looks like. Output is JSON
    because the server parks it in S3 verbatim and the UI renders it structured.
    """
    from src.qatest import emulators, inventory

    cloud = str(command.get("container") or "")      # the command's free-text slot
    known = emulators._BY_NAME.get(cloud)
    if not known:
        result["error"] = f"unknown cloud {cloud!r}"
        return result
    if not emulators._ready(known.port, timeout=2):
        result["error"] = (f"nothing is answering on :{known.port} — the {cloud} "
                           f"emulator is not running")
        return result

    found = inventory.collect({cloud: known.env()[_ENDPOINT_VAR[cloud]]})
    result["ok"] = True
    result["output"] = json.dumps(found)
    return result


#: The populate thread, if one is running. A list rather than a bare global so the poll
#: loop can test it without a lock: append and clear are atomic enough for a flag whose
#: only reader asks "is something running".
_POPULATING: list = []


#: Results from finished populate threads, waiting for the next state POST to carry them.
_PENDING_RESULTS: list = []


def populate_in_flight() -> str:
    """The project id of a populate currently running, or "" — read by the poll loop."""
    return _POPULATING[0][0] if _POPULATING else ""


def _start_populate(command: dict) -> None:
    """Run a populate on a worker thread, and return at once.

    Commands are normally executed INLINE on the poll loop. That is fine for the others,
    which take milliseconds, and fatal for this one: a first populate installs the
    project's dependencies, and while the loop is blocked nothing reports state. After
    RUNNER_STALE_S (90s) the row goes stale and the panel stops finding an online runner
    of its own — so it swaps itself for "No runner is connected" and unmounts the poller
    watching this very command. The work is still running; the screen says nothing is.

    So: thread it, let the loop keep heartbeating, and hand the result to whichever state
    POST comes after it finishes.
    """
    import threading

    project_id = str(command.get("container") or "")
    if _POPULATING:
        _PENDING_RESULTS.append({
            "id": command.get("id", ""), "output": "", "ok": False,
            "error": f"already populating {_POPULATING[0][0]} on this machine"})
        return

    def body() -> None:
        result = {"id": command.get("id", ""), "output": "", "ok": False, "error": ""}
        try:
            result = _command_populate(command, result)
        except Exception as exc:                              # noqa: BLE001
            # Never let a thread die silently: the reader is watching a spinner that
            # would otherwise run to its timeout and blame the runner for going quiet.
            result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        finally:
            _PENDING_RESULTS.append(result)
            _POPULATING.clear()

    thread = threading.Thread(target=body, name=f"populate-{project_id}", daemon=True)
    _POPULATING.append((project_id, thread))
    thread.start()


def _command_populate(command: dict, result: dict) -> dict:
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
    stale = provision.prepare(project_id, command.get("payload") or {}, None)
    problems: list[str] = []

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
    specs = [sp for sp in appserver.detect(root) if sp.kind == "api" or sp.compose]
    if not specs:
        result["error"] = (f"no runnable application found in {root} — there is nothing "
                           f"here whose startup could create cloud resources")
        return result

    # The emulator must be THIS project's. `_ready` alone is satisfied by another
    # project's emulator on the same fixed port, and populating someone else's is the
    # worst failure available here.
    env: dict[str, str] = {}
    endpoints: dict[str, str] = {}
    for cloud in clouds:
        known = emulators._BY_NAME.get(cloud)
        if not known:
            problems.append(f"unknown cloud {cloud}")
            continue
        expected = emulators.dev_container(cloud, project_id)
        if not emulators._ready(known.port, timeout=2):
            problems.append(f"the {cloud} emulator is not running — press Start first")
            continue
        holder = emulators._container_on_port(known.port).get("name", "")
        if holder != expected:
            problems.append(
                f"port {known.port} is held by {holder or 'another process'}, not this "
                f"project's emulator — populating it would write into someone else's")
            continue
        env.update(known.env())
        endpoints[cloud] = known.env()[_ENDPOINT_VAR[cloud]]

    if not endpoints:
        result["error"] = "; ".join(problems)[:400] or "no usable emulator for this project"
        return result

    try:
        with appserver.RunningApps(specs, extra_env=env) as apps:
            # Nothing to do in the body. `__enter__` has already waited for the app to
            # answer, which means its startup — and therefore its provisioning — is done.
            problems.extend(f"{spec.name}: {why}" for spec, why in apps.failures)
    except Exception as exc:                                  # noqa: BLE001
        result["error"] = f"the app did not start: {type(exc).__name__}: {exc}"[:400]
        return result

    found = inventory.collect(endpoints)
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

    result["ok"] = not problems
    result["output"] = json.dumps({"resources": found, "created": total,
                                   "problems": problems, "warnings": warnings})
    if problems:
        result["error"] = "; ".join(problems)[:400]
    return result


def _command_emulator(command: dict, result: dict) -> dict:
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

    done, problems = [], []
    for cloud in clouds:
        known = emulators._BY_NAME.get(cloud)
        if not known:
            problems.append(f"unknown cloud {cloud}")
            continue
        name = emulators.dev_container(cloud, project_id)
        if kind == "emulator-stop":
            ok, why = emulators.remove_container(name)
            (done if ok else problems).append(name if ok else f"{name}: {why}")
            continue

        # Start. Refuse rather than collide: the ports are fixed, so something already
        # there is either this project's emulator (already running — nothing to do) or
        # another project's, which the operator has to stop first.
        if emulators._ready(known.port, timeout=2):
            holder = emulators._container_on_port(known.port).get("name", "")
            if holder == name:
                done.append(f"{name} (already running)")
            else:
                problems.append(
                    f"port {known.port} is already held by {holder or 'another process'}. "
                    f"Floci's ports are fixed, so only one {cloud} emulator can run at a "
                    f"time — stop that one first.")
            continue
        ok, why = emulators.start_container(name, known)
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
        for command in (reply or {}).get("commands") or []:
            log.info("  command  %s %s", command.get("kind"),
                     command.get("container", ""))
            if command.get("kind") == "app-populate":
                # Returns immediately; its result arrives on a later state POST.
                _start_populate(command)
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
        populating = populate_in_flight()
        if populating:
            report_state(f"populate:{populating}")
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

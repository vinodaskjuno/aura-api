"""QA Workspace API — test generation, execution, results, activity."""
from __future__ import annotations
import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from fastapi import (APIRouter, Body, Depends, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from pydantic import BaseModel, Field
from src.routers.auth import get_current_user, require_permission
from src.database.dynamo_client import put_item, get_item, get_item_by_pk, scan_items, update_item
from src.storage.s3_client import get_json, list_objects, presigned_url

router = APIRouter(prefix="/api/qa", tags=["qa"])

log = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────────────────────

# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/projects")
def list_qa_projects(user: dict = Depends(require_permission("qa_workspace"))):
    """List all analyzed projects available for QA."""
    all_projects = scan_items("projects", limit=200)
    # Accept both legacy lowercase and current uppercase status values
    _ok = {"analyzed", "active", "ANALYSED", "TESTING_IN_PROGRESS", "TESTING_COMPLETE",
           "CODE_CHANGES_DONE", "CODE_CHANGES_IN_PROGRESS"}
    return [p for p in all_projects if p.get("status") in _ok]


@router.get("/projects/{project_id}/suites")
def get_test_suites(project_id: str, user: dict = Depends(require_permission("qa_workspace"))):
    """Test runs for a project: executed runs from S3, generation runs from DynamoDB.

    The S3 side is authoritative for anything QualityMind executed and is listed by
    key prefix, so it is neither scanned nor capped. The DynamoDB scan remains only
    for TestGenerationAgent output, which has no S3 report — `projectId` is the SORT
    key of `test-results`, so filtering on it needs either a scan or a new GSI.
    Merging the two means a run past the scan's limit is still listed, which it was
    not before.
    """
    from src.qatest import evidence

    runs: list[dict] = []
    seen: set[str] = set()

    for run_id in evidence.list_runs(project_id):
        report = evidence.read_report(project_id, run_id)
        if not report:
            continue
        seen.add(run_id)
        runs.append({
            "testRunId": run_id, "projectId": project_id,
            "type": "qatest", "status": report.get("status"),
            "totalPassed": report.get("totalPassed", 0),
            "totalFailed": report.get("totalFailed", 0),
            "totalSkipped": report.get("totalSkipped", 0),
            "appUrl": report.get("appUrl", ""),
            "createdAt": report.get("startedAt", ""),
            "completedAt": report.get("completedAt", ""),
            "hasEvidence": True,
        })

    for row in scan_items("test-results", limit=500):
        if row.get("projectId") == project_id and row.get("testRunId") not in seen:
            runs.append(row)

    runs.sort(key=lambda x: str(x.get("createdAt") or ""), reverse=True)
    return runs


@router.get("/runs/{run_id}")
def get_run_detail(run_id: str, user: dict = Depends(require_permission("qa_workspace"))):
    """Get full detail of a test run."""
    # query_items on the partition key, not a table scan: the previous
    # scan_items(limit=500) both cost a full scan per request and made any run past
    # the first 500 items unfindable.
    run = get_item_by_pk("test-results", run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _s3_key(uri: str) -> str:
    """Strip the bucket prefix off an s3:// URI to get the object key.

    The literal "s3://aura-test-artifacts/" never matched: s3_client._bucket()
    resolves to "aura-<accountId>-test-artifacts", so the whole s3:// URI was being
    passed to presigned_url as a key and every artifact link 404'd. Derive the real
    bucket name instead of hardcoding it.
    """
    if not uri.startswith("s3://"):
        return uri
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return key or rest


@router.get("/runs/{run_id}/artifacts")
def get_run_artifacts(run_id: str, user: dict = Depends(require_permission("qa_workspace"))):
    """Presigned URLs for everything a run produced.

    Two sources, and the second was missing — which is why the Artifacts tab said "No
    artifacts for this run" for every real test run.

    The legacy agents record an `artifacts` list of S3 URIs on the DynamoDB row.
    `src/qatest` does not: its evidence goes to S3 under `{projectId}/{runId}/` and the
    index row carries no artifact list at all. So the tab read an empty field and
    reported emptiness, while the screenshots sat in the bucket.
    """
    from src.storage import s3_client

    run = get_item_by_pk("test-results", run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    result = []
    for uri in run.get("artifacts", []):
        key = _s3_key(uri)
        try:
            url = presigned_url("test-artifacts", key, expires=3600)
            result.append({"key": key, "url": url, "filename": key.split("/")[-1]})
        except Exception:                                      # noqa: BLE001
            result.append({"key": key, "url": uri, "filename": key.split("/")[-1]})
    if result:
        return result

    # Fall back to the run's evidence prefix. S3 is the source of truth for a qatest
    # run, so listing it finds screenshots, the step log and the report.
    project_id = run.get("projectId", "")
    if not project_id:
        return []
    prefix = f"{project_id}/{run_id}/"
    try:
        objects = s3_client.list_objects("test-artifacts", prefix) or []
    except Exception as exc:                                   # noqa: BLE001
        log.warning("QA artifacts: could not list %s: %s", prefix, exc)
        return []

    for obj in sorted(objects, key=lambda o: o.get("key", "")):
        key = obj.get("key", "")
        # The shipped working copy is an implementation detail, not evidence.
        if not key or "/_workspace/" in key:
            continue
        try:
            url = presigned_url("test-artifacts", key, expires=3600)
        except Exception:                                      # noqa: BLE001
            continue
        result.append({"key": key, "url": url,
                       "filename": key.split("/")[-1],
                       "size": obj.get("size", 0)})
    return result


@router.get("/activity")
def get_qa_activity(user: dict = Depends(require_permission("qa_workspace"))):
    """QA activity for this user.

    `test_generation_agent` and `test_execution_agent` are kept in the filter for
    HISTORY only: the LLM test-generation flow that drove them has been removed, so
    nothing writes new rows under those names. They stay because deleting them would
    hide runs a user really did make, and re-adding a name later is worse than
    carrying two dead strings with a comment saying so.
    """
    all_activity = scan_items("activity", limit=500)
    qa_agents = {"test_generation_agent", "test_execution_agent", "qatest"}
    relevant = [
        a for a in all_activity
        if a.get("userId") == user["userId"] and a.get("agent") in qa_agents
    ]
    relevant.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return relevant[:100]


# ── Local test execution (podman emulators + Playwright, evidence in S3) ─────
#
# Runs execute where podman and a browser are available — a developer machine or CI —
# not in the deployed task. The deployed API can still SHOW any run, because evidence
# lives in the shared S3 bucket rather than on whichever machine produced it.
#
# What this replaced: an ECS run_task/exec/stop_task cycle per run that dispatched to
# a Lambda named `aura-test-runner` which was never deployed, so every run from a
# deployed environment failed.

#: What this server sends. Anything newer inside `cases[]` is stripped for agents that
#: predate it, because an older agent splats those into `Case(**c)` and dies on an
#: unknown key — uncaught, inside its poll loop, killing every runner at once.
RUNNER_PROTOCOL = 2

#: The same bucket evidence.py writes runs to, so a log and a run live together and
#: one lifecycle rule covers both.
_ARTIFACT_BUCKET = "test-artifacts"

#: Hard cap on a stored log body, matching the agent's own. A chatty emulator must not
#: be able to push megabytes through the API on someone else's behalf.
_LOG_MAX_BYTES = 128 * 1024


def _slug(value: str) -> str:
    """A runner name as a safe S3 path segment. `_runner_identity` returns
    `username/tool_label`, so it contains a slash and cannot be used raw."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "runner"

#: The fields a protocol-1 agent knows how to receive.
_LEGACY_CASE_FIELDS = ("case_id", "kind", "name", "verifies_label", "verifies_eid",
                       "method", "path", "source_file")


def _runner_protocol(request: Request) -> int:
    try:
        return int(request.headers.get("X-Aura-Runner-Protocol") or 1)
    except ValueError:
        return 1


def _cases_for_protocol(cases: list, protocol: int) -> list[dict]:
    out = [c if isinstance(c, dict) else c.__dict__ for c in cases]
    if protocol >= RUNNER_PROTOCOL:
        return out
    return [{k: v for k, v in c.items() if k in _LEGACY_CASE_FIELDS} for c in out]


class LocalRunRequest(BaseModel):
    project_id: str
    # Empty starts the project's own application from its working copy; a value
    # targets something already running.
    app_url: str = ""
    run_id: str | None = None
    exploratory: bool = False


RUNNER_STALE_S = 90


def _online_runners() -> list[dict]:
    """Self-hosted runners that have polled within RUNNER_STALE_S.

    Liveness comes from the POLL, not from claimed runs. Deriving it from run rows
    deadlocked: a runner that had never claimed anything looked offline, so canRun was
    false, so the button was disabled, so nothing was queued, so it never claimed. Found
    on dev with a runner that was demonstrably connected and polling every 5 seconds.
    """
    from src.qatest import queue

    return queue.online_runners(RUNNER_STALE_S)


def _runner_health() -> list[dict]:
    """Connected runners that reported something wrong with themselves."""
    from src.qatest import queue

    out = []
    for runner in queue.list_runner_state():
        if not runner.get("online"):
            continue
        health = runner.get("health") or {}
        blocking = [f for f in (health.get("findings") or [])
                    if f.get("severity") == "blocks"]
        out.append({"name": runner.get("name", ""), "blocking": blocking})
    return out


@router.get("/capabilities")
def qa_capabilities(user: dict = Depends(require_permission("qa_workspace"))):
    """Whether a run can execute AT ALL, and why not when it cannot.

    This used to answer a narrower question — whether THIS process has podman and a
    browser — which in a deployed Fargate task is permanently false. Correct, but it
    meant the button could never be enabled in a deployed environment no matter what
    was actually available.

    Now either answer is enough: this process can run it (a developer running the
    backend locally), OR a self-hosted runner is online and will pick it up. The local
    check is kept as the first branch so the local experience is unchanged.
    """
    from src.qatest.emulators import CLOUDS, podman_available
    from src.qatest.runner import _playwright_available

    podman = podman_available()
    browser, browser_why = _playwright_available()
    local = podman and browser
    runners = _online_runners()

    commands: list[str] = []
    if local:
        reason = ""
    elif runners:
        reason = ""
        # A runner is connected but cannot actually run anything. Say so — and do NOT
        # set canRun false: disabling the button recreates the deadlock this endpoint's
        # docstring exists to describe (nothing queued, so nothing ever claimed).
        broken = [r for r in _runner_health() if r.get("blocking")]
        if broken:
            first = broken[0]
            reason = (f"{first['name']} is connected but not ready: "
                      f"{first['blocking'][0]['title']}.")
            commands = [c for c in first["blocking"][0].get("remedy") or []]
    else:
        why = "; ".join(x for x in [
            "" if podman else "podman is not available here",
            "" if browser else browser_why,
        ] if x)
        reason = (f"{why} — and no self-hosted runner is connected. Start one on a "
                  f"machine that has podman and Chromium:\n"
                  f"  python -m src.qatest.agent --api <this-host> --key gw-…")
        commands = ["python -m src.qatest.agent --api <this-host> --key gw-…",
                    "python -m src.qatest.agent --doctor",
                    "python -m src.qatest.agent --setup"]

    return {
        "canRun": bool(local or runners),
        "podman": podman,
        "browser": browser,
        "local": local,
        "runners": runners,
        "reason": reason,
        # Structured, because the panel used to SCRAPE `reason` for the first line
        # starting with `python -m` — a heuristic that breaks the moment the prose
        # changes, which it just did.
        "commands": commands,
        "clouds": [{"name": c.name, "port": c.port, "image": c.image} for c in CLOUDS],
    }


@router.post("/run/local")
async def run_local(req: LocalRunRequest,
                    user: dict = Depends(require_permission("qa_workspace"))):
    """Plan from the graph, start only the emulators the project needs, run, store."""
    from src.qatest.service import execute

    def on_event(event: dict) -> None:
        # This path had no consumer at all, so a synchronous local run produced no
        # trace anywhere while it worked. The WebSocket path streams; this one at least
        # logs.
        log.info("qa run %s: %-9s %s", req.run_id or "-", event.get("type", ""),
                 str(event.get("message") or "")[:160])

    report = await asyncio.to_thread(
        lambda: execute(req.project_id, req.app_url, req.run_id,
                        user["username"], req.exploratory,
                        on_event=on_event, kinds=req.kinds))
    return report


# ── Queued runs and the self-hosted runner ───────────────────────────────────
#
# A run is enqueued here and executed somewhere else — a developer machine with podman,
# Chromium and the project's cloned working copy. Fargate has none of those, and the
# working copy is the one that cannot be solved by provisioning bigger compute.
#
# The runner reaches only these endpoints, over HTTPS, with a `gw-` key. It never touches
# Neo4j (private subnet) and holds no long-lived AWS credentials. Planning and graph
# write-back stay here; the runner just executes and uploads.


class EnqueueRequest(BaseModel):
    project_id: str
    app_url: str = ""
    exploratory: bool = False
    kinds: list[str] = Field(default_factory=list)


class HeartbeatRequest(BaseModel):
    phase: str = ""
    # Live pass/fail/skip. Without them a running remote run can only say "running",
    # and a twelve-case run looks identical to one that has hung.
    totalPassed: int = 0
    totalFailed: int = 0
    totalSkipped: int = 0
    totalUnemulated: int = 0
    totalCases: int = 0
    stepIndex: int = 0
    #: What the run is doing right now, in words — "aws emulator ready on :4566"
    #: beats the bare phase name.
    phaseDetail: str = ""
    #: The Floci containers serving this run, as the runner sees them. THIS is what
    #: makes the emulator panel live rather than only appearing in the stored report
    #: after the run is over — pydantic drops any field not declared here, so an
    #: omission is silent and total.
    emulators: list[dict] = Field(default_factory=list)
    #: What has happened since the last heartbeat — one entry per event, in order.
    #: `phaseDetail` carries only the CURRENT line, so without this a remote run has
    #: no history at all and the reader sees a single sentence that keeps changing.
    events: list[dict] = Field(default_factory=list)


class FinishRequest(BaseModel):
    report: dict


#: A hostname is 253 characters at the outside and a display label wants far less.
_MACHINE_MAX = 64


def _machine_name(request: Request) -> str:
    """The runner's own name for its machine, as the agent reports it.

    The agent has always sent this and the server has always discarded it, so the only
    name any screen could show was the synthetic `username/tool_label` identity — which
    is not what anyone calls their laptop.

    Sanitised rather than trusted: it is written by whoever holds the gateway key and is
    rendered in front of every other user of the deployment.
    """
    raw = str(request.headers.get("X-Aura-Runner-Name") or "")[:_MACHINE_MAX]
    return "".join(ch for ch in raw if ch >= " " and ch != "\x7f").strip()


def _runner_identity(request: Request) -> tuple[str, dict]:
    """Authenticate a runner by gateway key. Returns its label and who owns it.

    Reuses the same credential path as the model gateway and the OTLP receiver, so a key
    means the same thing everywhere and there is one place to revoke it. Both helpers
    RAISE HTTPException(401) rather than returning falsy.

    The label stays `username/tool_label`: it is the partition key of the runner's row,
    so changing its shape would orphan every runner record in the table. The owner and
    the machine name come back BESIDE it, structured, rather than being recovered later
    by splitting the label on its slash.
    """
    from src.services.gateway_service import extract_credential, resolve_credential

    user = resolve_credential(extract_credential(request))
    if "qa_workspace" not in (user.permissions or []):
        raise HTTPException(status_code=403,
                            detail="this key's role lacks qa_workspace")
    label = f"{user.username}/{getattr(user, 'tool_label', '') or 'qa-runner'}"
    return label, {"owner": user.username,
                   "ownerId": user.user_id,
                   "machine": _machine_name(request)}


@router.post("/runs", status_code=202)
def enqueue_run(body: EnqueueRequest,
                user: dict = Depends(require_permission("qa_workspace"))):
    """Queue a run and return at once. 202, because nothing has executed yet.

    Fire-and-forget on purpose: a remote run takes minutes, and the previous
    synchronous WebSocket bound the run's lifetime to a browser tab.
    """
    from src.qatest import queue

    row = queue.enqueue(body.project_id, body.app_url, user.get("username", ""),
                        body.exploratory)
    if body.kinds:
        # Recorded on the row so the claim can filter, and so a reader of a finished
        # run can tell a deliberately partial run from a project with three endpoints.
        update_item("test-results",
                    {"testRunId": row["testRunId"], "projectId": row["projectId"]},
                    {"kinds": list(body.kinds)})
    return {"runId": row["testRunId"], "projectId": row["projectId"],
            "status": row["status"], "kinds": list(body.kinds)}


@router.get("/projects/{project_id}/plan")
def get_plan_preview(project_id: str, refresh: bool = Query(False),
                     _: dict = Depends(require_permission("qa_workspace"))):
    """What a run WOULD do, so the launcher can offer a choice before starting one.

    Reports `graphReady: false` with a reason rather than an empty plan: "0 cases" and
    "this project was never analysed" look identical otherwise, and only one of them is
    something the user can act on.
    """
    from src.qatest import plan

    return plan.preview(project_id, refresh=refresh)


@router.get("/projects/{project_id}/coverage")
def get_project_coverage(project_id: str,
                         _: dict = Depends(require_permission("qa_workspace"))):
    """Coverage from this project's most recent run with evidence."""
    from src.qatest import evidence

    # list_runs returns run IDS, newest first — not row dicts, and it takes no limit.
    # Capped here instead: the first run with a readable report wins, and walking every
    # run a project ever had to find it would be a read per run.
    for run_id in (evidence.list_runs(project_id) or [])[:10]:
        report = evidence.read_report(project_id, run_id)
        if not report:
            continue
        coverage = _coverage_for(project_id, report)
        if coverage:
            return {"projectId": project_id, "runId": report.get("runId", ""),
                    "ranAt": report.get("startedAt", ""), "coverage": coverage}
    return {"projectId": project_id, "runId": "", "ranAt": "", "coverage": None}


def _coverage_for(project_id: str, report: dict) -> dict | None:
    """A report's coverage block, computed on read when it predates the feature.

    Stored on new reports at finish; recomputed here for older ones so the Coverage tab
    is not blank for every run that already exists.
    """
    if report.get("coverage"):
        return report["coverage"]
    try:
        from src.qatest import coverage as cov
        from src.qatest import evidence, plan
        from src.qatest.types import Report, Step

        run_id = report.get("runId", "")
        rebuilt = Report.from_dict(report)
        if not rebuilt.cases:
            return None
        steps = [Step(index=int(s.get("index") or 0), action=s.get("action", ""),
                      target=s.get("target", ""), status=s.get("status", "skipped"),
                      case_id=s.get("caseId", ""))
                 for s in (evidence.read_steps(project_id, run_id) or [])]
        totals = plan.graph_totals(plan.fetch_facts(project_id))
        return cov.summarise(rebuilt, steps, totals)
    except Exception as exc:                                  # noqa: BLE001
        log.debug("qa: could not compute coverage for %s: %s", report.get("runId"), exc)
        return None


@router.get("/runs/{run_id}/progress")
def get_run_progress(run_id: str, projectId: str = Query(...),
                     _: dict = Depends(require_permission("qa_workspace"))):
    """One run's live counters. A GetItem, deliberately.

    This is what a 2-second poll hits. `list_for_project` scans the table, and a polled
    endpoint backed by a scan is how a table gets hot.
    """
    from src.qatest import queue

    row = queue.progress(run_id, projectId)
    if row is None:
        raise HTTPException(404, "no such run")
    return row


# ── The runner's own machine ──────────────────────────────────────────────────
#
# The API runs on Fargate and can never see a developer's podman, so everything these
# endpoints report is second-hand — and says how old it is.

@router.post("/runner/state")
def post_runner_state(body: dict = Body(...), request: Request = None):  # noqa: B008
    """A runner reporting what it is running, and collecting any pending command.

    A separate endpoint rather than a richer `/runner/next` response ON PURPOSE: that
    call answers 204 when idle and deployed agents treat any 200 as a job. A 200
    without a `runId` would raise KeyError inside their poll loop and kill every runner
    at once.
    """
    from src.qatest import queue

    runner, identity = _runner_identity(request)

    # Results first: the runner may be handing back the output of the command it was
    # given last time.
    for result in (body.get("commandResults") or [])[:4]:
        _store_command_result(runner, result)

    queue.record_runner_state(runner, body, identity)
    command = queue.take_command(runner)
    return {"ok": True,
            "pollSeconds": 15,
            "commands": [command] if command else []}


def _store_command_result(runner: str, result: dict) -> None:
    """Park a command's output in S3 and note where it went.

    S3 rather than DynamoDB: the runner row is rewritten every 15 seconds and a log
    body would blow past the 400 KB item limit. The API does the write because an IDLE
    runner holds no AWS credentials at all — they are minted per run, scoped to that
    run's prefix.
    """
    from src.qatest import queue
    from src.storage.s3_client import put_object

    command_id = str(result.get("id") or "")
    if not command_id:
        return
    if not result.get("ok"):
        queue.record_command_result(runner, command_id,
                                    error=str(result.get("error") or "")[:400])
        return
    body = str(result.get("output") or "")[-_LOG_MAX_BYTES:]
    key = f"_runners/{_slug(runner)}/logs/{command_id}.log"
    try:
        put_object(_ARTIFACT_BUCKET, key, body.encode("utf-8"), "text/plain")
        queue.record_command_result(runner, command_id, key=key)
    except Exception as exc:                                  # noqa: BLE001
        queue.record_command_result(runner, command_id, error=str(exc)[:400])


@router.get("/runners")
def list_runners(user: dict = Depends(require_permission("qa_workspace"))):
    """Every known runner, with the Floci containers it last reported.

    `stale` is the field that matters: a sleeping laptop must not leave a panel
    claiming four emulators are running.

    EVERY runner, not just this user's. The queue has no affinity — a teammate's machine
    claiming your run is normal — so hiding theirs would leave the reader unable to
    explain where their own run went. `you` is returned instead, so the UI can say whose
    machine it is from one server-stated fact rather than inferring it.
    """
    from src.qatest import emulators, queue

    return {"runners": queue.list_runner_state(),
            "you": user.get("username", ""),
            "staleAfterSeconds": queue.RUNNER_STALE_S,
            "clouds": [{"name": c.name, "port": c.port, "image": c.image}
                       for c in emulators.CLOUDS]}


class LogsRequest(BaseModel):
    runner: str
    container: str
    tail: int = 200


@router.post("/runners/logs")
def request_container_logs(body: LogsRequest,
                           _: dict = Depends(require_permission("qa_workspace"))):
    """Ask a runner for a container's output. Answered on its next poll.

    Not a stream, and the UI must not call it one — the runner has no inbound port by
    design, so this is a round trip with one poll interval of latency.
    """
    from src.qatest import queue

    if not body.container.startswith(queue.MANAGED_PREFIX):
        # Enforced here as well as on the runner. The failure mode — reading arbitrary
        # container output off someone's laptop — is severe enough to check twice.
        raise HTTPException(400, "logs are only available for containers Aura started")
    tail = max(1, min(int(body.tail or 200), 500))
    return queue.request_command(body.runner, "logs", body.container, tail)


@router.get("/runners/logs/{command_id}")
def get_container_logs(command_id: str, runner: str = Query(...),
                       _: dict = Depends(require_permission("qa_workspace"))):
    """The output of a log request, once the runner has answered."""
    from src.qatest import queue
    from src.storage.s3_client import get_object

    record = queue.command_result(runner, command_id)
    if not record:
        raise HTTPException(404, "no such log request")
    if record.get("error"):
        return {"status": "failed", "error": record["error"],
                "container": record.get("container", "")}
    if not record.get("resultKey"):
        return {"status": "pending", "container": record.get("container", ""),
                "requestedAt": record.get("requestedAt", "")}
    try:
        raw = get_object(_ARTIFACT_BUCKET, record["resultKey"]) or b""
    except Exception as exc:                                  # noqa: BLE001
        raise HTTPException(502, f"could not read the stored log: {exc}")
    text = raw.decode("utf-8", errors="replace")
    return {"status": "ready", "container": record.get("container", ""),
            "fetchedAt": record.get("resultAt", ""),
            "truncated": len(raw) >= _LOG_MAX_BYTES,
            "lines": text.splitlines()}


@router.get("/results/{project_id}/{run_id}/console")
def get_run_console(project_id: str, run_id: str,
                    _: dict = Depends(require_permission("qa_workspace"))):
    """Browser console errors and failed requests captured during a run."""
    from src.storage.s3_client import get_object

    try:
        raw = get_object(_ARTIFACT_BUCKET, f"{project_id}/{run_id}/console.log") or b""
    except Exception:                                         # noqa: BLE001
        raw = b""
    return {"lines": raw.decode("utf-8", errors="replace").splitlines()}


@router.get("/runner/next")
def runner_next(request: Request):
    """Claim the oldest queued run. 204 when there is nothing to do.

    Returns the PLAN as well as the run, because the runner cannot reach the knowledge
    graph. Also returns short-lived scoped credentials so `evidence.py` can upload
    without modification — see src/qatest/credentials.py for that trade-off.
    """
    from fastapi.responses import Response

    from src.qatest import (appserver, credentials, emulators, plan, queue,
                            workspace)

    runner, identity = _runner_identity(request)
    # Before claiming, and unconditionally: a poll that finds nothing is still proof
    # that this runner is alive, and it is the ONLY proof available before it has ever
    # picked up work.
    #
    # The identity rides along because this is the call EVERY runner makes every few
    # seconds, including a protocol-1 agent that never reports state — so labelling here
    # is the only placement that covers all of them.
    queue.touch_runner(runner, identity)

    row = queue.claim(runner, identity)
    if not row:
        return Response(status_code=204)

    project_id = row["projectId"]
    run_id = row["testRunId"]

    # Planned HERE, against Neo4j, and shipped with the claim.
    facts = plan.fetch_facts(project_id)
    # Planned WITH the working copy, so file checks are included. The API can read it
    # (the workspace volume is mounted here); the runner receives them in the claim
    # like any other case and needs no new protocol.
    plan_root, _checked = appserver.locate(project_id)
    cases = plan.build_plan(project_id, facts, root=plan_root)
    clouds = [c.name for c in emulators.clouds_for(facts.get("dependencies") or [])]

    # Filtered HERE as well as inside service.execute. This is the pass that matters
    # for compatibility: an agent that has never heard of `kinds` simply receives a
    # shorter list and cannot tell the difference. filter_by_kind is idempotent, so
    # applying it twice is harmless.
    kinds = list(row.get("kinds") or [])
    cases = plan.filter_by_kind(cases, kinds)

    # So a queued run stops reporting an unknown plan size the moment it is claimed.
    queue.heartbeat(run_id, project_id, "claimed", runner,
                    {"totalCases": len(cases)})

    return {
        "runId": run_id,
        "projectId": project_id,
        "appUrl": row.get("appUrl", ""),
        "exploratory": bool(row.get("exploratory")),
        "ranBy": row.get("userId", ""),
        "kinds": kinds,
        "cases": _cases_for_protocol(cases, _runner_protocol(request)),
        "clouds": clouds,
        "planTotal": len(plan.build_plan(project_id, facts)),
        "credentials": credentials.mint(project_id, run_id),
        # Aura's own copy of the code, so the runner does not need the project cloned
        # on it. Only packaged when there is no explicit app_url — pointing a run at a
        # running instance needs no working copy at all, and shipping one would be
        # pure latency.
        "workspace": (None if row.get("appUrl")
                      else workspace.publish(project_id)),
    }


@router.post("/runner/{run_id}/heartbeat")
def runner_heartbeat(run_id: str, request: Request, projectId: str = Query(...),
                     phase: str = Query(""),
                     body: HeartbeatRequest = Body(...)):
    """Progress, and the signal that keeps the reaper away from this run.

    `phase` stays a query parameter as well as a body field, for a runner that predates
    the body. The counts arrive in the body.

    The body is REQUIRED, not `HeartbeatRequest | None = None`. That spelling looks
    like an optional body and is not one: FastAPI omitted the request body from the
    route entirely, so the endpoint accepted the POST, answered 200, and silently
    discarded every count — and zero counts are indistinguishable from a run that has
    not started a case yet, so nothing surfaced. There is a test on the OpenAPI schema.
    """
    from src.qatest import queue

    runner, identity = _runner_identity(request)
    queue.heartbeat(run_id, projectId, phase or body.phase, runner, body.model_dump(),
                    identity)
    return {"ok": True}


@router.post("/runner/{run_id}/finish")
def runner_finish(run_id: str, body: FinishRequest, request: Request,
                  projectId: str = Query(...)):
    """Record the terminal state and do the graph write-back.

    Write-back happens HERE, not on the runner: `graph_writeback` needs Neo4j. The
    report and steps are read back out of S3, which is exactly what `service.execute`
    does when it writes the graph itself.
    """
    from src.qatest import evidence, graph_writeback, queue
    from src.qatest.types import Report

    _runner_identity(request)          # authenticate; the run row is already labelled
    report = body.report or {}
    queue.finish(run_id, projectId, report)

    # Coverage is scored again HERE because only the API can reach Neo4j: the runner
    # computes it from the plan alone, which understates the denominator for a filtered
    # run. Re-stored so the Results tab reads one authoritative number.
    try:
        graded = _coverage_for(projectId, report)
        if graded and graded != report.get("coverage"):
            report["coverage"] = graded
            from src.qatest.types import Report as _R
            evidence.write_report(_R.from_dict(report))
    except Exception as exc:                                  # noqa: BLE001
        log.debug("qa: coverage rescore for %s failed: %s", run_id, exc)

    try:
        steps = evidence.read_steps(projectId, run_id)
        # Rebuilt into a Report: write_results reads attributes, and passing the raw
        # dict failed with "'dict' object has no attribute 'project_id'" AFTER the run
        # had succeeded — so the run looked fine and the graph silently stayed empty.
        graph_writeback.write_results(Report.from_dict(report), steps)
        wrote = True
    except Exception as exc:                                   # noqa: BLE001
        # The run itself succeeded and its evidence is stored; a failed write-back must
        # not turn that into a failed run.
        log.warning("QA graph write-back for %s failed: %s", run_id, exc)
        wrote = False

    return {"ok": True, "graphWriteback": wrote}


@router.get("/results/{project_id}")
def list_results(project_id: str,
                 user: dict = Depends(require_permission("qa_workspace"))):
    """Every run for a project — stored ones from S3, plus any still in flight.

    The endpoints this replaced called scan_items("test-results", limit=500) per
    request: a full-table scan that also hid every run past the first 500 items.

    `active` is a separate list rather than more entries in `runs`, because the two come
    from genuinely different places and cannot be merged honestly. S3 is the source of
    truth for FINISHED runs — including ones executed straight from the CLI that never
    touched the queue — but it cannot see an unfinished one at all: `report.json` is
    written last and its presence IS the done signal. The queue is the only thing that
    knows about a run that has not finished.

    Returns a BARE LIST, exactly as it always has.

    An earlier version of this change wrapped it as `{runs, active}` and that was a
    mistake: the shape is part of the contract, and a browser holding the previous JS
    bundle — from a cache, or mid rolling-deploy — got an object where it expected an
    array and died with "n.map is not a function". Which is precisely what happened.
    In-flight runs moved to their own additive endpoint, `GET /api/qa/active/{id}`,
    that an older client simply never calls.
    """
    from src.qatest import evidence

    out = []
    for run_id in evidence.list_runs(project_id):
        report = evidence.read_report(project_id, run_id)
        if report:
            out.append(report)
    return out


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, projectId: str = Query(...),
               user: dict = Depends(require_permission("qa_workspace"))):
    """Stop a run that is queued or executing.

    Needed because the runner is a poller with no inbound port: there is no way to
    reach into a laptop and stop the work. What this does is take the run out of the
    live set, so the Results tab stops showing something that will never finish — and
    so a project delete is not blocked for ever by a run whose machine went away.

    The reaper does this automatically after 15 minutes of silence, but only for a run
    that has gone QUIET. A run whose runner is alive and wedged never goes stale, and
    before this there was no way to stop it at all.
    """
    from src.qatest import queue

    row = queue.cancel(run_id, projectId, user.get("username", ""))
    if not row:
        raise HTTPException(
            status_code=409,
            detail="that run is not queued or running — it may have just finished")
    return {"ok": True, "runId": run_id, "status": row.get("status")}


@router.get("/active/{project_id}")
def list_active_runs(project_id: str,
                     _: dict = Depends(require_permission("qa_workspace"))):
    """Runs that are queued or executing.

    A separate endpoint, not a field on /results, so that adding it cannot break a
    client that predates it. Its own path rather than /results/{id}/active, because
    that would be captured by /results/{project_id}/{run_id} below.

    These cannot come from S3: `report.json` is written last and its presence IS the
    done signal, so an unfinished run is invisible there by design. The queue is the
    only thing that knows about one.
    """
    from src.qatest import queue

    return {"active": [{
        "runId": r["testRunId"],
        "status": r.get("status"),
        "phase": r.get("phase", ""),
        "runner": r.get("runner", ""),
        # Where this is actually executing. Stamped on the row at claim time, so it
        # survives the machine going offline mid-run.
        "runnerMachine": r.get("runnerMachine", ""),
        "runnerOwner": r.get("runnerOwner", ""),
        "appUrl": r.get("appUrl", ""),
        "createdAt": r.get("createdAt", ""),
        "updatedAt": r.get("updatedAt", ""),
        "totalPassed": int(r.get("totalPassed") or 0),
        "totalFailed": int(r.get("totalFailed") or 0),
        "totalSkipped": int(r.get("totalSkipped") or 0),
        "totalUnemulated": int(r.get("totalUnemulated") or 0),
        "totalCases": int(r.get("totalCases") or 0),
        "phaseDetail": r.get("phaseDetail", ""),
        "reason": r.get("reason", ""),
        "kinds": list(r.get("kinds") or []),
        # The Floci containers serving this run, as the runner last reported them.
        # `emulatorsStale` means the runner went quiet — the reaper stamps it — so the
        # panel can grey them out instead of showing phantoms.
        "emulators": list(r.get("emulators") or []),
        "emulatorsStale": bool(r.get("emulatorsStale", False)),
        # The run's own console, so the UI can show what is happening rather than a
        # single phase word that keeps changing.
        "activity": list(r.get("activity") or []),
    } for r in queue.list_for_project(project_id) if r.get("status") in queue.LIVE]}


@router.get("/results/{project_id}/{run_id}")
def get_result(project_id: str, run_id: str,
               user: dict = Depends(require_permission("qa_workspace"))):
    """One run in full: summary, every step, and a presigned URL per screenshot."""
    from src.qatest import evidence

    report = evidence.read_report(project_id, run_id)
    if not report:
        raise HTTPException(status_code=404,
                            detail=f"No stored run {run_id} for project {project_id}")
    steps = evidence.read_steps(project_id, run_id)
    urls = evidence.screenshot_urls(project_id, run_id)
    for step in steps:
        step["screenshotUrl"] = urls.get(step.get("screenshotKey") or "", "")
    # Computed on read for runs that predate the coverage field, so the detail view is
    # not blank for every run that already exists.
    return {"report": report, "steps": steps,
            "coverage": _coverage_for(project_id, report)}


@router.websocket("/ws/local-run")
async def ws_local_run(ws: WebSocket):
    """Stream a local run's progress as it happens.

    Client sends: {"token", "project_id", "app_url", "run_id"?, "kinds"?}
    Server streams: plan | planned | emulator | running | step | evidence | graph | done | error

    Events are forwarded from the orchestrator's own callback rather than reconstructed
    by pattern-matching log lines, which is what the container version did — it
    guessed an event type from substrings like "scaled down" and mislabelled anything
    whose wording drifted.
    """
    await ws.accept()
    try:
        data = await ws.receive_json()
        from src.services.auth_service import verify_token
        user = verify_token(data.get("token", ""))
        if not user:
            await ws.send_json({"type": "error", "message": "Unauthorized"})
            return
        if "qa_workspace" not in (user.get("permissions") or []):
            await ws.send_json({"type": "error", "message": "Forbidden"})
            return

        project_id  = (data.get("project_id") or "").strip()
        # app_url is OPTIONAL: empty means "start this project's own application".
        # Requiring it here contradicted execute(), which has defaulted to starting
        # the app since the runner learned how.
        app_url     = (data.get("app_url") or "").strip()
        if not project_id:
            await ws.send_json({"type": "error", "message": "project_id is required"})
            return

        await ws.send_json({"type": "connected",
                            "message": (f"Starting run against {app_url}" if app_url
                                        else "Starting run — the project's own app "
                                             "will be started")})

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = "__run_finished__"

        # execute() runs on a worker thread, so events cross back via the loop.
        def on_event(ev: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        from src.qatest.service import execute

        def runner():
            """Always queue the sentinel, so the reader below cannot hang on a raise."""
            try:
                return execute(project_id, app_url, data.get("run_id"),
                               user["username"], bool(data.get("exploratory")),
                               on_event=on_event,
                               kinds=list(data.get("kinds") or []))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, {"type": SENTINEL})

        task = loop.run_in_executor(None, runner)

        # A sentinel rather than racing queue.get() against the run: a cancelled
        # queue.get() can consume an item before the cancellation lands, so the
        # racing version silently dropped events. Reading until the sentinel also
        # guarantees every event queued before the run ended is delivered.
        while True:
            ev = await queue.get()
            if ev.get("type") == SENTINEL:
                break
            await ws.send_json(ev)

        report = await task
        await ws.send_json({"type": "report", **report})

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass

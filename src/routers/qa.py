"""QA Workspace API — test generation, execution, results, activity."""
from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from fastapi import (APIRouter, Body, Depends, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from pydantic import BaseModel
from src.routers.auth import get_current_user, require_permission
from src.database.dynamo_client import put_item, get_item, get_item_by_pk, scan_items, update_item
from src.storage.s3_client import get_json, list_objects, presigned_url

router = APIRouter(prefix="/api/qa", tags=["qa"])

log = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────────────────────

class GenerateTestsRequest(BaseModel):
    project_id: str
    test_types: list[str] = ["playwright_ui", "api", "integration", "regression", "negative", "boundary"]
    coverage_target: int = 80
    focus_areas: list[str] = []


class RunTestsRequest(BaseModel):
    run_id: str | None = None
    # Without this the run is stored under projectId "default" and never appears in
    # the project's suites list — the results simply vanish.
    project_id: str | None = None
    test_types: list[str] = []


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


@router.post("/generate")
async def generate_tests(req: GenerateTestsRequest,
                          user: dict = Depends(require_permission("qa_workspace"))):
    """Trigger TestGenerationAgent for a project."""
    from src.agents.base_agent import AgentContext
    from src.agents.test_generation_agent import TestGenerationAgent
    from src.agents.code_analysis_agent import CodeAnalysisAgent

    project = get_item_by_pk("projects", req.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    session_id = str(uuid.uuid4())
    context = AgentContext(
        user_id=user["userId"],
        username=user["username"],
        role=user["role"],
        intent=f"Generate tests: {', '.join(req.test_types)} for project {req.project_id}",
        project_id=req.project_id,
        session_id=session_id,
        extra={"test_types": req.test_types, "coverage_target": req.coverage_target,
               "focus_areas": req.focus_areas},
    )

    # Load prior code analysis if available
    kg = get_json("analysis", f"{req.project_id}/code_analysis.json")
    if kg:
        from src.agents.base_agent import AgentResult
        mock_analysis = AgentResult(agent_name="code_analysis_agent")
        mock_analysis.output = kg
        context.prior_results["code_analysis_agent"] = mock_analysis

    agent = TestGenerationAgent()
    result = await agent.run(context)

    return {
        "run_id": result.output.get("run_id"),
        "suites": result.output.get("suites", 0),
        "total_tests": result.output.get("total_tests", 0),
        "artifacts": result.output.get("artifacts", []),
        "status": result.status,
        "log": result.activity_log,
    }


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


@router.post("/run")
async def run_tests(req: RunTestsRequest,
                     user: dict = Depends(require_permission("qa_workspace"))):
    """Execute tests for a given run_id (artifacts from TestGenerationAgent)."""
    from src.agents.base_agent import AgentContext, AgentResult, S3Ref
    from src.agents.test_execution_agent import TestExecutionAgent

    # project_id was omitted, so every re-run was written under projectId "default"
    # and never appeared in GET /projects/{id}/suites — the results were invisible.
    context = AgentContext(
        user_id=user["userId"],
        username=user["username"],
        role=user["role"],
        intent="Execute test suite",
        project_id=req.project_id,
        session_id=str(uuid.uuid4()),
    )

    # Build mock prior result if run_id given
    if req.run_id:
        matching = get_item_by_pk("test-results", req.run_id)
        if matching:
            mock_gen = AgentResult(agent_name="test_generation_agent")
            mock_gen.artifacts = [
                S3Ref(bucket="test-artifacts", key=_s3_key(a), uri=a)
                for a in matching.get("artifacts", [])
            ]
            context.prior_results["test_generation_agent"] = mock_gen

    agent = TestExecutionAgent()
    result = await agent.run(context)
    return result.output


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
    """Return QA-related activity for this user."""
    all_activity = scan_items("activity", limit=500)
    qa_agents = {"test_generation_agent", "test_execution_agent"}
    relevant = [
        a for a in all_activity
        if a.get("userId") == user["userId"] and a.get("agent") in qa_agents
    ]
    relevant.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return relevant[:100]


# ── WebSocket: live test generation progress ──────────────────────────────────

@router.websocket("/ws/generate")
async def ws_generate(ws: WebSocket):
    """
    WebSocket for real-time test generation progress.
    Client sends: {"token": "...", "project_id": "...", "test_types": [...]}
    Server streams: progress | artifact | done | error events
    """
    await ws.accept()
    try:
        data = await ws.receive_json()
        from src.services.auth_service import verify_token
        user = verify_token(data.get("token", ""))
        if not user:
            await ws.send_json({"type": "error", "message": "Unauthorized"})
            return

        project_id = data.get("project_id", "")
        test_types = data.get("test_types", ["playwright_ui", "api", "integration"])

        await ws.send_json({"type": "status", "message": "Loading project knowledge graph..."})

        from src.agents.base_agent import AgentContext, AgentResult
        from src.agents.test_generation_agent import TestGenerationAgent

        context = AgentContext(
            user_id=user["userId"],
            username=user["username"],
            role=user["role"],
            intent=f"Generate {', '.join(test_types)} tests",
            project_id=project_id,
            session_id=str(uuid.uuid4()),
            extra={"test_types": test_types},
        )

        # Load analysis context — non-blocking S3 fetch
        loop = asyncio.get_running_loop()
        kg = await loop.run_in_executor(
            None, lambda: get_json("analysis", f"{project_id}/code_analysis.json")
        )
        if kg:
            mock = AgentResult(agent_name="code_analysis_agent")
            mock.output = kg
            context.prior_results["code_analysis_agent"] = mock
            tech_count = len(kg.get("tech_stack", []))
            await ws.send_json({"type": "status",
                                "message": f"Knowledge graph loaded — {tech_count} tech stack items found"})
        else:
            await ws.send_json({"type": "status",
                                "message": "No prior analysis found — generating tests from project metadata"})

        await ws.send_json({"type": "status",
                            "message": f"Starting AI generation for {len(test_types)} test suite type(s)..."})

        # Run agent with heartbeat every 5 s + hard 180 s timeout
        agent = TestGenerationAgent()

        async def _heartbeat():
            elapsed = 0
            while True:
                await asyncio.sleep(5)
                elapsed += 5
                try:
                    await ws.send_json({"type": "progress",
                                        "message": f"Generating tests... ({elapsed}s elapsed)"})
                except Exception:
                    break

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
            result = await asyncio.wait_for(agent.run(context), timeout=180.0)
        except asyncio.TimeoutError:
            heartbeat_task.cancel()
            await ws.send_json({
                "type": "error",
                "message": (
                    "Test generation timed out after 3 minutes. "
                    "Bedrock may be slow or unavailable. "
                    "Template tests were not saved — please retry."
                ),
            })
            return
        finally:
            heartbeat_task.cancel()

        # Stream activity log as progress
        for log_line in result.activity_log:
            await ws.send_json({"type": "progress", "message": log_line})

        # Stream artifacts
        for artifact in result.artifacts:
            await ws.send_json({
                "type": "artifact",
                "uri": artifact.uri,
                "filename": artifact.key.split("/")[-1],
            })

        await ws.send_json({
            "type": "done",
            "runId": result.output.get("run_id"),
            "suites": result.output.get("suites", 0),
            "totalTests": result.output.get("total_tests", 0),
            "status": result.status,
        })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


# ── Local test execution (podman emulators + Playwright, evidence in S3) ─────
#
# Runs execute where podman and a browser are available — a developer machine or CI —
# not in the deployed task. The deployed API can still SHOW any run, because evidence
# lives in the shared S3 bucket rather than on whichever machine produced it.
#
# What this replaced: an ECS run_task/exec/stop_task cycle per run that dispatched to
# a Lambda named `aura-test-runner` which was never deployed, so every run from a
# deployed environment failed.

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

    if local:
        reason = ""
    elif runners:
        reason = ""
    else:
        why = "; ".join(x for x in [
            "" if podman else "podman is not available here",
            "" if browser else browser_why,
        ] if x)
        reason = (f"{why} — and no self-hosted runner is connected. Start one on a "
                  f"machine that has podman and Chromium:\n"
                  f"  python -m src.qatest.agent --api <this-host> --key gw-…")

    return {
        "canRun": bool(local or runners),
        "podman": podman,
        "browser": browser,
        "local": local,
        "runners": runners,
        "reason": reason,
        "clouds": [{"name": c.name, "port": c.port, "image": c.image} for c in CLOUDS],
    }


@router.post("/run/local")
async def run_local(req: LocalRunRequest,
                    user: dict = Depends(require_permission("qa_workspace"))):
    """Plan from the graph, start only the emulators the project needs, run, store."""
    from src.qatest.service import execute

    report = await asyncio.to_thread(
        execute, req.project_id, req.app_url, req.run_id,
        user["username"], req.exploratory)
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


class HeartbeatRequest(BaseModel):
    phase: str = ""
    # Live pass/fail/skip. Without them a running remote run can only say "running",
    # and a twelve-case run looks identical to one that has hung.
    totalPassed: int = 0
    totalFailed: int = 0
    totalSkipped: int = 0
    totalCases: int = 0


class FinishRequest(BaseModel):
    report: dict


def _runner_identity(request: Request) -> str:
    """Authenticate a runner by gateway key and return a label for it.

    Reuses the same credential path as the model gateway and the OTLP receiver, so a key
    means the same thing everywhere and there is one place to revoke it. Both helpers
    RAISE HTTPException(401) rather than returning falsy.
    """
    from src.services.gateway_service import extract_credential, resolve_credential

    user = resolve_credential(extract_credential(request))
    if "qa_workspace" not in (user.permissions or []):
        raise HTTPException(status_code=403,
                            detail="this key's role lacks qa_workspace")
    return f"{user.username}/{getattr(user, 'tool_label', '') or 'qa-runner'}"


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
    return {"runId": row["testRunId"], "projectId": row["projectId"],
            "status": row["status"]}


@router.get("/runner/next")
def runner_next(request: Request):
    """Claim the oldest queued run. 204 when there is nothing to do.

    Returns the PLAN as well as the run, because the runner cannot reach the knowledge
    graph. Also returns short-lived scoped credentials so `evidence.py` can upload
    without modification — see src/qatest/credentials.py for that trade-off.
    """
    from fastapi.responses import Response

    from src.qatest import credentials, emulators, plan, queue, workspace

    runner = _runner_identity(request)
    # Before claiming, and unconditionally: a poll that finds nothing is still proof
    # that this runner is alive, and it is the ONLY proof available before it has ever
    # picked up work.
    queue.touch_runner(runner)

    row = queue.claim(runner)
    if not row:
        return Response(status_code=204)

    project_id = row["projectId"]
    run_id = row["testRunId"]

    # Planned HERE, against Neo4j, and shipped with the claim.
    facts = plan.fetch_facts(project_id)
    cases = plan.build_plan(project_id, facts)
    clouds = [c.name for c in emulators.clouds_for(facts.get("dependencies") or [])]

    return {
        "runId": run_id,
        "projectId": project_id,
        "appUrl": row.get("appUrl", ""),
        "exploratory": bool(row.get("exploratory")),
        "ranBy": row.get("userId", ""),
        "cases": [c.__dict__ for c in cases],
        "clouds": clouds,
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

    runner = _runner_identity(request)
    queue.heartbeat(run_id, projectId, phase or body.phase, runner, body.model_dump())
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

    _runner_identity(request)
    report = body.report or {}
    queue.finish(run_id, projectId, report)

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
        "appUrl": r.get("appUrl", ""),
        "createdAt": r.get("createdAt", ""),
        "updatedAt": r.get("updatedAt", ""),
        "totalPassed": int(r.get("totalPassed") or 0),
        "totalFailed": int(r.get("totalFailed") or 0),
        "totalSkipped": int(r.get("totalSkipped") or 0),
        "totalCases": int(r.get("totalCases") or 0),
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
    return {"report": report, "steps": steps}


@router.websocket("/ws/local-run")
async def ws_local_run(ws: WebSocket):
    """Stream a local run's progress as it happens.

    Client sends: {"token", "project_id", "app_url", "run_id"?, "exploratory"?}
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
                               on_event=on_event)
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

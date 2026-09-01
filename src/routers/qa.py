"""QA Workspace API — test generation, execution, results, activity."""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from src.routers.auth import get_current_user, require_permission
from src.database.dynamo_client import put_item, get_item, get_item_by_pk, scan_items, update_item
from src.storage.s3_client import get_json, list_objects, presigned_url

router = APIRouter(prefix="/api/qa", tags=["qa"])


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
    """Return presigned S3 URLs for test artifacts."""
    run = get_item_by_pk("test-results", run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    result = []
    for uri in run.get("artifacts", []):
        key = _s3_key(uri)
        try:
            url = presigned_url("test-artifacts", key, expires=3600)
            result.append({"key": key, "url": url, "filename": key.split("/")[-1]})
        except Exception:
            result.append({"key": key, "url": uri, "filename": key.split("/")[-1]})
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


@router.get("/capabilities")
def qa_capabilities(user: dict = Depends(require_permission("qa_workspace"))):
    """Whether THIS process can execute a run, and why not when it cannot.

    The UI uses this to disable the run button with a reason instead of offering a
    button that fails — which is what the old ECS path did in every deployed
    environment.
    """
    from src.qatest.emulators import CLOUDS, podman_available
    from src.qatest.runner import _playwright_available

    podman = podman_available()
    browser, browser_why = _playwright_available()
    return {
        "canRun": podman and browser,
        "podman": podman,
        "browser": browser,
        "reason": ("" if (podman and browser) else
                   "; ".join(x for x in [
                       "" if podman else "podman is not available here",
                       "" if browser else browser_why,
                   ] if x)),
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


@router.get("/results/{project_id}")
def list_results(project_id: str,
                 user: dict = Depends(require_permission("qa_workspace"))):
    """Every stored run for a project, newest first — read from S3, not scanned.

    The endpoints this replaced called scan_items("test-results", limit=500) per
    request: a full-table scan that also hid every run past the first 500 items.
    """
    from src.qatest import evidence

    out = []
    for run_id in evidence.list_runs(project_id):
        report = evidence.read_report(project_id, run_id)
        if report:
            out.append(report)
    return out


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

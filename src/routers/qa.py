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
    """Return test runs/suites for a project from DynamoDB."""
    all_runs = scan_items("test-results", limit=500)
    runs = [r for r in all_runs if r.get("projectId") == project_id]
    runs.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
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


@router.post("/run")
async def run_tests(req: RunTestsRequest,
                     user: dict = Depends(require_permission("qa_workspace"))):
    """Execute tests for a given run_id (artifacts from TestGenerationAgent)."""
    from src.agents.base_agent import AgentContext, AgentResult, S3Ref
    from src.agents.test_execution_agent import TestExecutionAgent

    context = AgentContext(
        user_id=user["userId"],
        username=user["username"],
        role=user["role"],
        intent="Execute test suite",
        session_id=str(uuid.uuid4()),
    )

    # Build mock prior result if run_id given
    if req.run_id:
        runs = scan_items("test-results", limit=500)
        matching = next((r for r in runs if r.get("testRunId") == req.run_id), None)
        if matching:
            mock_gen = AgentResult(agent_name="test_generation_agent")
            mock_gen.artifacts = [
                S3Ref(bucket="aura-test-artifacts", key=a.replace("s3://aura-test-artifacts/", ""), uri=a)
                for a in matching.get("artifacts", [])
            ]
            context.prior_results["test_generation_agent"] = mock_gen

    agent = TestExecutionAgent()
    result = await agent.run(context)
    return result.output


@router.get("/runs/{run_id}")
def get_run_detail(run_id: str, user: dict = Depends(require_permission("qa_workspace"))):
    """Get full detail of a test run."""
    all_runs = scan_items("test-results", limit=500)
    run = next((r for r in all_runs if r.get("testRunId") == run_id), None)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/runs/{run_id}/artifacts")
def get_run_artifacts(run_id: str, user: dict = Depends(require_permission("qa_workspace"))):
    """Return presigned S3 URLs for test artifacts."""
    all_runs = scan_items("test-results", limit=500)
    run = next((r for r in all_runs if r.get("testRunId") == run_id), None)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    result = []
    for uri in run.get("artifacts", []):
        key = uri.replace("s3://aura-test-artifacts/", "")
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


# ── Container test execution (ECS Fargate + screenshots) ─────────────────────

class ContainerRunRequest(BaseModel):
    run_id:          str
    project_id:      str
    app_url:         str         # URL of the application to test
    app_description: str = ""


@router.post("/run/container")
async def run_in_container(req: ContainerRunRequest,
                            user: dict = Depends(require_permission("qa_workspace"))):
    """Launch ECS Fargate container, run Playwright with screenshots, scale down."""
    from src.agents.container_test_runner import ContainerTestRunnerAgent
    from src.agents.base_agent import AgentContext

    session_id = str(uuid.uuid4())
    context = AgentContext(
        user_id=user["userId"],
        username=user["username"],
        role=user["role"],
        intent=f"Run tests in container against {req.app_url}",
        project_id=req.project_id,
        session_id=session_id,
        extra={
            "run_id":          req.run_id,
            "app_url":         req.app_url,
            "app_description": req.app_description,
        },
    )

    agent  = ContainerTestRunnerAgent()
    result = await agent.run(context)

    if result.status == "failed":
        raise HTTPException(status_code=500, detail=result.error or "Container test run failed")

    return {
        "session_id":        session_id,
        "run_id":            req.run_id,
        "status":            result.status,
        "total_screenshots": result.output.get("total_screenshots", 0),
        "total_passed":      result.output.get("total_passed", 0),
        "total_failed":      result.output.get("total_failed", 0),
        "log":               result.activity_log[-10:],
    }


@router.get("/run/container/{run_id}/screenshots")
async def get_container_screenshots(run_id: str,
                                     user: dict = Depends(require_permission("qa_workspace"))):
    """Return presigned URLs for all screenshots from a container test run."""
    all_runs = scan_items("test-results", limit=500)
    run      = next((r for r in all_runs if r.get("testRunId") == run_id), None)
    if not run:
        raise HTTPException(status_code=404, detail="Test run not found")

    screenshots_uris = run.get("screenshots", [])
    result = []
    for uri in screenshots_uris:
        key = uri.replace("s3://aura-test-artifacts/", "")
        try:
            url  = presigned_url("test-artifacts", key, expires=3600)
            name = key.split("/")[-1]
            result.append({
                "key":      key,
                "url":      url,
                "filename": name,
                "failed":   "FAIL" in name.upper(),
            })
        except Exception:
            pass
    return result


@router.websocket("/ws/container-run")
async def ws_container_run(ws: WebSocket):
    """
    WebSocket streaming for container test execution.
    Client sends: {"token": "...", "run_id": "...", "project_id": "...", "app_url": "..."}
    Server streams: scale_up | setup | running | screenshot | upload | scale_down | done | error
    """
    await ws.accept()
    try:
        data = await ws.receive_json()
        from src.services.auth_service import verify_token
        user = verify_token(data.get("token", ""))
        if not user:
            await ws.send_json({"type": "error", "message": "Unauthorized"})
            return

        run_id     = data.get("run_id", str(uuid.uuid4())[:8])
        project_id = data.get("project_id", "")
        app_url    = data.get("app_url", "http://localhost:3000")

        await ws.send_json({"type": "connected", "message": f"Starting container run for {app_url}"})

        from src.agents.container_test_runner import ContainerTestRunnerAgent
        from src.agents.base_agent import AgentContext

        context = AgentContext(
            user_id=user["userId"],
            username=user["username"],
            role=user["role"],
            intent=f"Container test run: {app_url}",
            project_id=project_id,
            session_id=str(uuid.uuid4()),
            extra={"run_id": run_id, "app_url": app_url},
        )

        agent  = ContainerTestRunnerAgent()

        # Heartbeat so the client sees activity while the agent runs
        async def _hb():
            elapsed = 0
            while True:
                await asyncio.sleep(3)
                elapsed += 3
                try:
                    await ws.send_json({"type": "running",
                                        "message": f"Container running... ({elapsed}s)"})
                except Exception:
                    break

        hb_task = asyncio.create_task(_hb())
        try:
            result = await agent.run(context)
        finally:
            hb_task.cancel()

        # Stream log lines as events
        for log_line in result.activity_log:
            lo = log_line.lower()
            event_type = (
                "screenshot"  if "screenshot" in lo else
                "scale_down"  if "scaled down" in lo or "scale-down" in lo else
                "scale_up"    if "scale-up" in lo or "scaling up" in lo or "running (" in lo else
                "setup"       if "installing" in lo or "dependencies" in lo else
                "upload"      if "upload" in lo else
                "running"
            )
            await ws.send_json({"type": event_type, "message": log_line})

        await ws.send_json({
            "type":              "done",
            "runId":             run_id,
            "status":            result.status,
            "totalScreenshots":  result.output.get("total_screenshots", 0),
            "totalPassed":       result.output.get("total_passed", 0),
            "totalFailed":       result.output.get("total_failed", 0),
        })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


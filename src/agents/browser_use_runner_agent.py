"""BrowserUseRunnerAgent — AI-driven browser testing via browser-use + Claude Bedrock.

Used when DEPLOYMENT_ENV=local: runs Playwright Chromium in-process.
The claim that this was "mirrored in lambda/test_runner/handler.py for
DEPLOYMENT_ENV=ecs" was stale: that Lambda was never deployed, and nothing
reads deployment_env for QA any more. Browser runs execute on a self-hosted
runner — src/qatest/agent.py.
"""
from __future__ import annotations
import asyncio
import base64
import uuid
from datetime import datetime, timezone

from src.agents.base_agent import BaseAgent, AgentContext, AgentResult, S3Ref
from src.config_settings import get_settings

DEFAULT_TASKS = [
    "verify the page loads without JavaScript errors and take a screenshot of the full page",
    "verify the main navigation menu or header is visible and take a screenshot",
    "verify the user is authenticated or that a login form is present",
]


class BrowserUseRunnerAgent(BaseAgent):
    name = "browser_use_runner"
    description = "AI-driven browser testing using browser-use + Claude Bedrock (local in-process mode)"

    async def run(self, context: AgentContext) -> AgentResult:
        from src.storage.s3_client import put_object, _bucket
        from src.database.dynamo_client import put_item

        result     = self._result(context)
        s          = get_settings()
        run_id     = context.extra.get("run_id") or str(uuid.uuid4())[:8]
        app_url    = context.extra.get("app_url", "http://localhost:3000")
        tasks      = context.extra.get("tasks", DEFAULT_TASKS)
        project_id = context.project_id or "default"
        loop       = asyncio.get_running_loop()

        result.log(f"BrowserUseRunnerAgent: {len(tasks)} tasks against {app_url}")

        # Import browser-use lazily so startup doesn't fail if not installed
        try:
            from browser_use import Agent as BrowserAgent
            from langchain_aws import ChatBedrock
        except ImportError as exc:
            result.log(f"browser-use not installed: {exc}")
            result.log("Run: pip install browser-use langchain-aws && playwright install chromium")
            result.error = str(exc)
            return result.finish("failed")

        llm            = ChatBedrock(model_id=s.bedrock_model_id, region_name=s.bedrock_region)
        bucket_suffix  = "test-artifacts"
        bucket_name    = _bucket(bucket_suffix)  # full name for S3Ref URIs only
        passed = failed = 0

        for i, task in enumerate(tasks):
            result.log(f"[{i + 1}/{len(tasks)}] {task}")
            try:
                agent   = BrowserAgent(task=f"On {app_url}: {task}", llm=llm)
                history = await agent.run(max_steps=10)
                ok      = history.is_done() and not history.has_errors()

                # Upload screenshots captured at each step
                for step_idx, step in enumerate(history.history):
                    raw = getattr(step.state, "screenshot", None) if step.state else None
                    if not raw:
                        continue
                    img = base64.b64decode(raw) if isinstance(raw, str) else raw
                    key = f"{project_id}/{run_id}/screenshots/task{i + 1}_step{step_idx}.png"
                    uri = await loop.run_in_executor(
                        None,
                        lambda k=key, d=img: put_object(bucket_suffix, k, d, "image/png"),
                    )
                    result.artifacts.append(S3Ref(bucket=bucket_name, key=key, uri=uri or f"s3://{bucket_name}/{key}"))

                if ok:
                    passed += 1
                    result.log("  ✓ PASSED")
                else:
                    failed += 1
                    errs = history.errors() if hasattr(history, "errors") else []
                    result.log(f"  ✗ FAILED — {errs[-1] if errs else 'task not completed'}")

            except Exception as exc:
                failed += 1
                result.log(f"  ✗ ERROR — {exc}")

        # S3 completion marker (used by _wait_for_completion in ECS mode)
        try:
            await loop.run_in_executor(
                None,
                lambda: put_object(
                    bucket_suffix,
                    f"{project_id}/{run_id}/results/COMPLETE",
                    b"DONE",
                    "text/plain",
                ),
            )
        except Exception as exc:
            result.log(f"S3 marker warning: {exc}")

        # DynamoDB record
        now = datetime.now(timezone.utc).isoformat()
        try:
            await loop.run_in_executor(
                None,
                lambda: put_item("test-results", {
                    "testRunId":   run_id,
                    "projectId":   project_id,
                    "userId":      context.user_id,
                    "type":        "browser_use",
                    "status":      "completed" if failed == 0 else "failed",
                    "totalPassed": passed,
                    "totalFailed": failed,
                    "totalSkipped": 0,
                    "appUrl":      app_url,
                    "screenshots": [a.uri for a in result.artifacts],
                    "artifacts":   [a.uri for a in result.artifacts],
                    "createdAt":   now,
                    "completedAt": now,
                }),
            )
        except Exception as exc:
            result.log(f"DynamoDB warning: {exc}")

        result.log(
            f"Done — {passed} passed, {failed} failed, "
            f"{len(result.artifacts)} screenshots"
        )
        result.output = {
            "run_id":            run_id,
            "app_url":           app_url,
            "total_passed":      passed,
            "total_failed":      failed,
            "total_screenshots": len(result.artifacts),
            "screenshots":       [a.uri for a in result.artifacts],
        }
        return result.finish("success" if failed == 0 else "failed")

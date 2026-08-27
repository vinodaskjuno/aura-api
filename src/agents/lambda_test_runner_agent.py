"""LambdaTestRunnerAgent — dispatches browser-use test execution to AWS Lambda.

Used when DEPLOYMENT_ENV=ecs. FastAPI calls lambda.invoke() synchronously;
the Lambda container runs BrowserUseRunnerAgent logic and returns results.
Screenshots and completion marker are uploaded to S3 by the Lambda function.
"""
from __future__ import annotations
import asyncio
import json
import uuid

import boto3

from src.agents.base_agent import BaseAgent, AgentContext, AgentResult, S3Ref
from src.config_settings import get_settings
from src.storage.s3_client import _bucket

DEFAULT_TASKS = [
    "verify the page loads without JavaScript errors and take a screenshot of the full page",
    "verify the main navigation menu or header is visible and take a screenshot",
    "verify the user is authenticated or that a login form is present",
]


class LambdaTestRunnerAgent(BaseAgent):
    name = "lambda_test_runner"
    description = "Dispatches browser-use tests to Lambda (used in ECS deployment)"

    async def run(self, context: AgentContext) -> AgentResult:
        result     = self._result(context)
        s          = get_settings()
        run_id     = context.extra.get("run_id") or str(uuid.uuid4())[:8]
        app_url    = context.extra.get("app_url", "http://localhost:3000")
        tasks      = context.extra.get("tasks") or DEFAULT_TASKS
        project_id = context.project_id or "default"

        result.log(f"LambdaTestRunnerAgent: invoking {s.test_runner_lambda} (run {run_id})")
        result.log(f"Target URL: {app_url}")
        result.log(f"Tasks: {len(tasks)} test scenarios")

        payload = {
            "run_id":     run_id,
            "project_id": project_id,
            "app_url":    app_url,
            "tasks":      tasks,
            "s3_bucket":  _bucket("test-artifacts"),
        }

        loop = asyncio.get_running_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: boto3.client("lambda", region_name=s.aws_region).invoke(
                    FunctionName=s.test_runner_lambda,
                    InvocationType="RequestResponse",  # synchronous — waits up to 900 s
                    Payload=json.dumps(payload),
                ),
            )
        except Exception as exc:
            result.log(f"Lambda invoke failed: {exc}")
            result.error = str(exc)
            return result.finish("failed")

        # Parse Lambda response
        raw_payload = resp["Payload"].read()
        try:
            outer = json.loads(raw_payload)
            # Lambda returns {"statusCode": 200, "body": "{...}"}
            stats = json.loads(outer.get("body", "{}")) if isinstance(outer.get("body"), str) else outer
        except Exception as exc:
            result.log(f"Lambda response parse error: {exc} — raw: {raw_payload[:200]}")
            stats = {}

        passed = stats.get("passed", 0)
        failed = stats.get("failed", 0)
        result.log(f"Lambda returned: {passed} passed, {failed} failed")

        # Collect screenshots Lambda uploaded to S3
        try:
            from src.storage.s3_client import list_objects
            objects = await loop.run_in_executor(
                None,
                lambda: list_objects("test-artifacts",   # suffix only — list_objects calls _bucket internally
                                     prefix=f"{project_id}/{run_id}/screenshots/"),
            )
            bucket_name = _bucket("test-artifacts")  # full name for S3Ref URIs only
            for obj in objects or []:
                if obj["key"].endswith(".png"):
                    uri = f"s3://{bucket_name}/{obj['key']}"
                    result.artifacts.append(S3Ref(bucket=bucket_name, key=obj["key"], uri=uri))
            result.log(f"Collected {len(result.artifacts)} screenshots from S3")
        except Exception as exc:
            result.log(f"Screenshot collection warning: {exc}")

        result.output = {
            "run_id":            run_id,
            "app_url":           app_url,
            "total_passed":      passed,
            "total_failed":      failed,
            "total_screenshots": len(result.artifacts),
            "screenshots":       [a.uri for a in result.artifacts],
        }
        return result.finish("success" if failed == 0 else "failed")

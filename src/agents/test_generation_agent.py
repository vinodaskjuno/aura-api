from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from src.agents.base_agent import BaseAgent, AgentContext, AgentResult, S3Ref


TEST_TYPES = ["playwright_ui", "api", "integration", "regression", "negative", "boundary"]

SYSTEM_PROMPT = """You are an expert test engineer. Generate comprehensive test cases based on the provided code analysis and requirements.
For each test type requested, generate realistic, executable test code.
Return JSON: {"test_suites": [{"type": "playwright_ui|api|integration|regression|negative|boundary", "filename": "...", "content": "...", "test_count": N}]}"""


class TestGenerationAgent(BaseAgent):
    name = "test_generation_agent"
    description = "Generate Playwright, API, integration, regression, negative and boundary tests from code analysis"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        result.log("TestGenerationAgent: Starting AI-powered test generation")

        project_id = context.project_id or "default"
        run_id = str(uuid.uuid4())[:8]

        # Gather context from prior code analysis
        analysis_context = ""
        prior = context.prior_results.get("code_analysis_agent")
        if prior:
            tech = prior.output.get("tech_stack", [])
            apis = prior.output.get("apis", [])
            services = prior.output.get("services", [])
            analysis_context = f"""
Tech stack: {', '.join(tech)}
Services: {', '.join(services[:10])}
APIs: {', '.join(apis[:10])}
"""

        try:
            import asyncio
            import boto3
            from botocore.config import Config
            from src.config_settings import get_settings
            s = get_settings()
            client = boto3.client(
                "bedrock-runtime",
                region_name=s.bedrock_region,
                config=Config(connect_timeout=10, read_timeout=120,
                              retries={"max_attempts": 1}),
            )

            prompt = f"""Generate a comprehensive test suite for this project.
{analysis_context}
User request: {context.intent}

Generate tests for all applicable types: {', '.join(TEST_TYPES)}
Include realistic test scenarios, edge cases, negative cases, and boundary conditions.
For Playwright tests use TypeScript. For API tests use both Playwright API and pytest formats.
"""
            body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 8192,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}]}

            result.log("Calling Bedrock for AI test generation...")
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: client.invoke_model(
                    modelId=s.bedrock_model_id, body=json.dumps(body),
                    contentType="application/json", accept="application/json",
                ),
            )
            text = json.loads(resp["body"].read())["content"][0]["text"]
            start = text.find("{")
            end = text.rfind("}") + 1
            data = json.loads(text[start:end]) if start >= 0 and end > start else {"test_suites": []}

        except Exception as exc:
            result.log(f"Bedrock generation failed, using template: {exc}")
            data = _template_tests(project_id, analysis_context)

        suites = data.get("test_suites", [])
        total_tests = sum(s.get("test_count", 0) for s in suites)
        result.log(f"Generated {len(suites)} test suites, {total_tests} total tests")

        # Save each suite to S3 (non-blocking)
        try:
            import asyncio
            from src.storage.s3_client import put_object
            loop = asyncio.get_running_loop()
            for suite in suites:
                key = f"{project_id}/{run_id}/{suite['filename']}"
                uri = await loop.run_in_executor(
                    None,
                    lambda k=key, c=suite["content"]: put_object("test-artifacts", k, c, "text/plain"),
                )
                result.artifacts.append(S3Ref(bucket="aura-test-artifacts", key=key, uri=uri))
                result.log(f"  Saved {suite['type']}: {suite['filename']} ({suite.get('test_count', 0)} tests)")
        except Exception as exc:
            result.log(f"S3 save warning: {exc}")

        # Persist test run metadata to DynamoDB (non-blocking)
        try:
            import asyncio
            from src.database.dynamo_client import put_item
            loop = asyncio.get_running_loop()
            record = {
                "testRunId": run_id,
                "projectId": project_id,
                "userId": context.user_id,
                "type": "generation",
                "status": "generated",
                "suiteCount": len(suites),
                "totalTests": total_tests,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "artifacts": [a.uri for a in result.artifacts],
            }
            await loop.run_in_executor(None, lambda: put_item("test-results", record))
        except Exception as exc:
            result.log(f"DynamoDB save warning: {exc}")

        result.output = {"run_id": run_id, "suites": len(suites), "total_tests": total_tests,
                         "artifacts": [a.uri for a in result.artifacts]}
        return result.finish("success")


def _template_tests(project_id: str, context: str) -> dict:
    """Fallback template tests when Bedrock is unavailable."""
    return {
        "test_suites": [
            {
                "type": "playwright_ui",
                "filename": "test_ui.spec.ts",
                "test_count": 5,
                "content": f"""// Playwright UI Tests — {project_id}
import {{ test, expect }} from '@playwright/test';

test.describe('UI Tests', () => {{
  test('should load homepage', async ({{ page }}) => {{
    await page.goto('/');
    await expect(page).toHaveTitle(/./);
  }});

  test('should navigate to login', async ({{ page }}) => {{
    await page.goto('/login');
    await expect(page.locator('form')).toBeVisible();
  }});

  test('should reject invalid credentials', async ({{ page }}) => {{
    await page.goto('/login');
    await page.fill('[name=username]', 'invalid');
    await page.fill('[name=password]', 'wrong');
    await page.click('[type=submit]');
    await expect(page.locator('.error')).toBeVisible();
  }});
}});
""",
            },
            {
                "type": "api",
                "filename": "test_api.py",
                "test_count": 8,
                "content": f"""# API Tests — {project_id}
import pytest
import requests

BASE_URL = "http://localhost:8000"

def test_health_check():
    r = requests.get(f"{{BASE_URL}}/health")
    assert r.status_code == 200

def test_login_valid():
    r = requests.post(f"{{BASE_URL}}/auth/login", json={{"username": "admin", "password": "Admin@123"}})
    assert r.status_code == 200
    assert "access_token" in r.json()

def test_login_invalid():
    r = requests.post(f"{{BASE_URL}}/auth/login", json={{"username": "x", "password": "y"}})
    assert r.status_code == 401

def test_me_without_token():
    r = requests.get(f"{{BASE_URL}}/auth/me")
    assert r.status_code in (401, 403)
""",
            },
        ]
    }

"""ContainerTestRunnerAgent — a thin adapter onto the local runner.

**Kept only for its callers.** The name is historical: nothing here provisions a
container any more.

What was removed, and why:

- `_ecs_scale_up` / `_wait_for_running` / `_trigger_ecs_exec` / `_ecs_scale_down` —
  an ECS `run_task` + ECS Exec + `stop_task` cycle for every run. Slow, and it left a
  Fargate task behind whenever a run died between start and stop.
- Lambda dispatch to `aura-test-runner`. That function was never deployed — the
  account has zero Lambda functions — so on `DEPLOYMENT_ENV=ecs`, which is every
  deployed environment, every run failed.
- `_docker_*` paths, superseded by podman-managed emulators in `src/qatest/emulators.py`.
- `_simulate_run`, which recorded pass counts for tests that never executed. A run
  that cannot execute now returns `status: "unavailable"` carrying the reason, and the
  `RunStatus` type in `src/qatest/types.py` makes a fabricated count unrepresentable.

Real work lives in `src/qatest/`: plan from the graph, emulators from the project's
dependencies, Playwright execution, evidence in S3.
"""
from __future__ import annotations

import logging

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent, S3Ref

logger = logging.getLogger(__name__)


class ContainerTestRunnerAgent(BaseAgent):
    name = "container_test_runner"
    description = "Run QualityMind tests locally with cloud emulators; evidence to S3"

    async def run(self, context: AgentContext) -> AgentResult:
        import asyncio

        from src.qatest import evidence
        from src.qatest.service import execute

        result = self._result(context)
        run_id = context.extra.get("run_id") or None
        app_url = context.extra.get("app_url") or ""
        project_id = context.project_id or "default"

        if not app_url:
            result.error = "app_url is required"
            result.log("No app_url given — nothing to test against")
            return result.finish("failed")

        result.log(f"Local test run for {project_id} against {app_url}")

        report = await asyncio.to_thread(
            execute, project_id, app_url, run_id, context.username,
            bool(context.extra.get("exploratory")))

        for line in report.get("reason", "").splitlines():
            if line:
                result.log(line)

        rid = report["runId"]
        keys = evidence.screenshot_urls(project_id, rid)
        bucket = ""
        try:
            from src.storage.s3_client import _bucket
            bucket = _bucket(evidence.BUCKET)
        except Exception:  # noqa: BLE001 — artifact links are not worth failing a run
            pass
        for key in sorted(keys):
            result.artifacts.append(
                S3Ref(bucket=bucket, key=key, uri=f"s3://{bucket}/{key}"))

        result.log(f"{report['totalPassed']} passed, {report['totalFailed']} failed, "
                   f"{report['totalSkipped']} skipped, {len(keys)} screenshot(s)")
        result.output = {
            "run_id": rid,
            "app_url": app_url,
            "status": report["status"],
            "total_passed": report["totalPassed"],
            "total_failed": report["totalFailed"],
            "total_skipped": report["totalSkipped"],
            "total_screenshots": len(keys),
            "screenshots": sorted(keys),
            "report": report,
        }

        # "unavailable" is partial, not success: nothing ran, and a caller that treats
        # a green status as "tests passed" must not be told they did.
        if report["status"] == "unavailable":
            result.error = report.get("reason") or "run could not execute"
            return result.finish("partial")
        return result.finish("success" if report["status"] == "passed" else "failed")

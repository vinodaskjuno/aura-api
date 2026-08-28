"""Execute test suites against a project's cloned repository.

Previously both runners returned hardcoded pass counts and stored them as
`completed`, so QualityMind reported green results for tests that never ran. This
now runs pytest for real inside the clone. When it genuinely cannot run — no clone,
no pytest, no test files — it returns `status: "simulated"` and the run is recorded
as `simulated`, never `completed`.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent

_TIMEOUT_S = 300


class TestExecutionAgent(BaseAgent):
    name = "test_execution_agent"
    description = "Execute generated test suites (pytest, Playwright) against the cloned repo"

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        run_id = str(uuid.uuid4())[:8]
        project_id = context.project_id or "default"
        result.log(f"TestExecutionAgent: run {run_id} for project {project_id}")

        clone_dir = self._clone_dir(project_id, result)

        prior = context.prior_results.get("test_generation_agent")
        artifacts = prior.artifacts if prior else []
        targets = context.extra.get("test_paths") or []

        if clone_dir and not targets:
            targets = self._discover(clone_dir, result)

        if not targets and not artifacts:
            result.log("Nothing to execute — no cloned repo with tests, and no generated artifacts")
            return result.finish("partial")

        execution_results: list[dict] = []
        simulated = False

        if clone_dir and targets:
            for target in targets:
                result.log(f"Running pytest: {target}")
                outcome = await asyncio.get_event_loop().run_in_executor(
                    None, self._run_pytest, clone_dir, target)
                execution_results.append({"file": target, **outcome})
                if outcome.get("status") == "simulated":
                    simulated = True
        else:
            # No clone to run against — be explicit rather than fabricating a pass.
            for artifact in artifacts:
                filename = artifact.key.split("/")[-1]
                execution_results.append({
                    "file": filename, "passed": 0, "failed": 0, "skipped": 0,
                    "duration": 0.0, "status": "simulated",
                    "reason": "no cloned repository to execute against",
                })
            simulated = True
            result.log("No clone available — results marked simulated, not completed")

        total_passed = sum(r.get("passed", 0) for r in execution_results)
        total_failed = sum(r.get("failed", 0) for r in execution_results)
        total_skipped = sum(r.get("skipped", 0) for r in execution_results)
        result.log(f"Execution complete: {total_passed} passed, {total_failed} failed, "
                   f"{total_skipped} skipped")

        run_status = "simulated" if simulated else "completed"
        now = datetime.now(timezone.utc).isoformat()
        try:
            from src.database.dynamo_client import put_item
            put_item("test-results", {
                "testRunId": run_id,
                "projectId": project_id,
                "userId": context.user_id,
                "type": "execution",
                "status": run_status,
                "totalTests": total_passed + total_failed + total_skipped,
                "totalPassed": total_passed,
                "totalFailed": total_failed,
                "totalSkipped": total_skipped,
                "suiteCount": len(execution_results),
                "results": execution_results,
                # Both fields are required by the UI: the runs list sorts on
                # createdAt and renders "Invalid Date" without it.
                "createdAt": now,
                "completedAt": now,
            })
        except Exception as exc:  # noqa: BLE001
            result.log(f"DynamoDB save warning: {exc}")

        result.output = {
            "run_id": run_id, "status": run_status,
            "passed": total_passed, "failed": total_failed, "skipped": total_skipped,
            "results": execution_results,
        }
        return result.finish("success" if total_failed == 0 and not simulated else "partial")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _clone_dir(self, project_id: str, result: AgentResult) -> Path | None:
        try:
            from src.services.advisor.tools import _resolve_project_dir
            path = _resolve_project_dir(project_id)
            result.log(f"Using cloned repo at {path}")
            return path
        except Exception as exc:  # noqa: BLE001
            result.log(f"No cloned repo for '{project_id}': {exc}")
            return None

    def _discover(self, clone_dir: Path, result: AgentResult) -> list[str]:
        """Find pytest suites in the clone, generated ones first."""
        found: list[str] = []
        for pattern in ("**/tests/generated/test_*.py", "**/test_*.py", "**/*_test.py"):
            for path in sorted(clone_dir.glob(pattern)):
                if any(p in path.parts for p in (".git", "node_modules", ".venv", "__pycache__")):
                    continue
                rel = str(path.relative_to(clone_dir))
                if rel not in found:
                    found.append(rel)
        result.log(f"Discovered {len(found)} pytest file(s)")
        return found

    def _run_pytest(self, clone_dir: Path, rel_path: str) -> dict:
        """Run one pytest file and parse the real counts from its summary line."""
        clone_dir = clone_dir.resolve()
        target = (clone_dir / rel_path).resolve()
        # Tests usually import the app package relative to their own project root,
        # so run from the nearest ancestor that looks like one.
        cwd = target.parent
        while cwd != clone_dir and not (cwd / "requirements.txt").exists() \
                and not (cwd / "pyproject.toml").exists():
            cwd = cwd.parent
        if cwd == clone_dir and not (clone_dir / "requirements.txt").exists():
            cwd = target.parent

        # sys.executable, not "python": the API runs inside a virtualenv that has
        # pytest and the project's dependencies. A bare "python" resolves to whatever
        # is first on PATH, which usually cannot even import pytest — every suite then
        # reports "failed" with zero counts and no visible reason.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(cwd)
        env.pop("PYTEST_ADDOPTS", None)     # don't inherit the host project's options
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", str(target), "-q", "--no-header",
                 "-p", "no:cacheprovider"],
                capture_output=True, text=True, timeout=_TIMEOUT_S, cwd=str(cwd), env=env,
            )
        except FileNotFoundError:
            return {"passed": 0, "failed": 0, "skipped": 0, "duration": 0.0,
                    "status": "simulated", "reason": "python not on PATH"}
        except subprocess.TimeoutExpired:
            return {"passed": 0, "failed": 0, "skipped": 0, "duration": float(_TIMEOUT_S),
                    "status": "failed", "reason": f"timed out after {_TIMEOUT_S}s"}

        return self._parse(proc.stdout + proc.stderr, proc.returncode)

    @staticmethod
    def _parse(output: str, returncode: int) -> dict:
        """Parse pytest's summary line, e.g. '3 failed, 4 passed in 0.12s'."""
        counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
        for name in counts:
            m = re.search(rf"(\d+) {name}", output)
            if m:
                counts[name] = int(m.group(1))
        duration = 0.0
        m = re.search(r"in ([\d.]+)s", output)
        if m:
            duration = float(m.group(1))

        # returncode 5 == "no tests collected"; treat as skipped, not success.
        if returncode == 5:
            return {**counts, "duration": duration, "status": "skipped",
                    "reason": "no tests collected", "output": output[-1500:]}
        status = "completed" if returncode == 0 else "failed"
        counts["failed"] += counts.pop("errors")
        out = {**counts, "duration": duration, "status": status, "output": output[-1500:]}
        if returncode != 0 and not any((counts["passed"], counts["failed"], counts["skipped"])):
            # pytest never ran (bad import, missing dependency, collection error).
            # Saying "failed: 0 passed, 0 failed" tells the operator nothing.
            first = next((l.strip() for l in output.splitlines()
                          if l.strip().startswith(("E ", "ImportError", "ModuleNotFoundError"))), "")
            out["reason"] = first[:200] or f"pytest exited {returncode} without running tests"
        return out

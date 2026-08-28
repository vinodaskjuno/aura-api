"""Regression tests for the DevMate / QualityMind demo path.

Each test here pins a bug that made a headline feature unusable:
  * write_file overwrote repo files with no preview, no undo and no UI feedback
  * `projects` is a composite table, so three separate single-key writes/reads failed
  * every artifact download URL was built from a bucket prefix that never matched
  * test execution returned hardcoded pass counts and stored them as `completed`
"""
from __future__ import annotations

import subprocess

import pytest

from src.agents.test_execution_agent import TestExecutionAgent
from src.routers.qa import _s3_key
from src.services.advisor import tools


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """A minimal git repo standing in for a cloned project."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(tools, "_WORKSPACE_ROOT", root)
    repo = root / "proj_1"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "calc.py").write_text("def rate(q):\n    return 1 if q > 10 else 0\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    return repo


# ── The approval gate ────────────────────────────────────────────────────────

def test_write_file_stages_and_does_not_touch_disk(clone):
    before = (clone / "app" / "calc.py").read_text()
    out = tools.write_file("proj 1", "app/calc.py", "def rate(q):\n    return 2\n")

    assert out["staged"] is True
    assert "NOT yet written" in out["message"]
    assert (clone / "app" / "calc.py").read_text() == before, \
        "write_file must not reach disk before approval"
    assert "-    return 1 if q > 10 else 0" in out["diff"]


def test_apply_writes_and_clears_the_stage(clone):
    tools.write_file("proj 1", "app/calc.py", "def rate(q):\n    return 2\n")
    assert tools.apply_pending("proj 1", "app/calc.py")["success"] is True
    assert "return 2" in (clone / "app" / "calc.py").read_text()
    assert tools.list_pending("proj 1") == []


def test_discard_leaves_the_file_alone(clone):
    before = (clone / "app" / "calc.py").read_text()
    tools.write_file("proj 1", "app/calc.py", "wrecked")
    assert tools.discard_pending("proj 1", "app/calc.py")["success"] is True
    assert (clone / "app" / "calc.py").read_text() == before
    assert tools.list_pending("proj 1") == []


def test_staging_survives_a_different_process(clone):
    """The agent stages on the WebSocket worker; the UI reads on a REST worker.
    A process-local dict would lose the change between them."""
    tools.write_file("proj 1", "app/calc.py", "def rate(q):\n    return 3\n")
    tools._PENDING = {}                      # simulate a fresh process
    pending = tools.list_pending("proj 1")
    assert [c["path"] for c in pending] == ["app/calc.py"]


def test_no_op_write_is_not_staged(clone):
    same = (clone / "app" / "calc.py").read_text()
    out = tools.write_file("proj 1", "app/calc.py", same)
    assert out["staged"] is False
    assert tools.list_pending("proj 1") == []


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "app/../../../x"])
def test_traversal_is_refused(clone, path):
    """`str().startswith()` accepted /workspace/foo-evil against a /workspace/foo
    root; relative_to compares path components instead."""
    assert "error" in tools.read_file("proj 1", path)
    assert "error" in tools.write_file("proj 1", path, "x")


# ── Artifact URLs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("uri,expected", [
    ("s3://aura-123456789012-test-artifacts/proj/run/a.py", "proj/run/a.py"),
    ("s3://aura-test-artifacts/proj/run/b.py", "proj/run/b.py"),
    ("s3://bucket/only-key", "only-key"),
    ("already/a/key.py", "already/a/key.py"),
])
def test_s3_key_strips_any_bucket(uri, expected):
    """The hardcoded literal never matched the real prefixed bucket name, so the
    whole s3:// URI was handed to presigned_url as a key."""
    assert _s3_key(uri) == expected


# ── Test execution ───────────────────────────────────────────────────────────

def test_pytest_output_is_parsed_not_invented():
    out = TestExecutionAgent._parse("3 failed, 7 passed, 1 skipped in 0.42s", 1)
    assert (out["passed"], out["failed"], out["skipped"]) == (7, 3, 1)
    assert out["duration"] == 0.42
    assert out["status"] == "failed"


def test_errors_count_as_failures():
    out = TestExecutionAgent._parse("2 errors in 0.10s", 1)
    assert out["failed"] == 2


def test_no_tests_collected_is_skipped_not_success():
    out = TestExecutionAgent._parse("no tests ran in 0.01s", 5)
    assert out["status"] == "skipped"


def test_a_run_that_never_started_explains_itself():
    """'failed: 0 passed, 0 failed' told the operator nothing about why."""
    out = TestExecutionAgent._parse(
        "ImportError: cannot import name 'app'\nERROR: not found", 4)
    assert out["status"] == "failed"
    assert out.get("reason"), "an empty run must carry a reason"


@pytest.mark.asyncio
async def test_execution_without_a_clone_is_labelled_simulated(monkeypatch, tmp_path):
    """It used to return {passed: 5, failed: 0} and store it as `completed`."""
    from src.agents.base_agent import AgentContext, AgentResult, S3Ref
    monkeypatch.setattr(tools, "_WORKSPACE_ROOT", tmp_path / "empty")
    ctx = AgentContext(user_id="u", username="u", role="admin", intent="run",
                       project_id="ghost", session_id="s")
    gen = AgentResult(agent_name="test_generation_agent")
    gen.artifacts = [S3Ref(bucket="test-artifacts", key="p/r/test_x.py", uri="s3://b/p/r/test_x.py")]
    ctx.prior_results["test_generation_agent"] = gen

    res = await TestExecutionAgent().run(ctx)
    assert res.output["status"] == "simulated"
    assert res.output["passed"] == 0
    assert all(r["status"] == "simulated" for r in res.output["results"])

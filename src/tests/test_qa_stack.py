"""Assertions about a running stack — the second half of testing a migration.

A stub Airflow stands in for the real one so these run in a second and without
Docker. What they pin is the judgement, not the transport: an import error must FAIL
even though Airflow itself returns 200 and carries on, because a DAG that does not
import is not a broken pipeline — it is an absent one, and nothing else anywhere
goes red.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.migration import runtime
from src.qatest import stack

# What the stub serves, per path. Rewritten per test.
RESPONSES: dict[str, tuple[int, dict]] = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                          # noqa: N802
        status, body = RESPONSES.get(self.path, (404, {"detail": "not found"}))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a):                                # noqa: D102
        pass


class _FastServer(HTTPServer):
    """HTTPServer without the reverse DNS lookup.

    `server_bind` calls `socket.getfqdn()`, which on macOS blocks for ~20 seconds
    when the network is unhelpful — turning a fixture that should be instant into the
    slowest thing in the suite.
    """

    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


@pytest.fixture
def airflow():
    server = _FastServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _run(base, assertion_id):
    found = next(a for a in stack.AIRFLOW.assertions if a.assertion_id == assertion_id)
    return stack.run_assertion(base, found)


# ── Import errors: the one that matters ─────────────────────────────────────

def test_a_dag_that_does_not_import_fails_the_run(airflow):
    """Airflow answers 200 and carries on; the DAG is simply never registered. If
    this passed, a migration that produced nothing usable would look clean."""
    RESPONSES.clear()
    RESPONSES["/api/v1/importErrors"] = (200, {"import_errors": [
        {"filename": "/opt/airflow/dags/claims_intake.py",
         "stack_trace": "Traceback…\nModuleNotFoundError: No module named 'wf_compat'"}]})

    ok, detail = _run(airflow, "stack-001")
    assert not ok
    assert "claims_intake.py" in detail
    assert "wf_compat" in detail


def test_no_import_errors_passes(airflow):
    RESPONSES.clear()
    RESPONSES["/api/v1/importErrors"] = (200, {"import_errors": []})
    ok, detail = _run(airflow, "stack-001")
    assert ok and "no import errors" in detail


# ── DAGs registered ─────────────────────────────────────────────────────────

def test_a_running_airflow_with_no_dags_fails(airflow):
    """Everything is up and nothing was migrated. Green here would be the worst
    possible outcome of the whole flow."""
    RESPONSES.clear()
    RESPONSES["/api/v1/dags"] = (200, {"dags": []})
    ok, detail = _run(airflow, "stack-002")
    assert not ok and "registered no DAGs" in detail


def test_registered_dags_are_named(airflow):
    RESPONSES.clear()
    RESPONSES["/api/v1/dags"] = (200, {"dags": [
        {"dag_id": "claims_intake"}, {"dag_id": "payout_processing"}]})
    ok, detail = _run(airflow, "stack-002")
    assert ok and "claims_intake" in detail and "2 DAG(s)" in detail


# ── Health ──────────────────────────────────────────────────────────────────

def test_an_unhealthy_component_fails_and_names_it(airflow):
    RESPONSES.clear()
    RESPONSES["/health"] = (200, {"metadatabase": {"status": "unhealthy"},
                                  "scheduler": {"status": "healthy"}})
    ok, detail = _run(airflow, "stack-000")
    assert not ok and "metadatabase" in detail


def test_a_healthy_stack_passes(airflow):
    RESPONSES.clear()
    RESPONSES["/health"] = (200, {"metadatabase": {"status": "healthy"},
                                  "scheduler": {"status": "healthy"}})
    ok, _ = _run(airflow, "stack-000")
    assert ok


# ── Transport failures are failures, not crashes ────────────────────────────

def test_a_stack_that_is_not_there_fails_cleanly():
    # A high port nothing is listening on: refused immediately, rather than the
    # privileged-port hang that :1 can produce.
    ok, detail = _run("http://127.0.0.1:9", "stack-000")
    assert not ok and "could not reach" in detail


def test_a_non_json_body_fails_rather_than_raising(airflow):
    RESPONSES.clear()
    RESPONSES["/api/v1/dags"] = (200, {})
    ok, detail = _run(airflow, "stack-002")
    assert not ok


# ── The generated stack ─────────────────────────────────────────────────────

def test_the_airflow_runtime_ships_what_compose_needs():
    stack_def = runtime.for_target("airflow")
    assert stack_def is not None
    assert "docker-compose.yml" in stack_def.files
    body = stack_def.files["docker-compose.yml"]
    assert "apache/airflow" in body
    assert "8080:8080" in body
    # The API must be reachable with basic auth, or none of the assertions can run.
    assert "basic_auth" in body
    assert stack_def.health_path == "/health"
    # Airflow migrates a database on first boot; 60s would fail every time.
    assert stack_def.start_timeout_s >= 180


def test_an_unknown_target_ships_no_stack():
    """Most targets are code-only. That is not an error."""
    assert runtime.for_target("step-functions") is None
    assert runtime.for_target("") is None


def test_the_generated_compose_says_it_is_not_for_production():
    body = runtime.for_target("airflow").files["docker-compose.yml"]
    assert "NOT a production deployment" in body
    assert "RUNNING.md" in runtime.for_target("airflow").files

"""Router-level contract tests: RBAC, the WebSocket handshake, and replay.

The socket contract matters disproportionately: without seq-based replay, one
dropped connection loses the whole run and the operator watches a frozen progress
bar — the most likely bad-demo moment in the whole feature.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.routers.auth import get_current_user
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)

OPS = {"userId": "u1", "username": "ops", "role": "user_ops",
       "permissions": ROLE_PERMISSIONS["user_ops"]}
DEV = {"userId": "u2", "username": "dev", "role": "user_dev",
       "permissions": ROLE_PERMISSIONS["user_dev"]}


def _as(user: dict):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _cleanup():
    """Restore, do not pop.

    test_ontology_lens_api.py installs its own get_current_user override at MODULE
    import time. Popping ours would delete theirs too, and every lens test would then
    fail depending on collection order.
    """
    previous = app.dependency_overrides.get(get_current_user)
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


# ── RBAC ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/observability/kpis", "/api/observability/providers",
    "/api/observability/runbooks", "/api/observability/learned",
    "/api/observability/incidents", "/api/observability/investigations",
])
def test_ops_can_read_every_surface(path, fake_dynamo, fake_graph):
    _as(OPS)
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", [
    "/api/observability/kpis", "/api/observability/investigations",
    "/api/observability/learned",
])
def test_dev_role_is_denied(path):
    _as(DEV)
    assert client.get(path).status_code == 403


def test_observability_is_granted_to_the_right_roles():
    for role in ("user_ops", "admin", "super_admin"):
        assert "observability" in ROLE_PERMISSIONS[role], role
    for role in ("user_dev", "user_qa", "ontology_maintainer"):
        assert "observability" not in ROLE_PERMISSIONS[role], role


def test_user_ops_can_reach_connectors():
    """Otherwise an ops user sees 'not configured' everywhere and then hits
    Access Denied when they click Configure."""
    assert "connectors" in ROLE_PERMISSIONS["user_ops"]


# ── Runbook fallback ─────────────────────────────────────────────────────────

def test_runbook_search_always_returns_something(fake_dynamo, fake_graph):
    _as(OPS)
    body = client.get("/api/observability/runbooks?service=checkout-service").json()
    assert body["runbooks"], "search must fall back to the SOP aiops template"
    assert body["runbooks"][0]["origin"] == "template"
    assert len(body["runbooks"][0]["steps"]) == 7


# ── WebSocket ────────────────────────────────────────────────────────────────

def test_ws_rejects_a_bad_token():
    with client.websocket_connect("/api/observability/ws/investigate") as ws:
        ws.send_json({"token": "garbage", "investigationId": "X"})
        assert ws.receive_json()["message"] == "Unauthorized"


def test_ws_checks_the_permission_not_just_the_token(monkeypatch):
    """routers/aiops.py::ws_live_alerts verifies the token but never checks the
    aiops permission. That gap is deliberately not replicated here."""
    import src.services.auth_service as auth
    monkeypatch.setattr(auth, "verify_token", lambda t: DEV)
    with client.websocket_connect("/api/observability/ws/investigate") as ws:
        ws.send_json({"token": "valid-but-wrong-role", "investigationId": "X"})
        assert "Permission denied" in ws.receive_json()["message"]


def test_ws_requires_an_investigation_id(monkeypatch):
    import src.services.auth_service as auth
    monkeypatch.setattr(auth, "verify_token", lambda t: OPS)
    with client.websocket_connect("/api/observability/ws/investigate") as ws:
        ws.send_json({"token": "ok"})
        assert "investigationId" in ws.receive_json()["message"]


@pytest.fixture
def seeded_stream(monkeypatch):
    import src.services.auth_service as auth
    import src.routers.observability as obs
    monkeypatch.setattr(auth, "verify_token", lambda t: OPS)
    obs._STREAMS["INV-WS"] = [
        {"type": "dag_start", "seq": 1, "investigationId": "INV-WS", "runId": "r1"},
        {"type": "stage_start", "seq": 2, "investigationId": "INV-WS", "stage": 1,
         "title": "Collect Signals", "agents": ["obs_signal_collector"]},
        {"type": "agent_done", "seq": 3, "investigationId": "INV-WS",
         "agent": "obs_signal_collector", "stage": 1, "status": "success"},
        {"type": "dag_done", "seq": 4, "investigationId": "INV-WS", "status": "success"},
    ]
    yield
    obs._STREAMS.pop("INV-WS", None)


def test_ws_replays_the_whole_run_from_zero(seeded_stream):
    with client.websocket_connect("/api/observability/ws/investigate") as ws:
        ws.send_json({"token": "ok", "investigationId": "INV-WS", "sinceSeq": 0})
        hello = ws.receive_json()
        assert hello["type"] == "connected" and hello["replaying"] == 4
        seqs = [ws.receive_json()["seq"] for _ in range(4)]
        assert seqs == [1, 2, 3, 4]


def test_ws_reconnect_skips_already_delivered_events(seeded_stream):
    """This is what stops a dropped socket from freezing the progress bar."""
    with client.websocket_connect("/api/observability/ws/investigate") as ws:
        ws.send_json({"token": "ok", "investigationId": "INV-WS", "sinceSeq": 2})
        hello = ws.receive_json()
        assert hello["replaying"] == 2
        assert [ws.receive_json()["seq"] for _ in range(2)] == [3, 4]


def test_ws_replay_falls_back_to_durable_rows(monkeypatch, fake_dynamo):
    """Under multiple workers a reconnect can land on a worker that never ran the
    investigation. The in-memory buffer misses; DynamoDB must cover it."""
    import src.services.auth_service as auth
    import src.routers.observability as obs
    monkeypatch.setattr(auth, "verify_token", lambda t: OPS)
    obs._STREAMS.pop("INV-COLD", None)          # simulate the other worker
    for seq in (1, 2, 3):
        fake_dynamo.put_item("observability-traces", {
            "runId": "INV-COLD", "seq": f"{seq:06d}", "type": "agent_done",
            "event": {"type": "agent_done", "seq": seq, "investigationId": "INV-COLD"}})
    monkeypatch.setattr(obs, "_replay_events",
                        lambda iid, since: [r["event"] for r in
                                            fake_dynamo.scan_items("observability-traces")
                                            if r["runId"] == iid
                                            and int(r["event"]["seq"]) > since])
    with client.websocket_connect("/api/observability/ws/investigate") as ws:
        ws.send_json({"token": "ok", "investigationId": "INV-COLD", "sinceSeq": 1})
        hello = ws.receive_json()
        assert hello["replaying"] == 2
        assert [ws.receive_json()["seq"] for _ in range(2)] == [2, 3]


# ── Regression: projectless investigations ───────────────────────────────────

def test_investigation_without_a_project_persists(fake_dynamo, fake_graph, fake_s3,
                                                  monkeypatch):
    """An empty string is not a legal GSI key value.

    `projectId` is the partition key of projectId-createdAt-index, so writing "" for
    a project-less investigation made real DynamoDB reject the entire row with a
    ValidationException — a 500 on the very first click of Investigate. The row is
    now written without the attribute, making that index sparse.
    """
    _as(OPS)
    monkeypatch.setattr("fastapi.BackgroundTasks.add_task", lambda self, fn: None)

    r = client.post("/api/observability/investigations",
                    json={"services": ["checkout-service"], "symptom": "5xx spike",
                          "window_minutes": 60, "background": True})
    assert r.status_code == 200, r.text
    inv_id = r.json()["investigationId"]

    rows = fake_dynamo.tables["observability-investigations"]
    assert len(rows) == 1
    assert "projectId" not in rows[0], "an empty GSI key must be omitted, not stored"
    assert rows[0]["serviceName"] == "checkout-service"
    assert rows[0]["investigationId"] == inv_id

    assert client.get(f"/api/observability/investigations/{inv_id}").status_code == 200


def test_investigation_keeps_a_real_project_id(fake_dynamo, fake_graph, fake_s3,
                                               monkeypatch):
    _as(OPS)
    monkeypatch.setattr("fastapi.BackgroundTasks.add_task", lambda self, fn: None)
    client.post("/api/observability/investigations",
                json={"services": ["checkout-service"], "project_id": "proj-1",
                      "window_minutes": 60, "background": True})
    row = fake_dynamo.tables["observability-investigations"][0]
    assert row["projectId"] == "proj-1"


def test_service_less_investigation_also_persists(fake_dynamo, fake_graph, fake_s3,
                                                  monkeypatch):
    """serviceName is the other GSI partition key — same failure mode."""
    _as(OPS)
    monkeypatch.setattr("fastapi.BackgroundTasks.add_task", lambda self, fn: None)
    r = client.post("/api/observability/investigations",
                    json={"services": [], "symptom": "unknown", "window_minutes": 60,
                          "background": True})
    assert r.status_code == 200, r.text
    row = fake_dynamo.tables["observability-investigations"][0]
    assert "serviceName" not in row and "projectId" not in row

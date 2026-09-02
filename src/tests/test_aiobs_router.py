"""AI Observability router: tenant scoping, the Overview aggregation, and feedback.

The scoping tests carry the most weight. Traces are written with tenantId set to the
ingesting credential's userId, but nothing read it back — so any holder of
`dev_workspace` could read every other user's prompts and completions, and
GET /projects full-scanned the trace table with no tenant predicate at all. These
tests are what stop that regressing.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.aiobs import service
from src.main import app
from src.routers.auth import get_current_user
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)

ALICE = {"userId": "alice", "username": "alice", "role": "user_dev",
         "permissions": ROLE_PERMISSIONS["user_dev"]}
BOB = {"userId": "bob", "username": "bob", "role": "user_dev",
       "permissions": ROLE_PERMISSIONS["user_dev"]}
ADMIN = {"userId": "root", "username": "root", "role": "admin",
         "permissions": ROLE_PERMISSIONS["admin"]}

BASE = "/api/ai-observability"


def _as(user: dict):
    app.dependency_overrides[get_current_user] = lambda: user


class FakeStore:
    """Minimal TraceStore double that records how it was called.

    Deliberately does NOT filter by itself — the point is to assert that the ROUTER
    passes the tenant down, not to re-test a store implementation.
    """
    name = "fake"

    def __init__(self, rows=None, caps=None):
        self.rows = rows or []
        self.caps = caps or {"store": "fake", "fullTextSearch": True,
                             "aggregations": True, "degraded": False}
        self.calls: list[dict] = []
        self.scores: list[tuple] = []

    def list_traces(self, project_id, limit=50, status="", thread_id="",
                    search="", tenant_id=""):
        self.calls.append({"project_id": project_id, "search": search,
                           "tenant_id": tenant_id, "status": status})
        rows = self.rows
        if tenant_id:
            rows = [r for r in rows if r.get("tenantId") in ("", None, tenant_id)]
        return rows[:limit]

    def get_trace(self, project_id, trace_id):
        return next((r for r in self.rows if r.get("traceId") == trace_id), None)

    def get_spans(self, trace_id, limit=1000, project_id=""):
        return []

    def list_threads(self, project_id, limit=50):
        return []

    def list_projects(self, tenant_id=""):
        self.calls.append({"list_projects_tenant": tenant_id})
        seen = {r["projectId"] for r in self.rows
                if not tenant_id or r.get("tenantId") in ("", None, tenant_id)}
        return [{"projectId": p, "traceCount": 1, "costUsd": 0.0, "lastSeen": ""}
                for p in sorted(seen)]

    def record_scores(self, project_id, trace, scores):
        self.scores.append((project_id, trace.get("traceId"), list(scores)))
        return True

    def capabilities(self):
        return self.caps


ROWS = [
    {"traceId": "t-alice", "projectId": "p", "tenantId": "alice", "status": "ok",
     "startTime": "2026-09-01T10:00:00Z", "latencyMs": 100, "costUsd": 0.01,
     "totalTokens": 500, "providers": ["anthropic"],
     "onlineScores": [{"name": "relevance", "value": 0.9}]},
    {"traceId": "t-bob", "projectId": "p2", "tenantId": "bob", "status": "error",
     "startTime": "2026-09-02T10:00:00Z", "latencyMs": 900, "costUsd": 0.05,
     "totalTokens": 1500, "providers": ["openai"], "onlineScores": []},
]


@pytest.fixture
def store():
    s = FakeStore(rows=list(ROWS))
    service.set_store(s)
    yield s
    service.set_store(None)


@pytest.fixture(autouse=True)
def _cleanup():
    previous = app.dependency_overrides.get(get_current_user)
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


# ── Tenant scoping ────────────────────────────────────────────────────────────

def test_traces_are_scoped_to_the_callers_tenant(store):
    _as(ALICE)
    res = client.get(f"{BASE}/traces?projectId=p")
    assert res.status_code == 200
    assert [t["traceId"] for t in res.json()["traces"]] == ["t-alice"]
    assert store.calls[-1]["tenant_id"] == "alice"


def test_an_admin_sees_every_tenant(store):
    """One company, multiple teams: an admin seeing everything is correct. What is
    not correct is an ordinary user reading a colleague's prompts."""
    _as(ADMIN)
    res = client.get(f"{BASE}/traces?projectId=p")
    assert store.calls[-1]["tenant_id"] == ""
    assert len(res.json()["traces"]) == 2


def test_another_tenants_trace_is_404_not_403(store):
    """403 confirms the id exists, which is itself a leak when ids are guessable."""
    _as(ALICE)
    assert client.get(f"{BASE}/traces/t-bob?projectId=p2").status_code == 404
    assert client.get(f"{BASE}/traces/t-alice?projectId=p").status_code == 200


def test_projects_are_scoped_and_no_longer_scanned(store):
    """This endpoint used to do scan_items("ai-traces", limit=2000) with no tenant
    predicate — a cross-tenant enumeration of project names."""
    _as(BOB)
    res = client.get(f"{BASE}/projects")
    assert [p["projectId"] for p in res.json()["projects"]] == ["p2"]
    assert store.calls[-1]["list_projects_tenant"] == "bob"


def test_span_payload_is_scoped(store):
    """The endpoint that returns raw prompt text is the one that matters most."""
    _as(ALICE)
    res = client.get(f"{BASE}/traces/t-bob/spans/s1/payload?which=input")
    assert res.status_code == 404


# ── Search honesty ────────────────────────────────────────────────────────────

def test_search_is_forwarded_when_the_store_supports_it(store):
    _as(ALICE)
    client.get(f"{BASE}/traces?projectId=p&search=hello")
    assert store.calls[-1]["search"] == "hello"


def test_search_is_refused_rather_than_ignored(store):
    """Silently dropping the term would hand the caller a COMPLETE list they believe
    was filtered. A 400 is the honest answer."""
    store.caps = {**store.caps, "fullTextSearch": False}
    _as(ALICE)
    res = client.get(f"{BASE}/traces?projectId=p&search=hello")
    assert res.status_code == 400
    assert "not available" in res.json()["detail"]


# ── Overview aggregation ──────────────────────────────────────────────────────

def test_summary_reports_kpis_and_a_daily_series(store):
    _as(ADMIN)
    body = client.get(f"{BASE}/summary?projectId=p&limit=500").json()
    k = body["kpis"]
    assert k["traces"] == 2
    assert k["errors"] == 1
    assert k["errorRate"] == 0.5
    assert k["costUsd"] == pytest.approx(0.06)
    assert k["totalTokens"] == 2000
    # Nearest-rank, not interpolated: with two samples an interpolated p95 would
    # invent a latency neither request had.
    assert k["p50LatencyMs"] in (100, 900)
    assert k["p95LatencyMs"] == 900
    assert [d["day"] for d in body["daily"]] == ["2026-09-01", "2026-09-02"]
    assert body["providers"][0]["traces"] == 1
    assert body["scores"] == [{"name": "relevance", "mean": 0.9, "count": 1}]


def test_summary_admits_when_it_is_a_sample(store):
    """A dashboard that reports a partial sum as a total is worse than one that says
    it is bounded."""
    _as(ADMIN)
    assert client.get(f"{BASE}/summary?projectId=p&limit=500").json()["window"]["exact"] is True
    assert client.get(f"{BASE}/summary?projectId=p&limit=2").json()["window"]["exact"] is False


def test_summary_is_tenant_scoped(store):
    _as(ALICE)
    assert client.get(f"{BASE}/summary?projectId=p").json()["kpis"]["traces"] == 1


def test_summary_surfaces_a_degraded_store(store):
    store.caps = {**store.caps, "degraded": True}
    _as(ADMIN)
    assert client.get(f"{BASE}/summary?projectId=p").json()["degraded"] is True


# ── Human feedback ────────────────────────────────────────────────────────────

def test_feedback_is_stored_through_the_store(store):
    _as(ALICE)
    res = client.put(f"{BASE}/traces/t-alice/feedback?projectId=p",
                     json={"name": "thumbs", "value": 1.0, "reason": "great"})
    assert res.status_code == 200
    project, trace_id, scores = store.scores[-1]
    assert (project, trace_id) == ("p", "t-alice")
    assert scores[0].name == "thumbs"
    # The reason is attributed, so a score can be traced back to who left it.
    assert "alice" in scores[0].reason


@pytest.mark.parametrize("value", [-0.1, 1.5, "high", None])
def test_feedback_rejects_an_out_of_range_or_non_numeric_value(store, value):
    _as(ALICE)
    res = client.put(f"{BASE}/traces/t-alice/feedback?projectId=p",
                     json={"name": "thumbs", "value": value})
    assert res.status_code == 400


def test_feedback_cannot_be_left_on_another_tenants_trace(store):
    _as(ALICE)
    res = client.put(f"{BASE}/traces/t-bob/feedback?projectId=p2",
                     json={"name": "thumbs", "value": 1.0})
    assert res.status_code == 404


def test_feedback_surfaces_a_store_rejection(store):
    """A silent success on a score that was not stored would make online eval and
    human review quietly disagree."""
    store.record_scores = lambda *a, **k: False
    _as(ALICE)
    res = client.put(f"{BASE}/traces/t-alice/feedback?projectId=p",
                     json={"name": "thumbs", "value": 1.0})
    assert res.status_code == 503


# ── The browser-facing Opik URL ───────────────────────────────────────────────

def test_capabilities_never_derives_the_opik_ui_url(store, monkeypatch):
    """The URL must be EXPLICIT, never inferred from opik_enabled.

    An earlier version derived "<request host>:<opik_ui_port>" whenever opik_enabled
    was true. That conflated two unrelated facts: opik_enabled means "Aura may WRITE
    spans to Opik", which says nothing about whether a browser can reach Opik's UI. In
    the single-instance deployment there is no listener on that port, so the SPA got a
    dead address, pointed an iframe at it, and rendered a blank panel — the least
    debuggable failure available.

    Only the infrastructure knows whether a listener exists, so Terraform supplies this
    and supplies it EMPTY when it does not.
    """
    from src.config_settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "opik_enabled", True, raising=False)
    monkeypatch.setattr(s, "opik_ui_url", "", raising=False)
    monkeypatch.setattr(s, "opik_ui_port", 8081, raising=False)

    _as(ADMIN)
    body = client.get(f"{BASE}/capabilities", headers={"Host": "aura.example.com"}).json()
    # Enabled for WRITES, but no browser address — and the two are independent.
    assert body["opikEnabled"] is True
    assert body["opikUiUrl"] == "", "must not invent a URL from the request host"


def test_explicit_opik_ui_url_wins(store, monkeypatch):
    from src.config_settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "opik_enabled", True, raising=False)
    monkeypatch.setattr(s, "opik_ui_url", "https://opik.internal", raising=False)
    _as(ADMIN)
    assert client.get(f"{BASE}/capabilities").json()["opikUiUrl"] == "https://opik.internal"


def test_no_opik_url_when_the_stack_is_disabled(store, monkeypatch):
    """With Opik off, the UI must not offer a tab that would 502."""
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "opik_enabled", False, raising=False)
    _as(ADMIN)
    body = client.get(f"{BASE}/capabilities").json()
    assert body["opikEnabled"] is False
    assert body["opikUiUrl"] == ""


# ── Demo agents ───────────────────────────────────────────────────────────────
# The trigger is a forwarder to a service that only exists in a demo environment.
# What matters is that it is inert and honest everywhere else, rather than 500ing.

class _StubResponse:
    def __init__(self, payload): self._payload = payload
    def raise_for_status(self): pass
    def json(self): return self._payload


def test_demo_run_is_503_when_no_demo_service_is_configured(monkeypatch):
    """Not deployed is a CONFIGURATION answer, not a fault. 503 tells the UI to hide
    the button; a 500 would send someone hunting for a bug that does not exist."""
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "demo_agents_url", "", raising=False)
    _as(ALICE)
    res = client.post(f"{BASE}/demo/run")
    assert res.status_code == 503
    assert "not deployed" in res.json()["detail"].lower()


def test_demo_run_forwards_agent_and_count(monkeypatch):
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "demo_agents_url", "http://demo-agents:8080",
                        raising=False)
    seen = {}

    def fake_post(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return _StubResponse({"triggered": ["rag"], "count": 3})

    monkeypatch.setattr("httpx.post", fake_post)
    _as(ALICE)
    res = client.post(f"{BASE}/demo/run", params={"agent": "rag", "count": 3})
    assert res.status_code == 200
    assert seen["url"] == "http://demo-agents:8080/run/rag"
    assert seen["params"] == {"count": 3}


def test_demo_run_rejects_an_unknown_agent(monkeypatch):
    """Caught here rather than forwarded, so a typo is a 400 with the valid list
    instead of a 404 from a service the caller cannot see."""
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "demo_agents_url", "http://demo-agents:8080",
                        raising=False)
    _as(ALICE)
    res = client.post(f"{BASE}/demo/run", params={"agent": "nope"})
    assert res.status_code == 400
    assert "rag" in res.json()["detail"]


def test_demo_run_caps_the_burst_size(monkeypatch):
    """A button that can be clicked forty times is a button that will be."""
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "demo_agents_url", "http://demo-agents:8080",
                        raising=False)
    _as(ALICE)
    assert client.post(f"{BASE}/demo/run", params={"count": 99}).status_code == 422


def test_demo_run_reports_an_unreachable_service_as_502(monkeypatch):
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "demo_agents_url", "http://demo-agents:8080",
                        raising=False)

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.post", boom)
    _as(ALICE)
    res = client.post(f"{BASE}/demo/run")
    assert res.status_code == 502
    assert "connection refused" in res.json()["detail"]


def test_demo_status_never_raises_when_the_service_is_down(monkeypatch):
    """This is the endpoint someone opens BECAUSE they think the demo is broken.
    Answering with a 500 would tell them nothing."""
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "demo_agents_url", "http://demo-agents:8080",
                        raising=False)

    def boom(*a, **k):
        raise RuntimeError("timed out")

    monkeypatch.setattr("httpx.get", boom)
    _as(ALICE)
    body = client.get(f"{BASE}/demo/status").json()
    assert body["deployed"] is True and body["reachable"] is False


def test_capabilities_reports_demo_agents_off_by_default(monkeypatch):
    from src.config_settings import get_settings
    monkeypatch.setattr(get_settings(), "demo_agents_url", "", raising=False)
    _as(ALICE)
    assert client.get(f"{BASE}/capabilities").json()["demoAgentsEnabled"] is False

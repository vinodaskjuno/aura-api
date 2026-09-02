"""The auth gate in front of Opik, and the agent-onboarding surface.

The gate matters more than it looks. Open-source Opik has NO authentication: with
AUTH_ENABLED=false its AuthModule installs a no-op auth service and every REST
endpoint answers without a credential (verified against a live 2.2.46 — the whole
project list came back with no headers at all). nginx therefore gates /opik/ with an
`auth_request` against `/api/ai-observability/opik-authz`, and if that endpoint ever
starts saying 204 too easily, every prompt and completion in the deployment is
readable by anyone who can reach the host.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.routers.auth import get_current_user
from src.routers.opik_gateway import COOKIE_NAME
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)

DEV = {"userId": "u-dev", "username": "dev", "role": "user_dev",
       "permissions": ROLE_PERMISSIONS["user_dev"]}
# user_ops has no dev_workspace, so it must not be able to open Opik.
OPS = {"userId": "u-ops", "username": "ops", "role": "user_ops",
       "permissions": ROLE_PERMISSIONS["user_ops"]}

AUTHZ = "/api/ai-observability/opik-authz"
SESSION = "/api/ai-observability/opik-session"


def _as(user: dict):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _cleanup():
    previous = app.dependency_overrides.get(get_current_user)
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous
    client.cookies.clear()


# ── The gate ──────────────────────────────────────────────────────────────────

def test_authz_denies_with_no_cookie():
    """The default answer must be no. This endpoint is the only thing standing
    between an unauthenticated Opik and the internet."""
    assert client.get(AUTHZ).status_code == 401


def test_authz_denies_a_garbage_cookie():
    client.cookies.set(COOKIE_NAME, "not-a-jwt", path="/opik")
    assert client.get(AUTHZ).status_code == 401


def test_authz_denies_a_normal_aura_session_jwt():
    """The cookie is a SEPARATE narrow-audience token, not the session JWT. A
    session JWT presented here must fail, so that a cookie leaked from the Opik
    origin cannot be replayed against Aura's API and vice versa."""
    from src.services.auth_service import create_token
    session_jwt = create_token({"username": "dev", "userId": "u-dev",
                                "role": "user_dev", "permissions": ["dev_workspace"]})
    client.cookies.set(COOKIE_NAME, session_jwt, path="/opik")
    assert client.get(AUTHZ).status_code == 401


def _authz_as_nginx_would(cookie_value: str):
    """Call the gate the way nginx's auth_request does.

    Passed explicitly rather than relying on the test client's cookie jar, so the
    test asserts the header the gate actually reads instead of whatever the jar
    happened to retain between requests.
    """
    return client.get(AUTHZ, headers={"Cookie": f"{COOKIE_NAME}={cookie_value}"})


def test_session_then_authz_allows():
    _as(DEV)
    res = client.post(SESSION)
    assert res.status_code == 204
    cookie = res.headers["set-cookie"].split(";")[0].split("=", 1)[1]

    res = _authz_as_nginx_would(cookie)
    assert res.status_code == 204
    # Handed on to Opik so its created_by column names the real Aura user rather
    # than always saying "admin".
    assert res.headers["Comet-Workspace"] == "default"
    assert res.headers["X-Aura-User"] == "dev"


def test_session_cookie_is_httponly_and_lax():
    """HttpOnly so Opik's own bundle cannot read it. Path is "/" because Opik is
    served at the root of its OWN PORT, not under /opik — a path-scoped cookie would
    never be presented there. SameSite=Lax is still right: a different port on the
    same host is the same site, so the cookie survives the iframe navigation.

    The narrowing that matters is the audience claim, not the path — see
    test_authz_denies_a_normal_aura_session_jwt."""
    _as(DEV)
    res = client.post(SESSION)
    header = res.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "path=/" in header
    assert "samesite=lax" in header


def test_session_requires_dev_workspace():
    """A role without the permission cannot mint the cookie, so cannot open Opik."""
    _as(OPS)
    assert client.post(SESSION).status_code == 403


def test_expired_cookie_is_denied(monkeypatch):
    import src.routers.opik_gateway as gw
    monkeypatch.setattr(gw, "COOKIE_TTL_MINUTES", -1)
    _as(DEV)
    res = client.post(SESSION)
    cookie = res.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert _authz_as_nginx_would(cookie).status_code == 401


def test_closing_the_session_clears_the_cookie():
    """Called on logout so a shared machine does not leave Opik open to whoever
    sits down next."""
    _as(DEV)
    client.post(SESSION)
    res = client.delete(SESSION)
    assert res.status_code == 204
    header = res.headers["set-cookie"].lower()
    # Either form expires it; Starlette emits max-age=0 plus a past expires.
    assert "max-age=0" in header or "expires=thu, 01 jan 1970" in header
    assert "path=/" in header


# ── The gate, for SDKs and CI ─────────────────────────────────────────────────
# A browser can only present a cookie; a navigation cannot carry an Authorization
# header. An SDK is the mirror image — it has no cookie and presents a key. The
# Onboard tab GENERATES snippets that do exactly that, so if these fail, the
# documented onboarding path is broken for every customer who follows it.

def _key_for(style: str = "opik-sdk", project: str = "demo") -> str:
    res = client.post("/api/ai-observability/onboarding",
                      json={"style": style, "projectName": project})
    assert res.status_code == 200, res.text
    key = res.json()["apiKey"]
    assert key.startswith("gw-"), f"expected a plaintext key on creation, got {key!r}"
    return key


def test_authz_accepts_a_gateway_key_via_bearer(fake_dynamo):
    """The regression this fix exists for: a key minted by the Onboard tab,
    presented the way its own snippet says to, used to come back 401."""
    _as(DEV)
    key = _key_for()
    res = client.get(AUTHZ, headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 204
    assert res.headers["Comet-Workspace"] == "default"


def test_authz_accepts_a_gateway_key_via_x_api_key(fake_dynamo):
    """The `anthropic` SDK sends x-api-key, not Authorization."""
    _as(DEV)
    key = _key_for()
    assert client.get(AUTHZ, headers={"x-api-key": key}).status_code == 204


def test_authz_accepts_a_bare_authorization_token(fake_dynamo):
    """The Opik SDK sends `Authorization: <key>` with NO Bearer scheme. Unusual
    enough that it is worth pinning — this is the header the demo agents send."""
    _as(DEV)
    key = _key_for()
    assert client.get(AUTHZ, headers={"Authorization": key}).status_code == 204


def test_authz_denies_a_revoked_key(fake_dynamo):
    """Revocation has to actually close the door: a key is a long-lived credential
    for an unauthenticated Opik, and deactivating it is the only way to withdraw it."""
    _as(DEV)
    key = _key_for()
    assert client.get(AUTHZ, headers={"x-api-key": key}).status_code == 204
    from src.config_settings import get_settings
    for row in fake_dynamo.tables[get_settings().gateway_keys_table]:
        row["active"] = False
    assert client.get(AUTHZ, headers={"x-api-key": key}).status_code == 401


def test_authz_denies_a_key_whose_role_lacks_dev_workspace(fake_dynamo):
    """A key must never be a WIDER grant than a browser session. The cookie is
    only minted for dev_workspace holders, so the key path checks the same thing."""
    _as(DEV)
    key = _key_for()
    # resolve_credential reads the role off the users table, not off the key —
    # so demoting the user is what withdraws the key's access.
    fake_dynamo.put_item("users", {"userId": "u-dev", "username": "dev",
                                   "roleId": "user_ops"})
    assert client.get(AUTHZ, headers={"x-api-key": key}).status_code == 401


def test_authz_denies_a_made_up_key(fake_dynamo):
    _as(DEV)
    assert client.get(AUTHZ, headers={"x-api-key": "gw-nope"}).status_code == 401


def test_authz_still_denies_when_nothing_is_presented(fake_dynamo):
    """Guard against the fallback turning the gate into a no-op."""
    _as(DEV)
    assert client.get(AUTHZ).status_code == 401


# ── Onboarding ────────────────────────────────────────────────────────────────

def test_onboarding_provisions_a_key_and_returns_a_snippet(fake_dynamo):
    _as(DEV)
    res = client.post("/api/ai-observability/onboarding",
                      json={"style": "opik-sdk", "projectName": "checkout-agent"})
    assert res.status_code == 200
    body = res.json()
    assert body["projectName"] == "checkout-agent"
    assert body["toolLabel"] == "opik-sdk"
    labels = [s["label"] for s in body["snippets"]]
    assert labels == ["Install", "Configure", "Instrument"]
    configure = next(s for s in body["snippets"] if s["label"] == "Configure")
    assert "OPIK_URL_OVERRIDE" in configure["code"]
    assert "checkout-agent" in configure["code"]


def test_onboarding_is_get_or_create_not_always_create(fake_dynamo):
    """Revisiting the page must not invalidate a key a team already deployed."""
    _as(DEV)
    payload = {"style": "opik-sdk", "projectName": "a"}
    first = client.post("/api/ai-observability/onboarding", json=payload).json()
    second = client.post("/api/ai-observability/onboarding", json=payload).json()
    # Same key both times: only the first call can hand back plaintext, but the hint
    # identifies it and must match.
    assert first["isNewKey"] is True
    assert second["isNewKey"] is False
    assert first["apiKeyHint"] and first["apiKeyHint"] == second["apiKeyHint"]


def test_otel_style_uses_the_otlp_endpoint_and_its_own_label(fake_dynamo):
    """A raw OTel exporter goes to /otlp, not the Opik API, and is labelled
    separately so usage can be attributed per integration style."""
    _as(DEV)
    body = client.post("/api/ai-observability/onboarding",
                       json={"style": "otel-sdk", "projectName": "svc"}).json()
    assert body["toolLabel"] == "otel-sdk"
    configure = next(s for s in body["snippets"] if s["label"] == "Configure")
    assert "/otlp" in configure["code"]
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in configure["code"]


@pytest.mark.parametrize("style", ["langchain", "crewai", "llamaindex",
                                   "anthropic", "openai", "bedrock", "typescript"])
def test_every_advertised_style_actually_renders(style, fake_dynamo):
    """The styles endpoint and the generator must not drift apart — an advertised
    style that 400s or KeyErrors is worse than one that was never offered."""
    _as(DEV)
    res = client.post("/api/ai-observability/onboarding",
                      json={"style": style, "projectName": "p"})
    assert res.status_code == 200, res.text
    assert res.json()["snippets"]


def test_styles_endpoint_matches_the_generator(fake_dynamo):
    _as(DEV)
    advertised = client.get("/api/ai-observability/onboarding/styles").json()["styles"]
    for style in advertised:
        res = client.post("/api/ai-observability/onboarding",
                          json={"style": style, "projectName": "p"})
        assert res.status_code == 200, f"{style} advertised but does not render"


def test_unknown_style_is_rejected_with_the_valid_list(fake_dynamo):
    _as(DEV)
    res = client.post("/api/ai-observability/onboarding",
                      json={"style": "smoke-signals", "projectName": "p"})
    assert res.status_code == 400
    assert "opik-sdk" in res.json()["detail"]


def test_snippets_never_leak_a_placeholder_when_a_real_key_exists(fake_dynamo):
    """If a key was minted, the snippet must contain it — printing
    'gw-<your key>' next to a real provisioned key is a support ticket."""
    _as(DEV)
    body = client.post("/api/ai-observability/onboarding",
                       json={"style": "opik-sdk", "projectName": "p"}).json()
    configure = next(s for s in body["snippets"] if s["label"] == "Configure")
    if body["isNewKey"]:
        assert body["apiKey"] in configure["code"]
        assert "<your key>" not in configure["code"]


def test_the_demo_agents_labels_are_provisionable(fake_dynamo):
    """Each demo agent needs its OWN key, which means its own tool label.

    The labels are an allowlist, so an agent whose label is missing cannot be
    provisioned at all — it fails with "Unknown tool_label" and then runs on synthetic
    spans, which looks like a working demo producing cheap traces. Pinned here because
    that failure is quiet and the demo is exactly where quiet failures hurt.
    """
    from src.routers.gateway_keys import _VALID_TOOL_LABELS
    for label in ("demo-rag", "demo-tools", "demo-chat", "demo-flaky", "demo-traces"):
        assert label in _VALID_TOOL_LABELS, f"{label} cannot be provisioned"

"""Aura's auth gate and onboarding surface in front of self-hosted Opik.

Why this file exists
--------------------
Open-source Opik has NO authentication. With `AUTH_ENABLED=false` its
`AuthModule` returns a no-op `AuthServiceImpl`, and every REST endpoint answers
without a credential of any kind — verified against a running 2.2.46, where
`GET /v1/private/projects` returned the full project list with no headers at all.
It also has exactly one workspace ("default"); named workspaces, SSO and roles are
Comet Cloud features.

So Opik must never be reachable except behind Aura. nginx enforces that with
`auth_request` pointing at `/api/ai-observability/opik-authz` below.

Why a COOKIE and not the Authorization header
---------------------------------------------
Aura's SPA keeps its JWT in localStorage and attaches it via an axios interceptor.
That works for XHR and is useless here: opening the Opik UI is a browser NAVIGATION
(an iframe src or a new tab), and a navigation cannot carry an Authorization header.
The only credential a browser will present on a navigation is a cookie.

`POST /opik-session` therefore exchanges the caller's normal JWT for a short-lived,
HttpOnly, SameSite=Lax cookie scoped to the Opik path. It is a SEPARATE token with a
narrow audience rather than the session JWT itself, so a cookie leaked from the Opik
origin cannot be replayed against Aura's own API.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from src.routers.auth import get_current_user, require_permission

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai-observability", tags=["ai-observability"])

_READ = require_permission("dev_workspace")

COOKIE_NAME = "aura_opik"
# Deliberately short. The cookie only has to survive opening the embedded UI, and
# the SPA re-mints it whenever the page is opened. A long-lived cookie sitting on a
# path that fronts an unauthenticated service is exactly what we are avoiding.
COOKIE_TTL_MINUTES = 60
_AUDIENCE = "opik-embed"


def _mint(user: dict) -> str:
    """A narrow-audience token for the Opik path only."""
    # python-jose, matching services/auth_service.py. PyJWT is not a dependency.
    from jose import jwt

    from src.config_settings import get_settings
    s = get_settings()
    payload = {
        "sub": user.get("username", ""),
        "userId": user.get("userId", ""),
        "role": user.get("role", ""),
        # Checked on the way back in. Without it, any Aura JWT would open Opik —
        # including one issued for a user who lacks dev_workspace.
        "aud": _AUDIENCE,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=COOKIE_TTL_MINUTES),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def _verify(token: str) -> dict | None:
    from jose import jwt

    from src.config_settings import get_settings
    try:
        return jwt.decode(token, get_settings().jwt_secret,
                          algorithms=["HS256"], audience=_AUDIENCE)
    except Exception:  # noqa: BLE001 — any decode failure is simply "not authorised"
        return None


@router.post("/opik-session", status_code=204)
def open_opik_session(user: dict = Depends(_READ)) -> Response:
    """Exchange the caller's JWT for the cookie that unlocks `/opik/`.

    Called by the UI immediately before rendering the embedded Opik view.

    The Response is CONSTRUCTED rather than injected-and-returned: returning the
    injected object hands FastAPI a response whose status_code was never set, which
    surfaces as a null status on the wire.
    """
    response = Response(status_code=204)
    response.set_cookie(
        COOKIE_NAME, _mint(user),
        max_age=COOKIE_TTL_MINUTES * 60,
        httponly=True,        # never readable from JS, including Opik's own bundle
        samesite="lax",
        # Path "/" rather than "/opik": Opik is served at the ROOT of its own port
        # now, so a path-scoped cookie would never be presented. SameSite=Lax is
        # still correct — a different PORT on the same host is the same site, so the
        # cookie is sent on the iframe navigation.
        #
        # The narrowing that matters is the audience claim, not the path: this token
        # is rejected by every endpoint except the gate.
        path="/",
    )
    return response


@router.delete("/opik-session", status_code=204)
def close_opik_session() -> Response:
    """Drop the cookie. Called on logout so a shared machine does not leave the
    Opik UI open to the next person."""
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/opik-authz")
def opik_authz(request: Request) -> Response:
    """nginx `auth_request` target. 204 to allow, 401 to deny.

    Returns no body on purpose: nginx discards it, and anything written here would
    just be latency on every single asset request for the embedded UI.
    """
    # Reached as an nginx `auth_request` subrequest, which inherits the original
    # request's headers — Cookie included. Also callable directly by the SPA to check
    # whether a session is still live.
    token = request.cookies.get(COOKIE_NAME, "")
    claims = _verify(token) if token else None
    if not claims:
        # Not logged as a warning: an expired cookie is the normal path, and this
        # endpoint is hit once per asset.
        return Response(status_code=401)
    return Response(status_code=204, headers={
        # Handed to Opik as its workspace/user context. OSS Opik has one workspace,
        # but it does read these headers, and echoing the Aura user makes its
        # created_by/last_updated_by columns meaningful instead of always "admin".
        "Comet-Workspace": "default",
        "X-Aura-User": str(claims.get("sub", ""))[:120],
    })


# ── Onboarding ────────────────────────────────────────────────────────────────

class OnboardRequest(BaseModel):
    # "opik-sdk" for @track-style instrumentation, "otel-sdk" for a raw exporter.
    style: str = "opik-sdk"
    projectName: str = "my-agent"


_SNIPPET_STYLES = ("opik-sdk", "otel-sdk", "langchain", "crewai", "llamaindex",
                   "anthropic", "openai", "bedrock", "typescript")


@router.get("/onboarding/styles")
def onboarding_styles(_: dict = Depends(_READ)):
    """Integration styles the snippet generator can produce."""
    return {"styles": list(_SNIPPET_STYLES), "default": "opik-sdk"}


@router.post("/onboarding")
def onboarding(body: OnboardRequest, user: dict = Depends(get_current_user)):
    """Provision a key and return copy-paste instrumentation for it.

    The key primitive already existed — `get_or_create_tool_key` mints `gw-` keys and
    the OTLP receiver already accepts them — but nothing ever rendered it, so
    "onboard your agent" had no product surface at all. This is that surface.

    Get-or-create rather than always-create: revisiting the page must not silently
    invalidate the key a team already deployed.
    """
    if body.style not in _SNIPPET_STYLES:
        raise HTTPException(status_code=400,
                            detail=f"Unknown style {body.style!r}. "
                                   f"Try one of: {', '.join(_SNIPPET_STYLES)}")

    from src.services.gateway_service import get_or_create_tool_key
    label = "opik-sdk" if body.style not in ("otel-sdk",) else "otel-sdk"
    key = get_or_create_tool_key(user.get("userId", ""), label)
    # Plaintext is only returned at mint time; a get on an existing key returns the
    # hint. Say which happened so the UI does not print "gw-••••" as if it were
    # pasteable.
    # generate_api_key returns the plaintext under "key" (and only on create);
    # get_or_create_tool_key returns "keyHint" on the get path. Read both names so
    # this cannot silently fall back to the placeholder.
    raw = key.get("key") or key.get("apiKey") or ""
    project = (body.projectName or "my-agent").strip()[:120] or "my-agent"
    # On CREATE the service returns the plaintext key and no hint; on GET it returns
    # the hint and no plaintext. Derive the missing half so the response shape is the
    # same either way and the UI does not have to branch.
    hint = key.get("keyHint") or (raw[-4:] if raw else "")

    return {
        "style": body.style,
        "projectName": project,
        "apiKey": raw,
        "apiKeyHint": hint,
        "isNewKey": bool(raw),
        "toolLabel": label,
        "snippets": _snippets(body.style, project, raw or "gw-<your key>"),
        "notes": _notes(body.style),
    }


def _base_url() -> str:
    """The host a customer's agent should export to.

    Left as a placeholder rather than guessed from the request: Aura sits behind an
    ALB and nginx, so `request.base_url` is frequently the internal address and a
    confidently-wrong endpoint is worse than an obvious blank.
    """
    from src.config_settings import get_settings
    return (get_settings().public_base_url or "https://<aura-host>").rstrip("/")


def _notes(style: str) -> list[str]:
    common = [
        "Traces group into projects by your OpenTelemetry `service.name` "
        "(or `aura.project`), so set it per agent.",
        "`session.id` becomes the thread id, which is what groups a whole "
        "conversation into one view.",
        "Ingest always answers HTTP 200, even on an auth failure — an observability "
        "endpoint must never make your exporter retry-loop. Check the "
        "`partialSuccess` field in the response body if traces do not appear.",
    ]
    if style == "opik-sdk":
        return common + [
            "The Opik SDK talks to Aura's Opik-compatible API, so every Opik "
            "integration (80+ frameworks) works unchanged.",
        ]
    return common


def _snippets(style: str, project: str, key: str) -> list[dict]:
    """Copy-paste blocks. Language tags match the UI's syntax highlighter."""
    base = _base_url()

    if style == "opik-sdk":
        return [
            {"label": "Install", "language": "bash", "code": "pip install opik"},
            {"label": "Configure", "language": "bash", "code":
                f"export OPIK_URL_OVERRIDE={base}/opik/api/\n"
                f"export OPIK_API_KEY={key}\n"
                f"export OPIK_WORKSPACE=default\n"
                f"export OPIK_PROJECT_NAME={project}"},
            {"label": "Instrument", "language": "python", "code":
                "from opik import track\n\n"
                "@track\n"
                "def retrieve(question: str) -> list[str]:\n"
                "    return search_docs(question)\n\n"
                "@track\n"
                "def agent(question: str) -> str:\n"
                "    docs = retrieve(question)\n"
                "    return llm(question, docs)\n\n"
                "# Nested @track calls become a span tree under one trace."},
        ]

    if style == "otel-sdk":
        return [
            {"label": "Install", "language": "bash", "code":
                "pip install opentelemetry-sdk opentelemetry-exporter-otlp"},
            {"label": "Configure", "language": "bash", "code":
                f"export OTEL_EXPORTER_OTLP_ENDPOINT={base}/otlp\n"
                f"export OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer\\ {key}\n"
                f"export OTEL_SERVICE_NAME={project}"},
            {"label": "Note", "language": "text", "code":
                "The Python OTLP HTTP exporter has no JSON mode, so it sends\n"
                "protobuf. Aura decodes both — see routers/otlp.py."},
        ]

    if style == "typescript":
        return [
            {"label": "Install", "language": "bash", "code": "npm install opik"},
            {"label": "Instrument", "language": "typescript", "code":
                'import { Opik } from "opik";\n\n'
                "const client = new Opik({\n"
                f'  apiUrl: "{base}/opik/api/",\n'
                f'  apiKey: "{key}",\n'
                '  workspaceName: "default",\n'
                f'  projectName: "{project}",\n'
                "});\n\n"
                'const trace = client.trace({ name: "agent", input: { q } });\n'
                'trace.span({ name: "llm", type: "llm", input: { q } }).end();\n'
                "trace.end();\n"
                "await client.flush();"},
        ]

    # Framework integrations: same configuration, one import each.
    integrations = {
        "langchain": ("python",
                      "from opik.integrations.langchain import OpikTracer\n\n"
                      f'tracer = OpikTracer(project_name="{project}")\n'
                      "chain.invoke({\"input\": q}, config={\"callbacks\": [tracer]})"),
        "crewai": ("python",
                   "from opik.integrations.crewai import track_crewai\n\n"
                   f'track_crewai(project_name="{project}")\n'
                   "crew.kickoff()"),
        "llamaindex": ("python",
                       "from opik.integrations.llama_index import "
                       "LlamaIndexCallbackHandler\n\n"
                       "from llama_index.core import Settings\n"
                       "from llama_index.core.callbacks import CallbackManager\n\n"
                       f'handler = LlamaIndexCallbackHandler(project_name="{project}")\n'
                       "Settings.callback_manager = CallbackManager([handler])"),
        "anthropic": ("python",
                      "import anthropic\n"
                      "from opik.integrations.anthropic import track_anthropic\n\n"
                      "client = track_anthropic(anthropic.Anthropic(),\n"
                      f'                        project_name="{project}")'),
        "openai": ("python",
                   "from openai import OpenAI\n"
                   "from opik.integrations.openai import track_openai\n\n"
                   f'client = track_openai(OpenAI(), project_name="{project}")'),
        "bedrock": ("python",
                    "import boto3\n"
                    "from opik.integrations.bedrock import track_bedrock\n\n"
                    'client = track_bedrock(boto3.client("bedrock-runtime"),\n'
                    f'                      project_name="{project}")'),
    }
    language, code = integrations[style]
    return [
        {"label": "Install", "language": "bash", "code": "pip install opik"},
        {"label": "Configure", "language": "bash", "code":
            f"export OPIK_URL_OVERRIDE={base}/opik/api/\n"
            f"export OPIK_API_KEY={key}\n"
            f"export OPIK_WORKSPACE=default"},
        {"label": "Instrument", "language": language, "code": code},
    ]

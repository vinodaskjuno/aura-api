"""FastAPI + WebSocket server: VS Code extension driver AND Aura REST API.

WebSocket protocol (JSON both ways) — VS Code extension:
  client -> {"type":"build","path": "<folder>"}         # map folder + build ontology
          -> {"type":"chat","sessionId":"s","text":"…"}  # ReAct advisor turn (streams)
          -> {"type":"abort"}                             # cancel current chat
  server -> progress/token/tool/done/error frames (see pipeline + orchestrator)

REST API — React frontend:
  /auth          JWT authentication
  /connectors    Data connector management
  /ws/advisor    WebSocket AI advisor (JWT-authenticated)
  /api/sdlc      SDLC phase tracker
  /upload        File upload

On startup prints `PORT=<n>` on stdout so the extension can discover the port.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
from contextlib import asynccontextmanager
import urllib.parse

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import config
from .advisor.orchestrator import Advisor
from .config_settings import get_settings

# ── Logging setup ────────────────────────────────────────────────────────────
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
))

for _name in ("src", "src.advisor.bedrock", "src.advisor.orchestrator",
              "src.advisor.memory"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.DEBUG)
    if not _lg.handlers:
        _lg.addHandler(_handler)
    _lg.propagate = False

log = logging.getLogger(__name__)


# ── Lifespan: initialize graph store, DynamoDB tables, S3 buckets ────────────
# Set SKIP_BOOTSTRAP=1 when DynamoDB tables and S3 buckets are provisioned out of
# band (Terraform, or `python -m src.scripts.bootstrap_aws`). ensure_tables() waits
# on the table_exists waiter for every table it creates — up to 30 s each, 25 tables
# — and it runs inside lifespan, so uvicorn accepts no connections until it returns.
# On a cold account that is minutes of connection-refused for a load balancer.
SKIP_BOOTSTRAP = os.getenv("SKIP_BOOTSTRAP", "").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    if SKIP_BOOTSTRAP:
        log.info("SKIP_BOOTSTRAP set — assuming DynamoDB tables and S3 buckets exist")
    else:
        # ── DynamoDB table bootstrap ──────────────────────────────────────────
        try:
            from .database.dynamo_client import ensure_tables
            ensure_tables()
            log.info("DynamoDB tables ready")
        except Exception as exc:
            log.warning("DynamoDB bootstrap skipped: %s", exc)

        # ── S3 bucket bootstrap (non-fatal — app works without S3 locally) ────
        try:
            from .storage.s3_client import ensure_buckets
            ensure_buckets()
            log.info("S3 buckets ready")
        except Exception as exc:
            log.info("S3 bootstrap skipped (non-fatal): %s", exc)

    # ── Seed default roles + admin user ──────────────────────────────────────
    try:
        from .services.auth_service import seed_default_data
        seed_default_data()
        log.info("Auth seed data ready")
    except Exception as exc:
        log.warning("Auth seed skipped: %s", exc)

    # ── Bootstrap agent registry ──────────────────────────────────────────────
    try:
        from .orchestrator.agent_registry import bootstrap
        bootstrap()
        log.info("Agent registry ready — 13 agents registered")
    except Exception as exc:
        log.warning("Agent bootstrap skipped: %s", exc)

    # ── Neo4j schema bootstrap ────────────────────────────────────────────────
    try:
        from .graph.neo4j_client import ensure_schema
        ensure_schema()
    except Exception as exc:
        log.info("Neo4j schema bootstrap skipped: %s", exc)

    # ── APScheduler ───────────────────────────────────────────────────────────
    _scheduler = None
    try:
        from .scheduler.jobs import setup_scheduler
        _scheduler = setup_scheduler()
    except Exception as exc:
        log.warning("Scheduler setup skipped: %s", exc)

    # ── Pre-warm coding assistant Bedrock client ───────────────────────────────
    try:
        _get_coding_client()
        log.info("Coding assistant Bedrock client ready")
    except Exception as exc:
        log.warning("Coding assistant client pre-warm skipped: %s", exc)

    yield

    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass


# ── App and middleware ────────────────────────────────────────────────────────
from src.routers import observability as observability_router

app = FastAPI(title="Aura API", lifespan=lifespan, redirect_slashes=False)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global exception handler: ensures CORS headers survive 500 errors ─────────
@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    origin = request.headers.get("origin", "")
    allowed = [o.strip() for o in _settings.cors_origins.split(",")]
    headers = {}
    if origin in allowed:
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
        }
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=headers,
    )


# ── Register REST API routers ─────────────────────────────────────────────────
from .routers import auth, connectors, upload
from .routers import advisor as advisor_ws_router
from .routers import sdlc, logs
from .orchestrator import router as orchestrator_router
from .routers import projects as projects_router
from .routers import qa as qa_router
from .routers import aiops as aiops_router
from .routers import sop as sop_router
from .routers import ontology_universe as ontology_universe_router
from .routers import ontology_loader as ontology_loader_router
from .routers import ontology_lens as ontology_lens_router
from .routers import scheduler_router
from .routers import chat_sessions as chat_sessions_router
from .routers import git_ops as git_ops_router
from .routers import metrics as metrics_router
from .routers import budget as budget_router
from .routers import credits as credits_router
from .routers import dashboard as dashboard_router

app.include_router(auth.router)
app.include_router(connectors.router)
app.include_router(upload.router)
app.include_router(advisor_ws_router.router)
app.include_router(sdlc.router)
app.include_router(logs.router)
app.include_router(orchestrator_router.router)
app.include_router(projects_router.router)
app.include_router(qa_router.router)
app.include_router(aiops_router.router)
app.include_router(observability_router.router)
app.include_router(sop_router.router)
app.include_router(ontology_universe_router.router)
app.include_router(ontology_loader_router.router)
app.include_router(ontology_lens_router.router)
app.include_router(scheduler_router.router)
app.include_router(chat_sessions_router.router)
app.include_router(git_ops_router.router)
app.include_router(metrics_router.router)
app.include_router(budget_router.router)
app.include_router(credits_router.router)
app.include_router(dashboard_router.router)
from .routers import commands as commands_router
app.include_router(commands_router.router)
from .routers import service_loader as service_loader_router
app.include_router(service_loader_router.router)

# ── AURA AI Gateway ───────────────────────────────────────────────────────────
from .routers import gateway as gateway_router
from .routers import gateway_keys as gateway_keys_router
from .routers import aiops_gateway as aiops_gateway_router
app.include_router(gateway_router.router, prefix="/gateway")
app.include_router(gateway_keys_router.router, prefix="/gateway")
app.include_router(aiops_gateway_router.router)
from .routers import graph_config as graph_config_router
app.include_router(graph_config_router.router)
from .routers import ai_observability as ai_observability_router
app.include_router(ai_observability_router.router)

# ── OTLP telemetry receiver (Claude Code usage capture) ───────────────────────
# Prefix is set on the router itself, so the exported paths are
# /otlp/v1/metrics and /otlp/v1/logs — what OTLP/HTTP derives from
# OTEL_EXPORTER_OTLP_ENDPOINT=<host>/otlp.
from .routers import otlp as otlp_router
app.include_router(otlp_router.router)

# ── VS Code plugin browser-based login flow ──────────────────────────────────
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AURA — Sign In</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@700;900&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #060c1a; color: #f1f5ff; min-height: 100vh;
    }}
    .layout {{ display: flex; min-height: 100vh; }}

    /* ── LEFT PANEL ─────────────────────────────────────── */
    .lp {{
      width: 38%; min-width: 340px;
      background: #0d1424; border-right: 1px solid #1a2d4a;
      display: flex; flex-direction: column; padding: 36px 40px;
      position: relative; overflow: hidden;
    }}
    .lp-glow {{
      position: absolute; top: 0; right: 0; width: 300px; height: 300px;
      background: radial-gradient(circle, rgba(96,165,250,0.07) 0%, transparent 70%);
      pointer-events: none;
    }}
    .logo-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 48px; }}
    .logo-mark {{ width: 42px; height: 42px; flex-shrink: 0; }}
    .logo-name {{
      font-family: 'Montserrat', sans-serif; font-weight: 900;
      font-size: 18px; letter-spacing: -0.03em; line-height: 1;
    }}
    .logo-name .pr {{ color: #60a5fa; }}
    .logo-name .tx {{ color: #f1f5ff; }}
    .logo-sub {{
      font-family: 'Montserrat', sans-serif; font-size: 9px; font-weight: 700;
      letter-spacing: 0.18em; color: #60a5fa; text-transform: uppercase; margin-top: 4px;
    }}
    .hero {{
      font-family: 'Montserrat', sans-serif; font-size: 30px;
      font-weight: 900; line-height: 1.15; color: #f1f5ff; margin-bottom: 6px;
    }}
    .hero.pr {{ color: #60a5fa; margin-bottom: 20px; }}
    .hero-desc {{
      font-size: 13px; color: #64748b; line-height: 1.7;
      margin-bottom: 36px; max-width: 300px;
    }}
    .features {{ display: flex; flex-direction: column; gap: 18px; }}
    .feat {{ display: flex; align-items: flex-start; gap: 12px; }}
    .feat-icon {{
      width: 32px; height: 32px; border-radius: 8px; flex-shrink: 0;
      background: rgba(255,255,255,0.05); border: 1px solid #1a2d4a;
      display: flex; align-items: center; justify-content: center;
    }}
    .feat-icon svg {{ width: 14px; height: 14px; }}
    .feat-title {{
      font-family: 'Montserrat', sans-serif; font-weight: 700;
      font-size: 13px; color: #f1f5ff; margin-bottom: 2px;
    }}
    .feat-desc {{ font-size: 11.5px; color: #64748b; line-height: 1.5; }}
    .version {{
      margin-top: auto; padding-top: 32px;
      font-size: 11px; color: #1e3a5f; font-family: 'Courier New', monospace;
    }}

    /* ── RIGHT PANEL ────────────────────────────────────── */
    .rp {{
      flex: 1; display: flex; align-items: center; justify-content: center;
      background: #060c1a; position: relative;
    }}
    .grid-overlay {{
      position: absolute; inset: 0; opacity: 0.025; pointer-events: none;
      background-image:
        linear-gradient(#60a5fa 1px, transparent 1px),
        linear-gradient(90deg, #60a5fa 1px, transparent 1px);
      background-size: 48px 48px;
    }}
    .form-wrap {{
      width: 100%; max-width: 380px; padding: 24px;
      position: relative; z-index: 1;
    }}
    .form-hdr {{ margin-bottom: 28px; }}
    .form-h2 {{
      font-family: 'Montserrat', sans-serif; font-size: 26px;
      font-weight: 800; color: #f1f5ff; margin-bottom: 6px;
    }}
    .form-sub {{ font-size: 13px; color: #64748b; }}
    .plugin-badge {{
      margin-top: 10px; display: flex; align-items: center; gap: 7px;
      background: rgba(96,165,250,0.08); border: 1px solid rgba(96,165,250,0.25);
      border-radius: 8px; padding: 7px 12px; font-size: 12px; color: #60a5fa;
    }}
    .plugin-badge svg {{ width: 13px; height: 13px; flex-shrink: 0; }}
    .field {{ margin-bottom: 16px; }}
    .field.last {{ margin-bottom: 24px; }}
    .field label {{
      display: block; font-size: 12px; font-weight: 600;
      color: #94a3b8; margin-bottom: 6px;
    }}
    .field input {{
      width: 100%; background: #0d1424; border: 1px solid #1a2d4a;
      border-radius: 8px; color: #f1f5ff;
      font-family: 'Inter', sans-serif; font-size: 14px;
      padding: 10px 14px; outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }}
    .field input:focus {{
      border-color: #60a5fa;
      box-shadow: 0 0 0 3px rgba(96,165,250,0.12);
    }}
    .field input::placeholder {{ color: #1e3a5f; }}
    .err-box {{
      background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3);
      border-radius: 8px; padding: 8px 12px; font-size: 12px;
      color: #ef4444; margin-bottom: 16px; display: none;
    }}
    .err-box.show {{ display: block; }}
    .submit-btn {{
      width: 100%; background: #60a5fa; border: none; border-radius: 8px;
      color: #060c1a; cursor: pointer;
      font-family: 'Montserrat', sans-serif; font-size: 14px; font-weight: 700;
      padding: 12px 20px; transition: background 0.15s, transform 0.1s;
    }}
    .submit-btn:hover {{ background: #93c5fd; transform: translateY(-1px); }}
    .submit-btn:active {{ transform: translateY(0); }}
    @media (max-width: 680px) {{ .lp {{ display: none; }} }}
  </style>
</head>
<body>
<div class="layout">

  <!-- LEFT -->
  <div class="lp">
    <div class="lp-glow"></div>
    <div class="logo-row">
      <!-- Logo mark: interconnected nodes -->
      <svg class="logo-mark" viewBox="0 0 42 42" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="21" cy="21" r="20" stroke="#60a5fa" stroke-width="1.5" opacity="0.25"/>
        <circle cx="21" cy="21" r="4" fill="#60a5fa"/>
        <circle cx="21" cy="6"  r="2.5" fill="#60a5fa" opacity="0.85"/>
        <circle cx="21" cy="36" r="2.5" fill="#60a5fa" opacity="0.85"/>
        <circle cx="6"  cy="21" r="2.5" fill="#60a5fa" opacity="0.85"/>
        <circle cx="36" cy="21" r="2.5" fill="#60a5fa" opacity="0.85"/>
        <circle cx="9"  cy="9"  r="2"   fill="#60a5fa" opacity="0.5"/>
        <circle cx="33" cy="9"  r="2"   fill="#60a5fa" opacity="0.5"/>
        <circle cx="9"  cy="33" r="2"   fill="#60a5fa" opacity="0.5"/>
        <circle cx="33" cy="33" r="2"   fill="#60a5fa" opacity="0.5"/>
        <line x1="21" y1="21" x2="21" y2="6"  stroke="#60a5fa" stroke-width="1" opacity="0.4"/>
        <line x1="21" y1="21" x2="21" y2="36" stroke="#60a5fa" stroke-width="1" opacity="0.4"/>
        <line x1="21" y1="21" x2="6"  y2="21" stroke="#60a5fa" stroke-width="1" opacity="0.4"/>
        <line x1="21" y1="21" x2="36" y2="21" stroke="#60a5fa" stroke-width="1" opacity="0.4"/>
        <line x1="21" y1="21" x2="9"  y2="9"  stroke="#60a5fa" stroke-width="1" opacity="0.22"/>
        <line x1="21" y1="21" x2="33" y2="9"  stroke="#60a5fa" stroke-width="1" opacity="0.22"/>
        <line x1="21" y1="21" x2="9"  y2="33" stroke="#60a5fa" stroke-width="1" opacity="0.22"/>
        <line x1="21" y1="21" x2="33" y2="33" stroke="#60a5fa" stroke-width="1" opacity="0.22"/>
      </svg>
      <div>
        <div class="logo-name">
          <span class="pr">A</span><span class="tx">ura</span>
        </div>
        <div class="logo-sub">AI Dev Agent Platform</div>
      </div>
    </div>

    <h1 class="hero">Multi-Agent<br>Orchestration.</h1>
    <h1 class="hero pr">Grounded in Your Schema.</h1>
    <p class="hero-desc">Aura maps multi-agent operations onto an explicit semantic schema, so every action is grounded in structured data triples rather than guesswork.</p>

    <div class="features">
      <div class="feat">
        <div class="feat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2" stroke-linecap="round">
            <circle cx="12" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><circle cx="5" cy="19" r="2"/>
            <line x1="12" y1="7" x2="19" y2="17"/><line x1="12" y1="7" x2="5" y2="17"/>
          </svg>
        </div>
        <div>
          <div class="feat-title">Development Cycle &amp; Schema Mapping</div>
          <div class="feat-desc">IDE plugins and web dashboard over one semantic schema</div>
        </div>
      </div>
      <div class="feat">
        <div class="feat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
          </svg>
        </div>
        <div>
          <div class="feat-title">AI-Driven Test Orchestration</div>
          <div class="feat-desc">Generates Playwright UI and API suites, runs them continuously</div>
        </div>
      </div>
      <div class="feat">
        <div class="feat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2" stroke-linecap="round">
            <line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/>
            <circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>
          </svg>
        </div>
        <div>
          <div class="feat-title">Reverse Engineering &amp; RCA</div>
          <div class="feat-desc">Maps legacy code to the graph and pinpoints root cause</div>
        </div>
      </div>
      <div class="feat">
        <div class="feat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2" stroke-linecap="round">
            <rect x="3" y="11" width="18" height="10" rx="2"/>
            <path d="M12 7v4"/><circle cx="12" cy="5" r="2"/>
            <line x1="8" y1="16" x2="8" y2="16" stroke-width="2.5"/>
            <line x1="16" y1="16" x2="16" y2="16" stroke-width="2.5"/>
          </svg>
        </div>
        <div>
          <div class="feat-title">Self-Healing Applications</div>
          <div class="feat-desc">Restores failing services from log and network signals</div>
        </div>
      </div>
    </div>

    <div class="version">v1.0.0 &middot; Powered by Neo4j &amp; AWS Bedrock</div>
  </div>

  <!-- RIGHT -->
  <div class="rp">
    <div class="grid-overlay"></div>
    <div class="form-wrap">
      <div class="form-hdr">
        <h2 class="form-h2">Welcome back</h2>
        <p class="form-sub">Sign in to connect the AURA VS Code extension</p>
        <div class="plugin-badge">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <rect x="3" y="11" width="18" height="10" rx="2"/>
            <path d="M12 7v4"/><circle cx="12" cy="5" r="2"/>
          </svg>
          After signing in you will be redirected back to VS Code
        </div>
      </div>

      <div class="err-box {err_class}" id="err">{error}</div>

      <form method="post" action="/login{qs}">
        <div class="field">
          <label for="u">Username</label>
          <input id="u" name="username" type="text" placeholder="admin"
                 autocomplete="username" required autofocus>
        </div>
        <div class="field last">
          <label for="p">Password</label>
          <input id="p" name="password" type="password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;"
                 autocomplete="current-password" required>
        </div>
        <button class="submit-btn" type="submit">Sign In</button>
      </form>
    </div>
  </div>

</div>
</body>
</html>"""


def _normalise_callback_uri(raw: str) -> str:
    """Recover a usable vscode:// callback from a double-encoded value.

    `openExternal()` takes a Uri, so the extension's login URL is round-tripped
    through VS Code's `Uri.parse()`/`toString()`. That pair decodes every
    component and re-encodes with a different table, so a callback carrying
    `?windowId=2` can reach us as `...callback%3FwindowId%253D2`: after our own
    single decode the separator is still `%3F` and the window id still `%253D`.

    Left alone, `urlsplit()` sees no query, we append our params with `?`, and
    the extension receives `windowId` buried in the *path* — invisible to VS
    Code's URL router, which then hands the token to whichever window was last
    active rather than the one that started the sign-in.

    Unquoting until it stabilises restores a real query string. Only the
    extension-supplied callback goes through here, never the token, and a
    correctly-encoded callback contains no `%` so this is a no-op for it.
    """
    if not raw:
        return raw
    for _ in range(3):
        if urllib.parse.urlsplit(raw).query:
            break
        unquoted = urllib.parse.unquote(raw)
        if unquoted == raw:
            break
        raw = unquoted
    return raw


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def plugin_login_page(request: Request):
    qs = "?" + str(request.url.query) if request.url.query else ""
    return HTMLResponse(_LOGIN_HTML.format(qs=qs, error="", err_class="err"))


@app.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def plugin_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    from .services.auth_service import authenticate, create_token
    from .config_settings import get_settings as _gs

    callback_uri = _normalise_callback_uri(request.query_params.get("callbackUri", ""))
    qs = "?" + str(request.url.query) if request.url.query else ""

    user = authenticate(username, password)
    if not user:
        return HTMLResponse(
            _LOGIN_HTML.format(qs=qs, error="Invalid username or password.", err_class="err show"),
            status_code=401,
        )

    token = create_token(user)
    s = _gs()
    expires_in = s.jwt_expire_minutes * 60

    if callback_uri:
        params = urllib.parse.urlencode({
            "access_token": token,
            "expires_in": expires_in,
            "username": user["username"],
            "userId": user["userId"],
            "role": user["role"],
        })
        # callbackUri comes from vscode.env.asExternalUri(), which already
        # carries a query string (windowId=N on desktop). A second "?" would
        # fold our params into the windowId value, so the extension would see
        # no access_token at all — append with "&" in that case.
        sep = "&" if urllib.parse.urlsplit(callback_uri).query else "?"
        redirect_target = f"{callback_uri}{sep}{params}"
        # Use JS redirect for vscode:// URIs since browsers block non-http redirects
        return HTMLResponse(
            f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>AURA — Signed In</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@700;900&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',sans-serif;background:#060c1a;color:#f1f5ff;
  display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}}
.card{{background:#0d1424;border:1px solid #1a2d4a;border-radius:16px;
  padding:40px 48px;max-width:400px;width:100%;box-shadow:0 24px 64px rgba(0,0,0,0.5)}}
.check{{width:56px;height:56px;border-radius:50%;background:rgba(96,165,250,0.12);
  border:1px solid rgba(96,165,250,0.3);display:flex;align-items:center;
  justify-content:center;margin:0 auto 20px}}
.check svg{{width:28px;height:28px}}
h2{{font-family:'Montserrat',sans-serif;font-size:20px;font-weight:800;
  color:#f1f5ff;margin-bottom:6px}}
p{{font-size:13px;color:#64748b;line-height:1.6}}
.user{{color:#60a5fa;font-weight:600}}
</style>
</head><body>
<div class="card">
  <div class="check">
    <svg viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  </div>
  <h2>Signed in as <span class="user">@{user['username']}</span></h2>
  <p style="margin-top:8px">Redirecting you back to VS Code&hellip;<br>You can close this tab if it doesn&rsquo;t happen automatically.</p>
</div>
<script>window.location.href = {repr(redirect_target)};</script>
</body></html>"""
        )

    return HTMLResponse("<p>Signed in. You may close this tab.</p>")


# ── Mock MCP HTTP server (SSE transport) ──────────────────────────────────────
try:
    from .connectors.mock_mcp.mcp_server import mock_mcp_asgi_app
    app.mount("/mock-mcp", mock_mcp_asgi_app())
    log.info("Mock MCP server mounted at /mock-mcp")
except Exception as _exc:
    log.warning("Mock MCP server not mounted: %s", _exc)

# ── VS Code extension: advisor sessions ──────────────────────────────────────
_advisors: dict[str, Advisor] = {}


def _get_advisor(session_id: str) -> Advisor:
    if session_id not in _advisors:
        _advisors[session_id] = Advisor(session_id)
    return _advisors[session_id]


# ── VS Code extension: general coding assistant ───────────────────────────────
_CODING_SYSTEM = """You are AURA, an AI coding assistant.

You have full workspace access via tools. When asked to create or write a file, call \
`write_file` immediately with the complete content — never tell the user to create it manually. \
For targeted edits use `edit_file`. Use `read_file` and `list_directory` to explore first.
You can also run shell commands, tests, linters, search the web, and analyse code.
Always use tools to ground your answers — never invent file contents or test results.
Write complete, working implementations with no placeholders.
Be concise, practical, and direct. Use markdown. Cite file paths and line numbers."""

_coding_client = None


def _get_coding_client():
    global _coding_client
    if _coding_client is None:
        from .advisor.bedrock import BedrockClient
        _coding_client = BedrockClient()
    return _coding_client


def _coding_chat(text: str, history: list, emit, abort: threading.Event,
                 workspace_root: str = "") -> None:
    from .tools.registry import TOOL_SPECS, execute_tool
    client = _get_coding_client()
    messages = []
    for h in (history or []):
        role = h.get("role", "user")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": [{"text": content}]})
    messages.append({"role": "user", "content": [{"text": text}]})

    system = _CODING_SYSTEM
    if workspace_root:
        system += f"\n\n**Workspace root:** `{workspace_root}`"

    full = ""
    for _ in range(8):
        if abort and abort.is_set():
            emit({"type": "aborted"})
            return

        assistant_text = ""
        tool_uses = []
        stop_reason = "end_turn"

        for ev in client.converse_stream(messages, system, TOOL_SPECS, abort):
            et = ev["type"]
            if et == "token":
                assistant_text += ev["text"]
                emit({"type": "token", "text": ev["text"]})
            elif et == "tool_use":
                tool_uses.append(ev)
            elif et == "usage":
                emit({"type": "usage", "input": ev.get("input"), "output": ev.get("output")})
            elif et == "stop":
                stop_reason = ev["reason"]

        assistant_content: list = []
        if assistant_text:
            assistant_content.append({"text": assistant_text})
        for tu in tool_uses:
            assistant_content.append({"toolUse": {
                "toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}})
        messages.append({"role": "assistant", "content": assistant_content or [{"text": ""}]})

        if not tool_uses or stop_reason in ("end_turn", "aborted"):
            full = assistant_text
            break

        tool_results = []
        for tu in tool_uses:
            emit({"type": "tool_start", "name": tu["name"], "input": tu["input"]})
            result = execute_tool(tu["name"], tu["input"], workspace_root)
            emit({"type": "tool_end", "name": tu["name"]})
            tool_results.append({"toolResult": {
                "toolUseId": tu["id"], "content": [{"text": result}]}})
        messages.append({"role": "user", "content": tool_results})

    emit({"type": "done", "text": full})


# ── VS Code extension: existing HTTP endpoints (PRESERVED) ───────────────────
@app.get("/healthz")
def healthz():
    """Liveness probe for load balancers. Deliberately does no I/O.

    Prefer this over /health for an ALB target group: /health calls
    bedrock_configured(), which constructs a fresh boto3 Session and resolves
    credentials on every request — under ECS that hits the task-role credential
    endpoint on every health check.
    """
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok", "bedrockConfigured": config.bedrock_configured()}



async def _pump(ws: WebSocket, work, abort: threading.Event):
    """Run blocking I/O-bound `work(emit)` (chat) in a thread and relay events."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = {"__done__": True}

    def emit(ev: dict):
        loop.call_soon_threadsafe(queue.put_nowait, ev)

    def runner():
        try:
            work(emit)
        except Exception as e:  # pragma: no cover
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(e)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    threading.Thread(target=runner, daemon=True).start()
    while True:
        ev = await queue.get()
        if ev is _SENTINEL:
            break
        await ws.send_json(ev)


# ── HTTP-polled build (reliable on Windows) ──────────────────────────────────
_build_state: dict = {"prog": None, "proc": None, "path": None, "errlog": None}


def _start_build(path: str) -> dict:
    import subprocess
    import sys

    config.ensure_dirs()
    prog = config.OUT_DIR / "progress.ndjson"
    prog.write_text("", encoding="utf-8")
    errlog_path = config.OUT_DIR / "child_err.log"
    errlog = open(errlog_path, "w", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.build_stream", path, str(prog)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errlog,
        cwd=str(config.BACKEND_DIR.parent), close_fds=True, creationflags=flags,
    )
    _build_state.update(prog=prog, proc=proc, path=path, errlog=errlog)
    return {"ok": True}


@app.post("/build")
async def http_build(payload: dict):
    path = (payload or {}).get("path") or str(config.BACKEND_DIR.parent)
    return _start_build(path)


@app.get("/progress")
def http_progress(offset: int = 0):
    prog = _build_state.get("prog")
    proc = _build_state.get("proc")
    if not prog or not prog.exists():
        return JSONResponse({"events": [], "offset": 0, "running": False})
    with open(prog, "rb") as f:
        f.seek(offset)
        raw = f.read()
    cut = raw.rfind(b"\n")
    complete = raw[: cut + 1] if cut != -1 else b""
    new_offset = offset + len(complete)
    events = []
    for line in complete.decode("utf-8", "replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(ev)
        if ev.get("type") == "pipeline_done" and ev.get("result"):
            r = ev["result"]
            stats = {"nodes": r["graph"]["nodes"], "edges": r["graph"]["edges"],
                     "recommendations": r["rules"]["recommendations"]}
            for adv in _advisors.values():
                adv.memory.note_build(_build_state.get("path", ""), stats)
    running = proc is not None and proc.poll() is None
    return JSONResponse({"events": events, "offset": new_offset, "running": running})


# ── VS Code extension: WebSocket endpoint (PRESERVED) ────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    abort = threading.Event()
    await ws.send_json({"type": "connected",
                        "bedrockConfigured": config.bedrock_configured()})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "invalid JSON"})
                continue

            mtype = msg.get("type")
            log.info("WS message received | type=%s", mtype)
            if mtype == "ping":
                await ws.send_json({"type": "pong"})
                continue
            if mtype == "chat":
                session_id = msg.get("sessionId", "default")
                chat_mode = msg.get("chatMode", "coding")
                text = msg.get("text", "")
                history = msg.get("history", [])
                context = msg.get("context", {})
                workspace_root = context.get("workspaceRoot", "") if isinstance(context, dict) else ""
                log.info("Chat request | session=%s mode=%s workspace=%r text=%r",
                         session_id, chat_mode, workspace_root or "(none)", text[:120])
                if not config.bedrock_configured():
                    await ws.send_json({"type": "error",
                        "message": "Bedrock credentials not configured. Add your AWS profile to "
                                   "~/.aws/credentials and set AWS_PROFILE in src/config/dev.env."})
                    continue
                abort.clear()
                if chat_mode == "advisory":
                    advisor = _get_advisor(session_id)

                    def chat_work(emit, _t=text, _a=advisor, _wr=workspace_root):
                        _a.run(_t, emit, abort=abort, workspace_root=_wr)
                else:
                    await ws.send_json({"type": "progress", "text": "Thinking…"})

                    def chat_work(emit, _t=text, _h=history, _ab=abort, _wr=workspace_root):
                        _coding_chat(_t, _h, emit, _ab, _wr)

                await _pump(ws, chat_work, abort)

            elif mtype == "abort":
                abort.set()
                await ws.send_json({"type": "aborted"})

            else:
                await ws.send_json({"type": "error", "message": f"unknown message type {mtype}"})
    except WebSocketDisconnect:
        return


# ── Port selection and server entry point ────────────────────────────────────
def _pick_port() -> int:
    if config.CODEONTOLOGY_PORT:
        return config.CODEONTOLOGY_PORT
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    import uvicorn

    config.ensure_dirs()
    port = _pick_port()
    # The extension parses this line from stdout to discover the port.
    print(f"PORT={port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()

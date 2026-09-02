# ─────────────────────────────────────────────────────────────────────────────
# Aura backend — FastAPI on ECS Fargate
#
# Build from the REPO ROOT:  podman build --platform linux/amd64 \
#                              -f docker/backend.Dockerfile -t aura-backend .
#
# Debian slim, NOT Alpine: bcrypt==4.2.1 is a Rust wheel with no musl build, and
# src/services/auth_service.py:65-72 silently downgrades password hashing to
# sha256 when bcrypt is missing — a security regression you would never notice.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is load-bearing, not a convenience: ~20 call sites shell out to it
# (src/routers/git_ops.py, src/tools/shell_tools.py:121,137,159,
# src/connectors/repo_connector.py:133), and git_ops.py:59 raises
# "Git not found on server" without it.
# ca-certificates is needed for HTTPS clones and every outbound API call.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# ── Dependencies ────────────────────────────────────────────────────────────
# Copied alone so the layer caches independently of source changes.
COPY src/requirements.txt /tmp/requirements.txt

# browser-use and langchain-aws pull Playwright + the langchain tree (~300 MB
# with Chromium). With DEPLOYMENT_ENV=ecs the browser path delegates to Lambda
# (src/agents/container_test_runner.py:138-143) and the import is lazy with a
# clean ImportError fallback (browser_use_runner_agent.py:42-49), so they are
# stripped here. Remove the grep if you ever run browser tests in this image.
# ONE pip invocation, deliberately. Splitting this across two `pip install`
# commands lets the second silently upgrade a dependency the first pinned:
# `mcp` 2.x requires starlette>=1.0, fastapi 0.115.6 requires starlette<0.42,
# and a second install happily replaced starlette 0.41 with 1.6 — producing
# "TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'"
# at import time. Resolved together, pip either finds a consistent set or fails
# loudly at build time, which is what you want.
#
# `mcp` and `starlette` now live in src/requirements.txt (one source of truth) and
# arrive via req.slim.txt below, still inside this single invocation. The cap alone
# turned out NOT to be enough: mcp 1.29 also accepts starlette 1.x, so requirements.txt
# pins starlette==0.41.3 explicitly. Read the note there before changing either.
RUN grep -viE '^(browser-use|langchain-aws)' /tmp/requirements.txt > /tmp/req.slim.txt \
 && pip install -r /tmp/req.slim.txt \
      "PyYAML>=6.0" \
      "pypdf>=5.0.0" \
      "kubernetes>=31.0.0" \
      "pytest-json-report>=1.5.0" \
 && pip check

# ── Application ─────────────────────────────────────────────────────────────
WORKDIR /app

COPY src/ /app/src/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# These paths are written at RUNTIME and must exist and be writable:
#   /app/data/repos  — repo_connector.py:92 mkdirs it at MODULE IMPORT time,
#                      cwd-relative, so a read-only cwd fails the import outright
#   /app/src/out     — config.py:81, progress.ndjson + child_err.log
#   /app/src/memory  — config.py:82, Fernet-encrypted advisor sessions
#   /workspace       — git_ops.py:19 AURA_WORKSPACE default, clone target
RUN mkdir -p /app/data/repos /app/src/out /app/src/memory /workspace

# Run unprivileged. Note src/tools/shell_tools.py:34 exposes `run_bash`
# (subprocess with shell=True) to the LLM, so a non-root user is a meaningful
# blast-radius reduction, not a formality.
RUN useradd --create-home --shell /bin/bash --uid 10001 aura \
 && chown -R aura:aura /app /workspace
USER aura

ENV APP_ENV=prod \
    SKIP_BOOTSTRAP=1 \
    DEPLOYMENT_ENV=ecs \
    AURA_WORKSPACE=/workspace \
    PORT=8000

EXPOSE 8000

# /healthz does no I/O, unlike /health which resolves AWS credentials per call.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

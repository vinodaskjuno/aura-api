#!/bin/sh
set -eu

# ── Why this script exists ───────────────────────────────────────────────────
# src/config/dev.env sets AWS_PROFILE=default and IS tracked in git. APP_ENV
# defaults to "dev" (src/config.py:16), so load_dotenv() injects that value into
# the process environment. botocore honours AWS_PROFILE globally, and there is no
# ~/.aws/credentials inside a container — so every boto3 call would raise
# ProfileNotFound and the ECS task role would never be used.
#
# APP_ENV=prod (set in the task definition) avoids it, but unsetting here is the
# belt-and-braces version: it holds even if someone ships APP_ENV=dev by mistake.
unset AWS_PROFILE || true

# Explicit empty credentials would override the task role in
# src/database/dynamo_client.py:44-46 — drop them if they arrived empty.
[ -z "${AWS_ACCESS_KEY_ID:-}" ] && unset AWS_ACCESS_KEY_ID || true
[ -z "${AWS_SECRET_ACCESS_KEY:-}" ] && unset AWS_SECRET_ACCESS_KEY || true

PORT="${PORT:-8000}"

echo "[entrypoint] APP_ENV=${APP_ENV:-unset} SKIP_BOOTSTRAP=${SKIP_BOOTSTRAP:-unset} port=${PORT}"
echo "[entrypoint] AWS_PROFILE=${AWS_PROFILE:-<unset, correct>} region=${AWS_REGION:-unset}"

# Never `python -m src.main`: main() binds 127.0.0.1 and _pick_port() picks a
# random port for VS Code extension discovery. Both are fatal behind an ALB.
exec uvicorn src.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --timeout-keep-alive 75 \
  --log-level info

"""Central configuration. Environment-specific settings are loaded from
config/{APP_ENV}.env (e.g. config/dev.env). AWS credentials are resolved from
~/.aws/credentials via the standard boto3 credential chain — no secrets in code.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent

# Load environment-specific non-sensitive config.
# Set APP_ENV=dev|staging|prod (defaults to dev).
APP_ENV = os.getenv("APP_ENV", "dev")
_env_file = SRC_DIR / "config" / f"{APP_ENV}.env"
if _env_file.exists():
    load_dotenv(_env_file)

# config/*.env declares `AWS_PROFILE=` (empty) to mean "use the default chain".
# botocore does not read it that way: an empty-but-present AWS_PROFILE makes it
# look for a profile literally named "", and every client raises
# ProfileNotFound("The config profile () could not be found").
#
# In a container there is no ~/.aws/credentials at all, so this silently breaks
# DynamoDB, S3, and the auth seed while the app still reports a healthy startup.
# Deleting the variable outright is the only way to get the default credential
# chain — which is what an ECS task role needs.
if not os.environ.get("AWS_PROFILE", "").strip():
    os.environ.pop("AWS_PROFILE", None)

# --- Paths ---
OUT_DIR = SRC_DIR / "out"
MEMORY_DIR = SRC_DIR / "memory"

# Keep SRC_DIR accessible as BACKEND_DIR for internal callers that use the old name.
BACKEND_DIR = SRC_DIR

# --- AWS / Bedrock ---
# Credentials come from ~/.aws/credentials (standard boto3 chain).
# AWS_PROFILE selects the named profile; leave blank to use [default].
AWS_PROFILE = os.getenv("AWS_PROFILE") or None
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
BEDROCK_DISABLE_SSL_VERIFY = os.getenv("BEDROCK_DISABLE_SSL_VERIFY", "false").lower() == "true"

# --- Memory ---
MEMORY_ENCRYPTION_KEY = os.getenv("MEMORY_ENCRYPTION_KEY") or None
MEMORY_PASSPHRASE = os.getenv("MEMORY_PASSPHRASE") or None
MEMORY_MAX_ACTIVE_TURNS = int(os.getenv("MEMORY_MAX_ACTIVE_TURNS", "12"))
MEMORY_MAX_ACTIVE_TOKENS = int(os.getenv("MEMORY_MAX_ACTIVE_TOKENS", "8000"))

# --- Server ---
CODEONTOLOGY_PORT = int(os.getenv("CODEONTOLOGY_PORT", "0"))

_AWS_CREDS_FILE = Path.home() / ".aws" / "credentials"


def load_bedrock_credentials() -> dict:
    """Read credentials from ~/.aws/credentials for the configured AWS profile."""
    creds = {
        "access_key": None, "secret_key": None, "session_token": None,
        "region": AWS_REGION, "model_id": BEDROCK_MODEL_ID, "profile": AWS_PROFILE,
    }
    if _AWS_CREDS_FILE.exists():
        import configparser
        cp = configparser.ConfigParser()
        try:
            cp.read(_AWS_CREDS_FILE)
            profile = AWS_PROFILE or "default"
            if profile in cp:
                s = cp[profile]
                creds["access_key"] = s.get("aws_access_key_id") or None
                creds["secret_key"] = s.get("aws_secret_access_key") or None
                creds["session_token"] = s.get("aws_session_token") or None
        except Exception:
            pass
    return creds


def bedrock_configured() -> bool:
    """True when boto3 can resolve AWS credentials from ~/.aws/credentials or env."""
    try:
        import boto3
        session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
        return session.get_credentials() is not None
    except Exception:
        return False


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

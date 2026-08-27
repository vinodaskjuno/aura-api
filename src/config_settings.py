import logging
import re

from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

SRC_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # ── Auth ─────────────────────────────────────────────────────────────────
    jwt_secret: str = ""  # REQUIRED — set JWT_SECRET in .env
    jwt_expire_minutes: int = 480
    bcrypt_rounds: int = 12

    # ── AWS general ───────────────────────────────────────────────────────────
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    # ── DynamoDB ──────────────────────────────────────────────────────────────
    dynamodb_region: str = "us-east-1"
    dynamodb_table_prefix: str = "aura-"
    dynamodb_endpoint_url: str = ""                 # leave empty for AWS; set for local DynamoDB

    # ── S3 ────────────────────────────────────────────────────────────────────
    s3_region: str = "us-east-1"
    s3_bucket_prefix: str = "aura-"

    # ── LLM backend ──────────────────────────────────────────────────────────
    # "bedrock" → AWS Bedrock (needs aws_access_key_id / aws_secret_access_key)
    # "anthropic" → Anthropic API directly (needs anthropic_api_key)
    llm_backend: str = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model_id: str = "claude-3-5-sonnet-20241022"

    # ── Bedrock ───────────────────────────────────────────────────────────────
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    bedrock_region: str = "us-east-1"

    # ── Deployment environment ────────────────────────────────────────────────
    # "local" → browser-use runs in-process (no Docker, no Lambda)
    # "ecs"   → FastAPI calls Lambda; Lambda runs browser-use in isolation
    deployment_env: str = "local"                   # "local" | "ecs"

    # ── Test runner (Lambda, used when deployment_env=ecs) ────────────────────
    test_runner_lambda: str = "aura-test-runner"    # Lambda function name

    # ── Legacy ECS Fargate settings (kept for backward compat) ───────────────
    test_runner_backend: str = "local"
    ecs_cluster: str = "aura-test-cluster"
    ecs_task_definition: str = "aura-playwright-runner"
    ecs_subnets: str = ""
    ecs_security_groups: str = ""
    playwright_image: str = "mcr.microsoft.com/playwright:v1.45.0-jammy"

    # ── Neo4j (Enterprise Ontology Graph) ────────────────────────────────────
    neo4j_uri: str = "neo4j://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""  # set NEO4J_PASSWORD in .env
    neo4j_database: str = "neo4j"
    neo4j_enabled: bool = False                     # set True when Neo4j is running

    # ── AI Gateway ───────────────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    google_api_key: str = ""
    rate_limit_rpm: int = 60                        # requests per minute per user
    rate_limit_burst: int = 10                      # max burst tokens in sliding window
    gateway_audit_table: str = "gateway-audit-log"
    gateway_keys_table: str = "gateway-api-keys"

    # ── OTLP telemetry receiver (Claude Code usage capture) ──────────────────────
    otlp_enabled: bool = True
    otlp_max_body_bytes: int = 8 * 1024 * 1024   # reject absurd payloads
    usage_rollup_table: str = "usage-daily"

    # ── App ───────────────────────────────────────────────────────────────────
    cors_origins: str = "http://localhost:5173,http://localhost:5174"
    app_env: str = "development"                    # development | staging | production

    class Config:
        env_file = str(SRC_DIR / ".env")   # absolute path — works regardless of cwd
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


# Module-level singleton — agents can do `from src.config_settings import settings`
settings = get_settings()


# ─────────────────────────────────────────────────────────────────────────────
# Model pricing
#
# Base rates are USD per 1M tokens, first-party Anthropic API list pricing.
# Cache rates are fixed multiples of the input rate, so they cannot drift out of
# sync with it:
#     cache read        0.10x input   (reading an existing cache hit)
#     5-minute write    1.25x input   (default ephemeral cache TTL)
#     1-hour write      2.00x input   (extended ephemeral cache TTL)
#
# Verified against Claude Code's own reported cost, 2026-08-27:
#   haiku-4-5   896 in +      9 out                      -> $0.000941  (1/5)
#   opus-5        2 in +      4 out + 25395 cacheRead
#                                   +  7075 cacheWrite1h -> $0.0835575 (5/25)
# Both reconcile exactly at the rates below. Note the previous table had Haiku
# at 0.8/4.0 and Opus at 15/75 -- both wrong.
# ─────────────────────────────────────────────────────────────────────────────

CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00

# Canonical model ID -> {input, output} USD per 1M tokens.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # ── Anthropic, current ────────────────────────────────────────────────────
    "claude-fable-5":       {"input": 10.0, "output": 50.0},
    "claude-mythos-5":      {"input": 10.0, "output": 50.0},
    "claude-opus-5":        {"input":  5.0, "output": 25.0},
    "claude-opus-4-8":      {"input":  5.0, "output": 25.0},
    "claude-opus-4-7":      {"input":  5.0, "output": 25.0},
    "claude-opus-4-6":      {"input":  5.0, "output": 25.0},
    "claude-sonnet-5":      {"input":  2.0, "output": 10.0},
    "claude-sonnet-4-6":    {"input":  3.0, "output": 15.0},
    "claude-sonnet-4-5":    {"input":  3.0, "output": 15.0},
    "claude-haiku-4-5":     {"input":  1.0, "output":  5.0},
    # ── Anthropic, legacy ─────────────────────────────────────────────────────
    "claude-3-5-sonnet":    {"input":  3.0, "output": 15.0},
    "claude-opus-4-5":      {"input":  5.0, "output": 25.0},
    # ── Other providers (AURA agents via the gateway) ─────────────────────────
    "gpt-4o":               {"input":  2.5, "output": 10.0},
    "gpt-4o-mini":          {"input": 0.15, "output":  0.6},
    "gemini-2.5-pro":       {"input": 1.25, "output": 10.0},
    "amazon.nova-pro-v1:0": {"input":  0.8, "output":  3.2},
}

# Fast mode (Opus 5 / 4.8) is the same model at premium pricing.
FAST_MODE_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5":   {"input": 10.0, "output": 50.0},
    "claude-opus-4-8": {"input": 10.0, "output": 50.0},
}

_DEFAULT_PRICING = {"input": 3.0, "output": 15.0}

# Long-context marker Claude Code appends to 1M-context model IDs, e.g.
# "claude-opus-5[1m]".
#
# STRIPPED, not preserved. Claude Code's two telemetry signals disagree about
# it for the SAME request -- the api_request event reports "claude-opus-5" while
# claude_code.token.usage reports "claude-opus-5[1m]" -- so keeping it splits one
# model into two phantom dashboard rows. Long-context usage is tracked as the
# separate boolean returned by is_long_context() instead.
_LONG_CONTEXT_SUFFIX = "[1m]"

# Bedrock / Vertex ID prefixes to strip when canonicalizing.
_PROVIDER_PREFIXES = ("us.anthropic.", "eu.anthropic.", "apac.anthropic.", "anthropic.")

# Trailing date snapshot (-20251101) or Bedrock version (-v1:0 / @20251101).
_DATE_SUFFIX_RE = re.compile(r"-(?:20\d{6})(?:-v\d+:\d+)?$")
_VERSION_SUFFIX_RE = re.compile(r"-v\d+:\d+$")
_AT_VERSION_RE = re.compile(r"@\d{8}$")


def normalize_model_id(model_id: str) -> str:
    """Collapse provider prefixes and date snapshots into one canonical model ID.

    So "us.anthropic.claude-opus-5-20251101-v1:0", "claude-opus-5-20251101" and
    "claude-opus-5" all group as "claude-opus-5" in the usage dashboards,
    instead of appearing as three unrelated rows.

    The "[1m]" long-context marker is preserved -- it is a real distinction
    users care about -- and stripped only for the pricing lookup.
    """
    if not model_id:
        return "unknown"

    m = model_id.strip()
    if m.endswith(_LONG_CONTEXT_SUFFIX):
        m = m[: -len(_LONG_CONTEXT_SUFFIX)]

    for prefix in _PROVIDER_PREFIXES:
        if m.startswith(prefix):
            m = m[len(prefix):]
            break

    # Keep non-Anthropic IDs (nova, gpt, gemini) intact apart from prefixes.
    if m.startswith("claude"):
        m = _DATE_SUFFIX_RE.sub("", m)
        m = _VERSION_SUFFIX_RE.sub("", m)
        m = _AT_VERSION_RE.sub("", m)

    return m


def is_long_context(model_id: str) -> bool:
    """True when the model ID carries Claude Code's 1M-context marker."""
    return (model_id or "").strip().endswith(_LONG_CONTEXT_SUFFIX)


def get_model_pricing(model_id: str, fast: bool = False) -> dict[str, float]:
    """Full rate card for a model: input, output, and the three cache rates."""
    canonical = normalize_model_id(model_id)

    base = None
    if fast:
        base = FAST_MODE_PRICING.get(canonical)
    if base is None:
        base = MODEL_PRICING.get(canonical)
    if base is None:
        log.warning(
            "Unknown model %r (canonical %r) -- falling back to Sonnet pricing; "
            "cost figures for this model are estimates. Add it to MODEL_PRICING.",
            model_id, canonical,
        )
        base = _DEFAULT_PRICING

    return {
        "input": base["input"],
        "output": base["output"],
        "cache_read": base["input"] * CACHE_READ_MULTIPLIER,
        "cache_write_5m": base["input"] * CACHE_WRITE_5M_MULTIPLIER,
        "cache_write_1h": base["input"] * CACHE_WRITE_1H_MULTIPLIER,
    }


def calculate_cost_v2(
    model_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
    fast: bool = False,
) -> float:
    """Cache-aware USD cost for one model invocation.

    cache_creation_tokens is billed at the 5-minute rate; pass the 1-hour
    portion separately via cache_creation_1h_tokens when the caller knows it
    (Claude Code reports it as usage.cache_creation.ephemeral_1h_input_tokens).

    This is an ESTIMATE. When a provider reports its own cost -- Claude Code's
    claude_code.api_request events carry cost_usd -- prefer that; the two are
    reconciled in the usage dashboards and a gap indicates a stale rate here.
    """
    p = get_model_pricing(model_id, fast=fast)
    micros = (
        (input_tokens or 0) * p["input"]
        + (output_tokens or 0) * p["output"]
        + (cache_read_tokens or 0) * p["cache_read"]
        + (cache_creation_tokens or 0) * p["cache_write_5m"]
        + (cache_creation_1h_tokens or 0) * p["cache_write_1h"]
    )
    return round(micros / 1_000_000, 6)


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Backwards-compatible two-token cost. Prefer calculate_cost_v2."""
    return calculate_cost_v2(model_id, input_tokens, output_tokens)


# Tier model assignments (1=Premium, 2=Balanced, 3=Economy)
# Compared against normalize_model_id(model), so entries are canonical IDs.
TIER_MODELS: dict[int, list[str]] = {
    1: [
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
    ],
    2: [
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-3-5-sonnet",
        "gpt-4o",
        "gemini-2.5-pro",
    ],
    3: [
        "claude-haiku-4-5",
        "amazon.nova-pro-v1:0",
        "gpt-4o-mini",
    ],
}

# Default model to fall back to when a tier is exhausted
TIER_DEFAULT_MODELS: dict[int, str] = {
    1: "claude-sonnet-5",   # fall back to tier 2 default
    2: "claude-haiku-4-5",  # fall back to tier 3 default
    3: "claude-haiku-4-5",  # already economy
}


def get_model_tier(model_id: str) -> int:
    """Return the tier number (1/2/3) for a given model ID. Defaults to 2."""
    canonical = normalize_model_id(model_id)
    for tier, models in TIER_MODELS.items():
        if canonical in models:
            return tier
    return 2

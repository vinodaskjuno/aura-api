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
    anthropic_model_id: str = "claude-opus-5"

    # ── Bedrock ───────────────────────────────────────────────────────────────
    # Must be a cross-region INFERENCE PROFILE id (region prefix: us. / eu. / apac.),
    # not a bare foundation-model id. Current Anthropic models are not invocable
    # on-demand by their bare id — Bedrock rejects that with a ValidationException.
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_region: str = "us-east-1"

    # ── Deployment environment ────────────────────────────────────────────────
    deployment_env: str = "local"                   # "local" | "ecs"

    # Where project working copies live. Declared here so the value in src/.env
    # actually takes effect: src/services/advisor/tools.py reads AURA_WORKSPACE from
    # os.environ, which pydantic-settings never populates, so a configured
    # ./data/workspace was silently ignored and every lookup resolved /workspace.
    aura_workspace: str = "/workspace"

    # ── QualityMind test runner ──────────────────────────────────────────────
    # Runs execute where podman and a browser exist — a developer machine or CI — and
    # never in the deployed task. GET /api/qa/capabilities reports what THIS process
    # can do, so the UI disables the run button with a reason instead of offering one
    # that fails.
    #
    # Removed here, not renamed: test_runner_lambda pointed at `aura-test-runner`,
    # which was never deployed, and test_runner_backend/ecs_* configured the per-run
    # Fargate provisioning that src/qatest replaces. Emulator images and ports live in
    # src/qatest/emulators.py, deliberately not in settings — get_settings() is
    # lru_cached, and which emulators a run needs is derived from the project's
    # dependency nodes rather than configured.
    qatest_emulator_timeout_s: int = 60
    # Self-hosted QA runner. Empty disables scoped-credential minting, which is the
    # correct default: a developer running the backend locally already has their own
    # AWS credentials, and no other environment should hand any out.
    qa_runner_role_arn: str = ""
    # How long a claimed run may go without a heartbeat before the reaper declares it
    # abandoned. Generous: one navigation can sit quiet for 20s and an emulator image
    # pull is minutes.
    qa_run_stale_after_s: int = 900

    # ── Neo4j (Enterprise Ontology Graph) ────────────────────────────────────
    neo4j_uri: str = "neo4j://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""  # set NEO4J_PASSWORD in .env
    neo4j_database: str = "neo4j"
    neo4j_enabled: bool = False                     # set True when Neo4j is running

    # ── Memgraph ──────────────────────────────────────────────────────────────
    # Bolt-compatible, so the same neo4j driver connects. These are connection
    # facts and belong here; which backend is ACTIVE does not — get_settings() is
    # lru_cached, so a value read from here cannot change without a restart, and
    # the read-source toggle has to be switchable from the UI.
    memgraph_enabled: bool = False
    memgraph_uri: str = "bolt://127.0.0.1:7688"
    memgraph_user: str = ""
    memgraph_password: str = ""

    # Arms the Settings → Danger Zone graph wipe. Defaults to FALSE, and is set only
    # in the dev task definition.
    #
    # Deliberately its own flag rather than a check on app_env: APP_ENV is hardcoded
    # to "prod" in infra/ecs.tf for every environment, so the backend cannot tell dev
    # from prod. An explicit opt-in means a new environment is safe by OMISSION —
    # nobody has to remember to add a block — and fixing APP_ENV later cannot
    # silently arm this.
    allow_graph_wipe: bool = False

    # ── Directory (LDAP / Active Directory) ──────────────────────────────────
    # Connection facts only. WHETHER directory auth is on, and which groups map to
    # which permissions, is runtime state in DynamoDB (services/auth_config.py) —
    # adding an AD group must not need a redeploy.
    ldap_uri: str = "ldaps://localhost:636"
    ldap_base_dn: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    # {username} is substituted with the ESCAPED login name. sAMAccountName suits AD;
    # OpenLDAP typically wants (uid={username}).
    ldap_user_filter: str = "(sAMAccountName={username})"
    # A simple bind over plaintext sends the password in clear text. Lab use only.
    ldap_allow_insecure: bool = False

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

    # ── Opik (self-hosted LLM observability engine) ───────────────────────────
    # Opik replaces DynamoDB as the trace/span engine behind src/aiobs/. It is
    # reached over its REST API; nothing here talks to ClickHouse directly.
    #
    # Two independent switches, deliberately:
    #   opik_enabled  -> may Aura WRITE spans for its own agents?
    #   aiobs_store   -> which engine does the READ path use?
    # Keeping them apart is what makes the migration reversible: dual-write with
    # `opik_enabled=true, aiobs_store=dynamodb`, verify, then flip the reader.
    opik_enabled: bool = False
    opik_url: str = "http://opik-frontend:5173/api/"
    opik_workspace: str = "default"          # OSS Opik has exactly one workspace
    opik_api_key: str = ""                   # unused while AUTH_ENABLED=false
    opik_timeout_seconds: float = 10.0

    # Where a BROWSER reaches the Opik UI. Served on its own port rather than a
    # sub-path of Aura: Comet's published frontend image is built with Vite base=/,
    # so its assets are absolute and a sub-path deployment makes the browser fetch
    # them from Aura's origin — which returns Aura's own SPA.
    #
    # Sent to the UI from /capabilities rather than baked into the bundle. The
    # frontend deliberately never reads import.meta.env for addresses (see
    # aura-ui/frontend/src/api/wsUrl.ts): it is inlined at build time and would pin
    # one image to one environment. Empty means "same host, opik_ui_port".
    opik_ui_url: str = ""
    opik_ui_port: int = 8081

    # The externally reachable base URL, used to render onboarding snippets. NOT
    # derived from the request: Aura sits behind an ALB and nginx, so request.base_url
    # is usually the internal address, and a confidently wrong endpoint in a
    # copy-paste snippet is worse than an obvious placeholder.
    public_base_url: str = ""

    # "dynamodb" | "opik". The one line the TraceStore protocol exists to make
    # swappable (src/aiobs/service.py::get_store).
    aiobs_store: str = "dynamodb"
    # Forward inbound OTLP to Opik as well as storing locally. Independent of
    # aiobs_store so a deployment can backfill Opik before trusting it to read.
    aiobs_forward_otlp: bool = False

    # Demo agents (aura-infra/demo-agents): four standalone agents producing continuous
    # traffic so the observability screens are never empty. Reached over ECS Service
    # Connect, which is why this is a plain internal address and not an ALB path —
    # exposing a demo trigger on the public listener would mean another target group,
    # another listener rule and another auth surface, for a button.
    #
    # Empty disables the forwarder, which is the correct default: no environment should
    # have a demo trigger it did not ask for.
    demo_agents_url: str = ""

    # ── App ───────────────────────────────────────────────────────────────────
    cors_origins: str = "http://localhost:5173,http://localhost:5174"
    app_env: str = "development"                    # development | staging | production

    # ── Observability / SRE agents ────────────────────────────────────────────
    # Org-wide fallbacks. Per-user/per-project credentials live in the
    # `user-connectors` DynamoDB table and take precedence (see
    # src/observability/credentials.py::resolve_config).
    grafana_url: str = ""
    loki_url: str = ""
    mimir_url: str = ""
    tempo_url: str = ""
    grafana_api_token: str = ""
    grafana_org_id: str = ""

    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"

    sentry_auth_token: str = ""
    sentry_org: str = ""
    sentry_base_url: str = "https://sentry.io"

    elasticsearch_url: str = ""
    elasticsearch_api_key: str = ""
    elasticsearch_index_pattern: str = "logs-*"

    pagerduty_api_token: str = ""
    kubernetes_api_server: str = ""
    kubernetes_token: str = ""
    kubernetes_namespace: str = ""

    observability_query_timeout_s: int = 20
    observability_max_log_records: int = 500
    observability_max_evidence_records: int = 400
    observability_case_corpus_floor: int = 5        # below this, retrieval stays inert
    observability_learning_min_confidence: float = 0.5   # outcomes below this never teach
    observability_promotion_min_confidence: float = 0.7  # runbook synthesis/promotion gate
    observability_promotion_repeat_count: int = 3        # candidate -> active

    # ── Reversible identifier masking ────────────────────────────────────────
    observability_masking_enabled: bool = True
    observability_mask_classes: str = "pod,host,ip,account,cluster,namespace,email,url_host"
    observability_mask_max_tokens: int = 5000
    observability_mask_ttl_s: int = 7200

    # ── Notifications ────────────────────────────────────────────────────────
    notifications_enabled: bool = True
    slack_bot_token: str = ""
    slack_webhook_url: str = ""
    slack_default_channel: str = ""
    telegram_bot_token: str = ""
    telegram_default_chat_id: str = ""
    notification_dedupe_ttl_s: int = 3600
    app_public_url: str = "http://localhost:5173"   # for deep links in notifications

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

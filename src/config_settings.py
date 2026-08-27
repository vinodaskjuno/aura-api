from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

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

    # ── Model token pricing (USD per 1M tokens) ──────────────────────────────
    model_pricing: dict = {}  # populated at runtime

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


# Model pricing map: modelId → {input: USD/1M, output: USD/1M}
MODEL_PRICING: dict[str, dict[str, float]] = {
    "us.anthropic.claude-sonnet-4-20250514-v1:0": {"input": 3.0,   "output": 15.0},
    "us.anthropic.claude-opus-5-20251101-v1:0":   {"input": 15.0,  "output": 75.0},
    "us.anthropic.claude-haiku-4-5-20251001-v1:0":{"input": 0.8,   "output": 4.0},
    "claude-3-5-sonnet-20241022":                  {"input": 3.0,   "output": 15.0},
    "claude-sonnet-4-5":                           {"input": 3.0,   "output": 15.0},
    "claude-opus-5-20251101":                      {"input": 15.0,  "output": 75.0},
    "claude-haiku-4-5-20251001":                   {"input": 0.8,   "output": 4.0},
    "gpt-4o":                                      {"input": 2.5,   "output": 10.0},
    "gpt-4o-mini":                                 {"input": 0.15,  "output": 0.6},
    "amazon.nova-pro-v1:0":                        {"input": 0.8,   "output": 3.2},
}


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost for a model invocation."""
    pricing = MODEL_PRICING.get(model_id, {"input": 3.0, "output": 15.0})
    return round((input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000, 6)


# Tier model assignments (1=Premium, 2=Balanced, 3=Economy)
TIER_MODELS: dict[int, list[str]] = {
    1: [
        "us.anthropic.claude-opus-5-20251101-v1:0",
        "claude-opus-5-20251101",
    ],
    2: [
        "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "claude-3-5-sonnet-20241022",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "gpt-4o",
    ],
    3: [
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "claude-haiku-4-5-20251001",
        "amazon.nova-pro-v1:0",
        "gpt-4o-mini",
    ],
}

# Default model to fall back to when a tier is exhausted
TIER_DEFAULT_MODELS: dict[int, str] = {
    1: "us.anthropic.claude-sonnet-4-20250514-v1:0",  # fall back to tier 2 default
    2: "us.anthropic.claude-haiku-4-5-20251001-v1:0",  # fall back to tier 3 default
    3: "us.anthropic.claude-haiku-4-5-20251001-v1:0",  # already economy
}


def get_model_tier(model_id: str) -> int:
    """Return the tier number (1/2/3) for a given model ID. Defaults to 2."""
    for tier, models in TIER_MODELS.items():
        if model_id in models:
            return tier
    return 2

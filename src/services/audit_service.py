"""Gateway audit service — writes per-request records to DynamoDB and updates budget."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from time import monotonic

from src.config_settings import get_settings, calculate_cost
from src.database import dynamo_client as db
from src.routers.budget import update_user_budget

log = logging.getLogger(__name__)


def _infer_tool(user_agent: str) -> str:
    """Map User-Agent string to a readable tool name."""
    ua = (user_agent or "").lower()
    if "claude-code" in ua or "anthropic" in ua:
        return "claude-code"
    if "copilot" in ua:
        return "copilot"
    if "codex" in ua or "openai" in ua:
        return "codex"
    if "gemini" in ua or "google" in ua:
        return "gemini"
    if "cursor" in ua:
        return "cursor"
    return "unknown"


def record(
    *,
    user_id: str,
    model: str,
    backend_provider: str,
    input_tokens: int,
    output_tokens: int,
    status_code: int,
    latency_ms: int,
    user_agent: str = "",
    project_id: str = "",
    request_id: str = "",
    tool_source: str = "",
) -> None:
    """
    Write one gateway audit record to DynamoDB and update the user's budget.
    Intended to be called as a FastAPI BackgroundTask (fire-and-forget).

    tool_source: when the caller used a per-tool virtual key the key's toolLabel
    is passed here directly (most accurate). Falls back to User-Agent inference.
    """
    s = get_settings()
    cost = calculate_cost(model, input_tokens, output_tokens)
    now = datetime.now(timezone.utc).isoformat()
    rid = request_id or str(uuid.uuid4())
    tool = tool_source if tool_source and tool_source != "unknown" else _infer_tool(user_agent)

    item: dict = {
        "requestId": rid,
        "timestamp": now,
        "userId": user_id,
        "tool": tool,
        "model": model,
        "backendProvider": backend_provider,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "costUsd": str(round(cost, 6)),
        "latencyMs": latency_ms,
        "statusCode": status_code,
        "userAgent": user_agent[:512],
        "ttl": _ttl_90_days(),
    }
    if project_id:
        item["projectId"] = project_id

    try:
        db.put_item(s.gateway_audit_table, item)
    except Exception as exc:
        log.warning("audit_service.record: DynamoDB write failed: %s", exc)

    # Update the user's tier spend (best-effort)
    if input_tokens + output_tokens > 0:
        try:
            update_user_budget(user_id, model, cost)
        except Exception as exc:
            log.warning("audit_service.record: budget update failed: %s", exc)

    # Mirror into the main token-usage table (same as POST /api/metrics/tokens)
    try:
        sort_key = f"{now}#{rid[:8]}"
        db.put_item("token-usage", {
            "userId": user_id,
            "sortKey": sort_key,
            "sessionId": rid,
            "projectId": project_id or "",
            "model": model,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cost": str(round(cost, 6)),
            "timestamp": now,
            "source": "gateway",
            "tool": tool,        # tool-source label (aura-plugin, claude-ext, claude-cli, …)
        })
    except Exception as exc:
        log.warning("audit_service.record: token-usage mirror failed: %s", exc)


def _ttl_90_days() -> int:
    from time import time as _time
    return int(_time()) + 90 * 24 * 3600

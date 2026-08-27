"""AURA AI Gateway — core proxy service.

Responsibilities:
  - Authenticate requests via AURA JWT or gateway API key
  - Translate between Anthropic and OpenAI wire formats
  - Stream responses from providers using httpx (wire-level pass-through)
  - Expose helpers consumed by the gateway router
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import httpx
from fastapi import HTTPException, Request

from src.config_settings import get_settings, MODEL_PRICING
from src.database import dynamo_client as db
from src.services.auth_service import verify_token

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

class GatewayUser:
    def __init__(
        self,
        user_id: str,
        username: str,
        role: str,
        permissions: list[str],
        tool_label: str = "unknown",
    ):
        self.user_id = user_id
        self.username = username
        self.role = role
        self.permissions = permissions
        # Which tool made this request (aura-plugin, claude-ext, claude-cli, codex-cli, …)
        self.tool_label = tool_label


# ─────────────────────────────────────────────────────────────────────────────
# Gateway API key management helpers
# ─────────────────────────────────────────────────────────────────────────────

def _key_id(raw_key: str) -> str:
    """Stable ID derived from the raw key (SHA-256 prefix)."""
    return hashlib.sha256(raw_key.encode()).hexdigest()[:32]


def generate_api_key(user_id: str, label: str = "", tool_label: str = "") -> dict:
    """Create a new gateway API key. Returns the plain-text key (shown once).

    tool_label identifies which AI tool will use this key
    (e.g. 'aura-plugin', 'claude-ext', 'claude-cli', 'codex-cli').
    Stored on the key record so audit logs can attribute usage to the right tool
    without relying on User-Agent parsing.
    """
    s = get_settings()
    raw_key = "gw-" + secrets.token_urlsafe(48)
    kid = _key_id(raw_key)
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "keyId": kid,
        "userId": user_id,
        "keyHint": raw_key[-4:],
        "label": label or "default",
        "toolLabel": tool_label or "unknown",
        "createdAt": now,
        "lastUsedAt": "",
        "active": True,
    }
    db.put_item(s.gateway_keys_table, item)
    log.info("Created gateway API key %s for user %s (tool=%s)", kid, user_id, tool_label)
    return {"keyId": kid, "key": raw_key, "label": item["label"], "toolLabel": item["toolLabel"], "createdAt": now}


def get_or_create_tool_key(user_id: str, tool_label: str) -> dict:
    """Return the existing active key for this (user, tool_label) pair, or create one.

    Used by the VS Code plugin on login to provision per-tool virtual keys without
    duplicating them on repeated login/logout cycles.

    Uses a scan with FilterExpression rather than the userId-index GSI so that it works
    even on tables that were created before that GSI was added.
    """
    from boto3.dynamodb.conditions import Attr
    s = get_settings()
    existing = db.scan_items(
        s.gateway_keys_table,
        filter_expr=Attr("userId").eq(user_id) & Attr("toolLabel").eq(tool_label),
        limit=10,
    )
    for item in existing:
        if item.get("active", True):
            # Return existing key metadata (key plaintext is not stored — hint only)
            return {
                "keyId": item["keyId"],
                "keyHint": item.get("keyHint", ""),
                "label": item.get("label", ""),
                "toolLabel": tool_label,
                "createdAt": item.get("createdAt", ""),
                "exists": True,
            }
    # No existing key — create one
    return {**generate_api_key(user_id, label=tool_label, tool_label=tool_label), "exists": False}


def list_user_keys(user_id: str) -> list[dict]:
    s = get_settings()
    items = db.query_items(
        s.gateway_keys_table,
        pk_name="userId",
        pk_value=user_id,
        index_name="userId-index",
    )
    return [
        {
            "keyId": it["keyId"],
            "label": it.get("label", ""),
            "keyHint": it.get("keyHint", ""),
            "createdAt": it.get("createdAt", ""),
            "lastUsedAt": it.get("lastUsedAt", ""),
            "active": it.get("active", True),
        }
        for it in items
        if it.get("active", True)
    ]


def revoke_api_key(key_id: str, user_id: str, is_admin: bool = False) -> None:
    s = get_settings()
    item = db.get_item(s.gateway_keys_table, {"keyId": key_id})
    if not item:
        raise HTTPException(status_code=404, detail="Key not found")
    if not is_admin and item.get("userId") != user_id:
        raise HTTPException(status_code=403, detail="Not your key")
    db.update_item(s.gateway_keys_table, {"keyId": key_id}, {"active": False})


def list_all_keys() -> list[dict]:
    s = get_settings()
    items = db.scan_items(s.gateway_keys_table, limit=500)
    return [
        {
            "keyId": it["keyId"],
            "userId": it.get("userId", ""),
            "label": it.get("label", ""),
            "keyHint": it.get("keyHint", ""),
            "createdAt": it.get("createdAt", ""),
            "lastUsedAt": it.get("lastUsedAt", ""),
            "active": it.get("active", True),
        }
        for it in items
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────

async def authenticate_request(request: Request) -> GatewayUser:
    """
    Resolve a GatewayUser from the Authorization header.

    Accepts two token forms:
      1. AURA JWT  — standard bearer token from /auth/login
      2. Gateway API key — "gw-<random>" issued by POST /gateway/keys
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = auth_header[len("Bearer "):]

    # ── Try AURA JWT first ────────────────────────────────────────────────────
    if not token.startswith("gw-"):
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired AURA token")
        return GatewayUser(
            user_id=payload.get("userId", ""),
            username=payload.get("username", ""),
            role=payload.get("role", ""),
            permissions=payload.get("permissions", []),
        )

    # ── Gateway API key ───────────────────────────────────────────────────────
    s = get_settings()
    kid = _key_id(token)
    item = db.get_item(s.gateway_keys_table, {"keyId": kid})
    if not item or not item.get("active", True):
        raise HTTPException(status_code=401, detail="Invalid or revoked gateway API key")

    try:
        db.update_item(
            s.gateway_keys_table,
            {"keyId": kid},
            {"lastUsedAt": datetime.now(timezone.utc).isoformat()},
        )
    except Exception:
        pass

    user_id = item.get("userId", "")
    user_record = db.get_item("users", {"userId": user_id}) or {}
    role = user_record.get("roleId", "user_dev")

    return GatewayUser(
        user_id=user_id,
        username=user_record.get("username", user_id),
        role=role,
        permissions=user_record.get("permissions", []),
        tool_label=item.get("toolLabel", "unknown"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Policy enforcement
# ─────────────────────────────────────────────────────────────────────────────

# Explicit denylist — add model IDs here to block them for all users.
# Empty by default: the gateway allows any model and falls back to Sonnet
# pricing for unknowns (see calculate_cost in config_settings.py).
_DENIED_MODELS: set[str] = set()


def check_policy(user: GatewayUser, model: str) -> None:
    """Block explicitly denied models; allow everything else."""
    if model and model in _DENIED_MODELS:
        raise HTTPException(
            status_code=403,
            detail=f"Model '{model}' is not permitted by AURA policy",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Model routing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_anthropic_model(model: str) -> bool:
    return "claude" in model.lower() or "anthropic" in model.lower()


def _is_google_model(model: str) -> bool:
    return "gemini" in model.lower() or "google" in model.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Protocol translation helpers
# ─────────────────────────────────────────────────────────────────────────────

def translate_openai_to_anthropic(body: dict) -> dict:
    """Convert an OpenAI chat/completions request body to Anthropic Messages API format."""
    messages = body.get("messages", [])
    system_parts: list[str] = []
    anthropic_messages: list[dict] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts.extend(p.get("text", "") for p in content if p.get("type") == "text")
        elif role in ("user", "assistant"):
            if isinstance(content, str):
                anthropic_messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                blocks = []
                for part in content:
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            media_type, data = url.split(",", 1)
                            media_type = media_type.split(";")[0].split(":")[1]
                            blocks.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": data},
                            })
                anthropic_messages.append({"role": role, "content": blocks})

    result: dict = {
        "model": body.get("model", "claude-3-5-sonnet-20241022"),
        "max_tokens": body.get("max_tokens") or 4096,
        "messages": anthropic_messages,
    }
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    if body.get("temperature") is not None:
        result["temperature"] = body["temperature"]
    if body.get("stop"):
        result["stop_sequences"] = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
    return result


def translate_anthropic_to_openai(body: dict) -> dict:
    """Convert an Anthropic Messages request body to OpenAI chat/completions format."""
    messages: list[dict] = []
    system = body.get("system", "")
    if system:
        messages.append({"role": "system", "content": system})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            parts = []
            for block in content:
                if block.get("type") == "text":
                    parts.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        url = f"data:{src['media_type']};base64,{src['data']}"
                        parts.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": role, "content": parts or ""})

    result: dict = {
        "model": body.get("model", "gpt-4o"),
        "messages": messages,
        "max_tokens": body.get("max_tokens") or 4096,
    }
    if body.get("temperature") is not None:
        result["temperature"] = body["temperature"]
    if body.get("stop_sequences"):
        result["stop"] = body["stop_sequences"]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Backend streaming proxies (httpx — wire-level pass-through)
# ─────────────────────────────────────────────────────────────────────────────

async def _proxy_http_stream(
    url: str,
    headers: dict,
    json_body: dict,
) -> AsyncGenerator[bytes, None]:
    """Low-level httpx streaming proxy — yields raw response bytes."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0)) as client:
        async with client.stream("POST", url, headers=headers, json=json_body) as resp:
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                raise HTTPException(status_code=resp.status_code, detail=body_bytes.decode(errors="replace"))
            async for chunk in resp.aiter_bytes(chunk_size=None):
                if chunk:
                    yield chunk


_BEDROCK_MODEL_MAP: dict[str, str] = {
    "claude-sonnet-4-6":      "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-sonnet-4-5":      "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-3-5-sonnet-20241022": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-opus-5-20251101": "us.anthropic.claude-opus-5-20251101-v1:0",
    "claude-haiku-4-5-20251001": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}


def _resolve_bedrock_model(model: str, fallback: str) -> str:
    """Map a Claude alias to its Bedrock model ID, or return as-is if already a Bedrock ID."""
    if model.startswith("us.anthropic.") or model.startswith("anthropic."):
        return model
    return _BEDROCK_MODEL_MAP.get(model, fallback)


def _use_bedrock(s) -> bool:
    """True when Bedrock should be used — either configured explicitly or Anthropic key is absent."""
    return s.llm_backend == "bedrock" or not s.anthropic_api_key


async def proxy_anthropic_stream(body: dict, model: str) -> AsyncGenerator[bytes, None]:
    """Stream to Anthropic API or Bedrock and yield raw SSE bytes."""
    s = get_settings()
    send_body = {**body, "model": model, "stream": True}

    if _use_bedrock(s):
        import boto3
        import json as _json
        bedrock_model = _resolve_bedrock_model(model, s.bedrock_model_id)
        client = boto3.client(
            "bedrock-runtime",
            region_name=s.bedrock_region,
            aws_access_key_id=s.aws_access_key_id or None,
            aws_secret_access_key=s.aws_secret_access_key or None,
        )
        bedrock_body = {k: v for k, v in send_body.items() if k not in ("stream", "model")}
        bedrock_body["anthropic_version"] = "bedrock-2023-05-31"
        resp = client.invoke_model_with_response_stream(
            modelId=bedrock_model,
            body=_json.dumps(bedrock_body),
            contentType="application/json",
            accept="application/json",
        )
        for event in resp["body"]:
            chunk_data = event.get("chunk", {})
            if chunk_data:
                data = _json.loads(chunk_data.get("bytes", b"{}"))
                yield ("data: " + _json.dumps(data) + "\n\n").encode()
        yield b"data: [DONE]\n\n"
    else:
        # Direct Anthropic API — wire-level httpx pass-through
        async for chunk in _proxy_http_stream(
            url="https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": s.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "accept": "text/event-stream",
            },
            json_body=send_body,
        ):
            yield chunk


async def proxy_openai_stream(
    body: dict, model: str, google: bool = False
) -> AsyncGenerator[bytes, None]:
    """Stream to OpenAI or Google (via OpenAI-compat endpoint) and yield raw SSE bytes."""
    s = get_settings()
    if google:
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        api_key = s.google_api_key
    else:
        url = f"{s.openai_base_url.rstrip('/')}/chat/completions"
        api_key = s.openai_api_key

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail=f"No API key configured for {'Google' if google else 'OpenAI'} backend",
        )

    send_body = {**body, "model": model, "stream": True}
    async for chunk in _proxy_http_stream(
        url=url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        },
        json_body=send_body,
    ):
        yield chunk


# ─────────────────────────────────────────────────────────────────────────────
# Unified proxy entry point
# ─────────────────────────────────────────────────────────────────────────────

async def proxy_stream(
    body: dict, model: str, protocol: str
) -> AsyncGenerator[bytes, None]:
    """
    Route a streaming request to the correct backend.

    protocol: 'anthropic' | 'openai'
    Backend selection is based on the model name.
    Cross-protocol translation is applied automatically when needed.
    """
    if protocol == "anthropic":
        if _is_google_model(model):
            oai_body = translate_anthropic_to_openai(body)
            async for chunk in proxy_openai_stream(oai_body, model, google=True):
                yield chunk
        elif _is_anthropic_model(model):
            async for chunk in proxy_anthropic_stream(body, model):
                yield chunk
        else:
            # OpenAI model requested via Anthropic protocol — translate
            oai_body = translate_anthropic_to_openai(body)
            async for chunk in proxy_openai_stream(oai_body, model):
                yield chunk
    else:  # openai protocol
        if _is_anthropic_model(model):
            # Claude model requested via OpenAI protocol — translate
            anthropic_body = translate_openai_to_anthropic(body)
            anthropic_body["model"] = model
            async for chunk in proxy_anthropic_stream(anthropic_body, model):
                yield chunk
        elif _is_google_model(model):
            async for chunk in proxy_openai_stream(body, model, google=True):
                yield chunk
        else:
            async for chunk in proxy_openai_stream(body, model):
                yield chunk


# ─────────────────────────────────────────────────────────────────────────────
# Non-streaming (sync) proxy — for token counting and non-stream requests
# ─────────────────────────────────────────────────────────────────────────────

async def proxy_anthropic_sync(body: dict, model: str) -> dict:
    """Non-streaming Anthropic call — returns full response as a dict."""
    s = get_settings()
    send_body = {k: v for k, v in body.items() if k != "stream"}

    if _use_bedrock(s):
        import boto3
        import json as _json
        bedrock_model = _resolve_bedrock_model(model, s.bedrock_model_id)
        client = boto3.client(
            "bedrock-runtime",
            region_name=s.bedrock_region,
            aws_access_key_id=s.aws_access_key_id or None,
            aws_secret_access_key=s.aws_secret_access_key or None,
        )
        bedrock_body = {k: v for k, v in send_body.items() if k != "model"}
        bedrock_body["anthropic_version"] = "bedrock-2023-05-31"
        resp = client.invoke_model(
            modelId=bedrock_model,
            body=_json.dumps(bedrock_body),
            contentType="application/json",
            accept="application/json",
        )
        return _json.loads(resp["body"].read())
    else:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0)) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": s.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=send_body,
            )
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Model listing
# ─────────────────────────────────────────────────────────────────────────────

def available_models() -> list[dict]:
    import time as _time
    now_ts = int(_time.time())
    return [
        {"id": model_id, "object": "model", "created": now_ts, "owned_by": "aura-gateway"}
        for model_id in MODEL_PRICING
    ]

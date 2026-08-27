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

from src.config_settings import get_settings, normalize_model_id, MODEL_PRICING
from src.database import dynamo_client as db
from src.services.auth_service import verify_token

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

class UpstreamError(Exception):
    """A provider returned an error.

    Carries the provider's own status, body and rate-limit headers so the router
    can hand the caller back something in the provider's native error shape.
    Raising a bare HTTPException here produced {"detail": "..."}, which the
    Anthropic SDK cannot parse — the user saw only a generic "API Error" with no
    indication of the cause.
    """

    def __init__(self, status_code: int, body: str, headers: dict | None = None):
        super().__init__(f"upstream {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}


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


def rotate_tool_key(user_id: str, tool_label: str) -> dict:
    """Revoke any existing key for this (user, tool) pair and issue a fresh one.

    Needed because the plaintext key is only ever returned at creation time.
    If the client loses it -- SecretStorage cleared, a reinstall, or a second
    machine -- get_or_create_tool_key returns {"exists": true} with no `key`,
    the client stores nothing, and anything depending on that key (Claude Code
    telemetry included) silently stops working with no error surfaced anywhere.

    Rotation is the only safe recovery: we cannot recover the old plaintext, so
    we replace it. Callers should rotate only when they genuinely have no stored
    key, since rotation invalidates the key on every other machine.
    """
    from boto3.dynamodb.conditions import Attr
    s = get_settings()

    try:
        existing = db.scan_items(
            s.gateway_keys_table,
            filter_expr=Attr("userId").eq(user_id) & Attr("toolLabel").eq(tool_label),
            limit=25,
        )
    except Exception as exc:
        log.warning("rotate_tool_key: lookup failed for %s/%s: %s", user_id, tool_label, exc)
        existing = []

    revoked = 0
    for item in existing:
        if not item.get("active", True):
            continue
        try:
            db.update_item(s.gateway_keys_table, {"keyId": item["keyId"]}, {"active": False})
            revoked += 1
        except Exception as exc:
            log.warning("rotate_tool_key: could not revoke %s: %s", item.get("keyId"), exc)

    created = generate_api_key(user_id, label=tool_label, tool_label=tool_label)
    log.info("Rotated gateway key for %s/%s (revoked %d)", user_id, tool_label, revoked)
    return {**created, "rotated": True, "revokedCount": revoked}


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

def extract_credential(request: Request) -> str:
    """Pull the caller's credential out of whichever header carries it.

    Header choice is not ours to make — it is decided by the client's env:
        ANTHROPIC_API_KEY    -> x-api-key: <key>
        ANTHROPIC_AUTH_TOKEN -> Authorization: Bearer <token>
        OPENAI_API_KEY       -> Authorization: Bearer <key>
        OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ... -> Authorization

    Reading only Authorization is what made every Claude Code request 401:
    Claude Code sends x-api-key when ANTHROPIC_API_KEY is set, so the gateway
    rejected the request before it ever reached a provider.
    """
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return api_key

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        # Bare token without the scheme — accept rather than fail obscurely.
        return auth_header

    raise HTTPException(
        status_code=401,
        detail="Missing credential: send an AURA JWT or gateway key in "
               "'Authorization: Bearer <token>' or 'x-api-key: <key>'",
    )


def resolve_credential(token: str) -> GatewayUser:
    """Resolve a raw credential string to a GatewayUser.

    Accepts two token forms:
      1. AURA JWT  — standard bearer token from /auth/login
      2. Gateway API key — "gw-<random>" issued by POST /gateway/keys

    Shared by the gateway wire routes and the OTLP telemetry receiver so both
    honour the same virtual keys.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Empty credential")

    # ── AURA JWT ──────────────────────────────────────────────────────────────
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
        permissions=_permissions_for_role(role),
        tool_label=item.get("toolLabel", "unknown"),
    )


def _permissions_for_role(role: str) -> list[str]:
    """Look up a role's permissions.

    Permissions live on the `roles` table, not on user records — reading
    users[].permissions (as this used to) always yielded [], so every API-key
    caller appeared to have no permissions at all.
    """
    if not role:
        return []
    try:
        record = db.get_item("roles", {"roleId": role}) or {}
        perms = record.get("permissions") or []
        if perms:
            return list(perms)
    except Exception as exc:
        log.warning("resolve_credential: role lookup failed for %r: %s", role, exc)

    # Fall back to the in-code defaults seeded into the roles table at boot.
    try:
        from src.services.auth_service import ROLE_PERMISSIONS
        return list(ROLE_PERMISSIONS.get(role, []))
    except Exception:
        return []


async def authenticate_request(request: Request) -> GatewayUser:
    """Resolve a GatewayUser from the request's credential header."""
    return resolve_credential(extract_credential(request))


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

# Client headers forwarded upstream. Dropping these breaks any feature the
# client negotiated: anthropic-beta gates prompt caching, 1M context, fine-
# grained tool streaming and more, and hard-coding anthropic-version pins every
# caller to the 2023-06-01 schema regardless of what they asked for.
_FORWARD_REQUEST_HEADERS = (
    "anthropic-beta",
    "anthropic-version",
    "anthropic-dangerous-direct-browser-access",
    "x-request-id",
    "idempotency-key",
)

# Upstream headers worth returning: rate-limit and retry hints are useless to a
# client that never sees them.
_FORWARD_RESPONSE_HEADERS = (
    "retry-after",
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-organization-id",
    "request-id",
)


def forwardable_headers(request: Request | None) -> dict[str, str]:
    """Subset of the client's headers that should reach the provider."""
    if request is None:
        return {}
    out: dict[str, str] = {}
    for name in _FORWARD_REQUEST_HEADERS:
        value = request.headers.get(name)
        if value:
            out[name] = value
    return out


# Streaming turns can idle for a long time between chunks on a hard task, and a
# large tool-heavy request body takes real time to write.
_STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=120.0, pool=15.0)
_SYNC_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=120.0, pool=15.0)


async def _proxy_http_stream(
    url: str,
    headers: dict,
    json_body: dict,
    response_headers: dict | None = None,
) -> AsyncGenerator[bytes, None]:
    """Low-level httpx streaming proxy — yields raw response bytes.

    `response_headers`, when given, is populated with the upstream headers worth
    forwarding before the first chunk is yielded.
    """
    async with httpx.AsyncClient(timeout=_STREAM_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=json_body) as resp:
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                raise UpstreamError(
                    status_code=resp.status_code,
                    body=body_bytes.decode(errors="replace"),
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() in _FORWARD_RESPONSE_HEADERS},
                )
            if response_headers is not None:
                for k, v in resp.headers.items():
                    if k.lower() in _FORWARD_RESPONSE_HEADERS:
                        response_headers[k] = v
            async for chunk in resp.aiter_bytes(chunk_size=None):
                if chunk:
                    yield chunk


# Claude alias -> Bedrock model ID.
#
# Keys are CANONICAL ids (normalize_model_id output), so dated snapshots and
# already-prefixed Bedrock IDs resolve without needing an entry each.
# claude-sonnet-4-6 previously mapped to the sonnet-4 Bedrock ID alongside
# claude-sonnet-4-5, so a 4.6 request was silently served AND priced as a
# different model.
_BEDROCK_MODEL_MAP: dict[str, str] = {
    "claude-opus-5":       "us.anthropic.claude-opus-5-20251101-v1:0",
    "claude-opus-4-5":     "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-sonnet-4-5":   "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4-5":    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-3-5-sonnet":   "anthropic.claude-3-5-sonnet-20241022-v2:0",
}


def _resolve_bedrock_model(model: str, fallback: str) -> str:
    """Map a Claude alias to its Bedrock model ID, or return as-is if already one."""
    if model.startswith(("us.anthropic.", "eu.anthropic.", "apac.anthropic.", "anthropic.")):
        return model
    mapped = _BEDROCK_MODEL_MAP.get(normalize_model_id(model))
    if mapped:
        return mapped
    log.warning(
        "No Bedrock mapping for model %r — falling back to %s. Requests will be "
        "served AND priced as the fallback model.", model, fallback,
    )
    return fallback


def _use_bedrock(s) -> bool:
    """True only when Bedrock is explicitly configured.

    This used to also return True whenever anthropic_api_key was empty, so a
    missing key silently rerouted every request to Bedrock — whose streaming
    path emits a non-Anthropic SSE dialect that Anthropic clients cannot parse.
    A missing key is now a clear error instead of a silent backend switch.
    """
    return s.llm_backend == "bedrock"


async def proxy_anthropic_stream(
    body: dict,
    model: str,
    client_headers: dict | None = None,
    response_headers: dict | None = None,
) -> AsyncGenerator[bytes, None]:
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
        # Bedrock hands back bare JSON objects. Anthropic's SSE decoder dispatches
        # on the `event:` line and has no concept of `data: [DONE]`, so emitting
        # the OpenAI-style framing this used to produce broke every Anthropic
        # client outright. Re-frame each chunk as a proper named SSE event.
        for event in resp["body"]:
            chunk_data = event.get("chunk", {})
            if not chunk_data:
                continue
            data = _json.loads(chunk_data.get("bytes", b"{}"))
            event_type = data.get("type")
            if not event_type:
                continue
            yield (
                f"event: {event_type}\n"
                f"data: {_json.dumps(data)}\n\n"
            ).encode()
    else:
        # Direct Anthropic API — wire-level httpx pass-through
        if not s.anthropic_api_key:
            raise HTTPException(
                status_code=500,
                detail="No ANTHROPIC_API_KEY configured on the AURA server. "
                       "Set it, or set LLM_BACKEND=bedrock.",
            )
        # anthropic-version is a DEFAULT here: a client that sent its own wins,
        # because forwarded headers are merged last.
        headers = {
            "x-api-key": s.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "text/event-stream",
            **(client_headers or {}),
        }
        async for chunk in _proxy_http_stream(
            url="https://api.anthropic.com/v1/messages",
            headers=headers,
            json_body=send_body,
            response_headers=response_headers,
        ):
            yield chunk


async def proxy_openai_stream(
    body: dict, model: str, google: bool = False,
    response_headers: dict | None = None,
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
    # Ask for a usage frame on the final chunk — without this an OpenAI-protocol
    # stream reports no token counts at all, so the request goes unbilled.
    send_body.setdefault("stream_options", {"include_usage": True})
    async for chunk in _proxy_http_stream(
        url=url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        },
        json_body=send_body,
        response_headers=response_headers,
    ):
        yield chunk


# ─────────────────────────────────────────────────────────────────────────────
# Unified proxy entry point
# ─────────────────────────────────────────────────────────────────────────────

async def proxy_stream(
    body: dict, model: str, protocol: str,
    client_headers: dict | None = None,
    response_headers: dict | None = None,
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
            async for chunk in proxy_openai_stream(
                oai_body, model, google=True, response_headers=response_headers):
                yield chunk
        elif _is_anthropic_model(model):
            async for chunk in proxy_anthropic_stream(
                body, model, client_headers=client_headers,
                response_headers=response_headers):
                yield chunk
        else:
            # OpenAI model requested via Anthropic protocol — translate
            oai_body = translate_anthropic_to_openai(body)
            async for chunk in proxy_openai_stream(
                oai_body, model, response_headers=response_headers):
                yield chunk
    else:  # openai protocol
        if _is_anthropic_model(model):
            # Claude model requested via OpenAI protocol — translate
            anthropic_body = translate_openai_to_anthropic(body)
            anthropic_body["model"] = model
            async for chunk in proxy_anthropic_stream(
                anthropic_body, model, client_headers=client_headers,
                response_headers=response_headers):
                yield chunk
        elif _is_google_model(model):
            async for chunk in proxy_openai_stream(
                body, model, google=True, response_headers=response_headers):
                yield chunk
        else:
            async for chunk in proxy_openai_stream(
                body, model, response_headers=response_headers):
                yield chunk


# ─────────────────────────────────────────────────────────────────────────────
# Non-streaming (sync) proxy — for token counting and non-stream requests
# ─────────────────────────────────────────────────────────────────────────────

async def proxy_anthropic_count_tokens(body: dict, model: str,
                                       client_headers: dict | None = None) -> dict:
    """Proxy to Anthropic's /v1/messages/count_tokens.

    This used to be routed through proxy_anthropic_sync, which is hard-coded to
    /v1/messages — so a token-count request either 400'd on the missing
    max_tokens or ran, and BILLED, a real completion. Bedrock has no
    count-tokens equivalent, so that backend is rejected explicitly rather than
    silently invoking the model.
    """
    s = get_settings()
    if _use_bedrock(s):
        raise HTTPException(
            status_code=501,
            detail="Token counting is not available on the Bedrock backend.",
        )
    if not s.anthropic_api_key:
        raise HTTPException(status_code=500, detail="No ANTHROPIC_API_KEY configured.")

    send_body = {k: v for k, v in body.items() if k not in ("stream", "max_tokens")}
    send_body["model"] = model

    async with httpx.AsyncClient(timeout=_SYNC_TIMEOUT) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages/count_tokens",
            headers={
                "x-api-key": s.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                **(client_headers or {}),
            },
            json=send_body,
        )
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text,
                                {k: v for k, v in resp.headers.items()
                                 if k.lower() in _FORWARD_RESPONSE_HEADERS})
        return resp.json()


async def proxy_anthropic_sync(body: dict, model: str,
                               client_headers: dict | None = None) -> dict:
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
        if not s.anthropic_api_key:
            raise HTTPException(
                status_code=500,
                detail="No ANTHROPIC_API_KEY configured on the AURA server. "
                       "Set it, or set LLM_BACKEND=bedrock.",
            )
        async with httpx.AsyncClient(timeout=_SYNC_TIMEOUT) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": s.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                    **(client_headers or {}),
                },
                json=send_body,
            )
            if resp.status_code >= 400:
                raise UpstreamError(resp.status_code, resp.text,
                                    {k: v for k, v in resp.headers.items()
                                     if k.lower() in _FORWARD_RESPONSE_HEADERS})
            return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Streaming usage extraction
# ─────────────────────────────────────────────────────────────────────────────

class SSEUsageExtractor:
    """Accumulates token usage out of an SSE stream as it passes through.

    Replaces a substring scan of each raw chunk. That approach missed usage
    whenever a `usage` field straddled a TCP chunk boundary, and the request was
    then recorded with zero tokens -- unbilled and invisible. This buffers the
    byte stream and only parses complete lines, so a split frame is still read.

    Handles both wire formats and all four Anthropic token classes; cache tokens
    were previously never captured at all, which over-reports cost badly on
    cache-heavy traffic.
    """

    __slots__ = ("_buf", "input_tokens", "output_tokens",
                 "cache_read_tokens", "cache_creation_tokens")

    def __init__(self) -> None:
        self._buf = b""
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        # Keep the trailing partial line in the buffer for the next chunk.
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                import json as _json
                evt = _json.loads(payload)
            except Exception:
                continue
            self._absorb(evt)

    def _absorb(self, evt: dict) -> None:
        if not isinstance(evt, dict):
            return
        usage = evt.get("usage") or (evt.get("message") or {}).get("usage") or {}
        if not isinstance(usage, dict):
            return

        # Anthropic reports input_tokens once on message_start and output_tokens
        # cumulatively on message_delta, so take the max rather than overwrite:
        # a later frame carrying only output must not zero the input count.
        self.input_tokens = max(
            self.input_tokens,
            int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        )
        self.output_tokens = max(
            self.output_tokens,
            int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        )
        self.cache_read_tokens = max(
            self.cache_read_tokens,
            int(usage.get("cache_read_input_tokens") or 0),
        )
        self.cache_creation_tokens = max(
            self.cache_creation_tokens,
            int(usage.get("cache_creation_input_tokens") or 0),
        )

    def as_kwargs(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }


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

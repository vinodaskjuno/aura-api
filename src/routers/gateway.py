"""AURA AI Gateway — wire-protocol endpoints.

Exposes two protocol surfaces under /gateway:

  POST /gateway/v1/messages              Anthropic Messages API
  POST /gateway/v1/messages/count_tokens Anthropic token count
  POST /gateway/v1/chat/completions      OpenAI Chat Completions API
  GET  /gateway/v1/models                OpenAI model list

This serves AURA's own agents and chat (see aura-vsix AgentManager.toLLMModel).

IT IS NOT FOR CLAUDE CODE. Routing Claude Code through here broke it outright —
Claude Code sends its key as `x-api-key`, which this gateway did not read, so
every request 401'd. Claude Code usage is captured from its own OpenTelemetry
export instead; see routers/otlp.py. Do not reintroduce ANTHROPIC_BASE_URL
redirection for it.
"""
from __future__ import annotations

import logging
import uuid
from time import monotonic

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from src.config_settings import get_settings
from src.routers.budget import check_and_enforce_budget
from src.services import rate_limiter, audit_service
from src.services.gateway_service import (
    GatewayUser,
    SSEUsageExtractor,
    UpstreamError,
    authenticate_request,
    available_models,
    check_policy,
    forwardable_headers,
    proxy_anthropic_count_tokens,
    proxy_anthropic_sync,
    proxy_stream,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["gateway"])


# ─────────────────────────────────────────────────────────────────────────────
# Auth dependency (shared by all gateway routes)
# ─────────────────────────────────────────────────────────────────────────────

async def _get_gateway_user(request: Request) -> GatewayUser:
    return await authenticate_request(request)


# ─────────────────────────────────────────────────────────────────────────────
# Error framing
# ─────────────────────────────────────────────────────────────────────────────

# Anthropic error `type` per HTTP status. Clients branch on these, and the SDK
# raises a typed exception from them.
_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


def _anthropic_error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    """Return an error in Anthropic's native envelope.

    FastAPI's default {"detail": "..."} is unparseable by the Anthropic SDK, so
    the caller saw a bare "API Error" with the cause discarded.
    """
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {"type": _ERROR_TYPES.get(status, "api_error"), "message": message},
        },
        headers=headers or {},
    )


def _passthrough_upstream(exc: UpstreamError) -> JSONResponse:
    """Forward a provider error verbatim when it is already well-formed."""
    import json as _json
    try:
        parsed = _json.loads(exc.body)
        if isinstance(parsed, dict) and parsed.get("type") == "error":
            return JSONResponse(status_code=exc.status_code, content=parsed,
                                headers=exc.headers)
    except Exception:
        pass
    return _anthropic_error(exc.status_code, exc.body[:2000], exc.headers)


# ─────────────────────────────────────────────────────────────────────────────
# Model list (OpenAI format) — used by Copilot / Codex tool discovery
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/v1/models")
async def list_models(user: GatewayUser = Depends(_get_gateway_user)):
    return {"object": "list", "data": available_models()}


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic Messages API surface
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    background_tasks: BackgroundTasks,
    user: GatewayUser = Depends(_get_gateway_user),
):
    """Drop-in replacement for POST https://api.anthropic.com/v1/messages."""
    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "Request body is not valid JSON")
    if not isinstance(body, dict):
        return _anthropic_error(400, "Request body must be a JSON object")

    model = body.get("model", get_settings().anthropic_model_id)
    stream = bool(body.get("stream", False))

    try:
        check_policy(user, model)
        await rate_limiter.check(user.user_id)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail),
                                getattr(exc, "headers", None))

    s = get_settings()
    resolved_model, budget_events = await check_and_enforce_budget(user.user_id, model, s)
    downgraded_from = None
    if resolved_model != model:
        log.info("Gateway: budget downgrade %s → %s for user %s",
                 model, resolved_model, user.user_id)
        downgraded_from = model
        model = resolved_model
        body = {**body, "model": model}

    request_id = str(uuid.uuid4())
    user_agent = request.headers.get("User-Agent", "")
    client_headers = forwardable_headers(request)

    # The caller is otherwise never told its model was swapped mid-conversation.
    # budget_events used to be computed here and discarded entirely.
    extra_headers: dict[str, str] = {"X-Request-Id": request_id}
    if downgraded_from:
        extra_headers["X-AURA-Model-Downgraded-From"] = downgraded_from
        extra_headers["X-AURA-Model-Served"] = model
    if budget_events:
        extra_headers["X-AURA-Budget-Event"] = ",".join(
            str(e.get("type", "budget")) for e in budget_events if isinstance(e, dict)
        )[:200]

    if stream:
        t_start = monotonic()
        upstream_headers: dict[str, str] = {}

        async def _stream_with_audit():
            usage = SSEUsageExtractor()
            status = 200
            try:
                async for chunk in proxy_stream(
                    body, model, protocol="anthropic",
                    client_headers=client_headers,
                    response_headers=upstream_headers,
                ):
                    yield chunk
                    usage.feed(chunk)
            except UpstreamError as exc:
                status = exc.status_code
                log.warning("Gateway stream upstream error %s: %s",
                            exc.status_code, exc.body[:300])
                # Headers are already on the wire, so the status cannot be
                # changed. Emit an Anthropic `error` SSE event, which is exactly
                # how Anthropic signals a mid-stream failure.
                import json as _json
                payload = _json.dumps({
                    "type": "error",
                    "error": {"type": _ERROR_TYPES.get(exc.status_code, "api_error"),
                              "message": exc.body[:1000]},
                })
                yield f"event: error\ndata: {payload}\n\n".encode()
            except Exception as exc:
                status = 500
                log.error("Gateway stream error: %s", exc)
                import json as _json
                payload = _json.dumps({
                    "type": "error",
                    "error": {"type": "api_error", "message": str(exc)[:1000]},
                })
                yield f"event: error\ndata: {payload}\n\n".encode()
            finally:
                latency_ms = int((monotonic() - t_start) * 1000)
                background_tasks.add_task(
                    audit_service.record,
                    user_id=user.user_id,
                    model=model,
                    backend_provider=_backend_name(model),
                    status_code=status,
                    latency_ms=latency_ms,
                    user_agent=user_agent,
                    request_id=request_id,
                    tool_source=user.tool_label,
                    **usage.as_kwargs(),
                )

        return StreamingResponse(
            _stream_with_audit(),
            media_type="text/event-stream",
            headers={
                **extra_headers,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Non-streaming ────────────────────────────────────────────────────────
    t_start = monotonic()
    try:
        resp = await proxy_anthropic_sync(body, model, client_headers=client_headers)
    except UpstreamError as exc:
        background_tasks.add_task(
            audit_service.record,
            user_id=user.user_id, model=model,
            backend_provider=_backend_name(model),
            input_tokens=0, output_tokens=0,
            status_code=exc.status_code,
            latency_ms=int((monotonic() - t_start) * 1000),
            user_agent=user_agent, request_id=request_id,
            tool_source=user.tool_label,
        )
        return _passthrough_upstream(exc)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))
    except Exception as exc:
        log.error("Gateway sync error: %s", exc)
        return _anthropic_error(502, str(exc))

    latency_ms = int((monotonic() - t_start) * 1000)
    usage = resp.get("usage", {}) if isinstance(resp, dict) else {}
    background_tasks.add_task(
        audit_service.record,
        user_id=user.user_id,
        model=model,
        backend_provider=_backend_name(model),
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
        status_code=200,
        latency_ms=latency_ms,
        user_agent=user_agent,
        request_id=request_id,
        tool_source=user.tool_label,
    )
    return JSONResponse(content=resp, headers=extra_headers)


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    user: GatewayUser = Depends(_get_gateway_user),
):
    """Proxy to Anthropic's token-counting endpoint.

    This used to be sent to /v1/messages, which meant a count request either
    400'd on the missing max_tokens or ran a real, billed completion.
    """
    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "Request body is not valid JSON")

    model = body.get("model", get_settings().anthropic_model_id)
    try:
        check_policy(user, model)
        resp = await proxy_anthropic_count_tokens(
            body, model, client_headers=forwardable_headers(request))
    except UpstreamError as exc:
        return _passthrough_upstream(exc)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail))
    except Exception as exc:
        return _anthropic_error(502, str(exc))
    return JSONResponse(content=resp)


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI Chat Completions API surface
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/v1/chat/completions")
async def openai_chat_completions(
    request: Request,
    background_tasks: BackgroundTasks,
    user: GatewayUser = Depends(_get_gateway_user),
):
    """Drop-in replacement for POST https://api.openai.com/v1/chat/completions.

    Also accepts Anthropic Claude model IDs — the gateway translates the format.
    """
    try:
        body = await request.json()
    except Exception:
        return _anthropic_error(400, "Request body is not valid JSON")

    model = body.get("model", "gpt-4o")
    stream = bool(body.get("stream", False))

    try:
        check_policy(user, model)
        await rate_limiter.check(user.user_id)
    except HTTPException as exc:
        return _anthropic_error(exc.status_code, str(exc.detail),
                                getattr(exc, "headers", None))

    s = get_settings()
    resolved_model, _budget_events = await check_and_enforce_budget(user.user_id, model, s)
    downgraded_from = None
    if resolved_model != model:
        log.info("Gateway: budget downgrade %s → %s for user %s",
                 model, resolved_model, user.user_id)
        downgraded_from = model
        model = resolved_model
        body = {**body, "model": model}

    request_id = str(uuid.uuid4())
    user_agent = request.headers.get("User-Agent", "")
    client_headers = forwardable_headers(request)
    extra_headers: dict[str, str] = {"X-Request-Id": request_id}
    if downgraded_from:
        extra_headers["X-AURA-Model-Downgraded-From"] = downgraded_from
        extra_headers["X-AURA-Model-Served"] = model

    if stream:
        t_start = monotonic()

        async def _stream_with_audit():
            usage = SSEUsageExtractor()
            status = 200
            try:
                async for chunk in proxy_stream(
                    body, model, protocol="openai",
                    client_headers=client_headers,
                ):
                    yield chunk
                    usage.feed(chunk)
            except Exception as exc:
                status = getattr(exc, "status_code", 500)
                log.error("Gateway OpenAI stream error: %s", exc)
                import json as _json
                yield ("data: " + _json.dumps({
                    "error": {"message": str(exc)[:1000], "type": "api_error"},
                }) + "\n\n").encode()
                yield b"data: [DONE]\n\n"
            finally:
                latency_ms = int((monotonic() - t_start) * 1000)
                background_tasks.add_task(
                    audit_service.record,
                    user_id=user.user_id,
                    model=model,
                    backend_provider=_backend_name(model),
                    status_code=status,
                    latency_ms=latency_ms,
                    user_agent=user_agent,
                    request_id=request_id,
                    tool_source=user.tool_label,
                    **usage.as_kwargs(),
                )

        return StreamingResponse(
            _stream_with_audit(),
            media_type="text/event-stream",
            headers={
                **extra_headers,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Non-streaming ────────────────────────────────────────────────────────
    # Reassemble a real completion from the upstream stream.
    #
    # This previously kept only the LAST `data:` frame, which is a delta chunk
    # (usually the usage-only frame) — so a non-streaming caller received a body
    # with no assistant content at all. That is the most likely cause of the
    # gateway "returning nothing" for OpenAI-protocol clients.
    t_start = monotonic()
    usage = SSEUsageExtractor()
    text_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    meta: dict = {}
    finish_reason = None

    try:
        import json as _json
        buf = b""
        async for chunk in proxy_stream(body, model, protocol="openai",
                                        client_headers=client_headers):
            usage.feed(chunk)
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == b"[DONE]":
                    continue
                try:
                    evt = _json.loads(payload)
                except Exception:
                    continue
                for key in ("id", "model", "created", "system_fingerprint"):
                    if evt.get(key) is not None:
                        meta[key] = evt[key]
                for choice in evt.get("choices") or []:
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(idx, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
    except UpstreamError as exc:
        return _passthrough_upstream(exc)
    except Exception as exc:
        log.error("Gateway OpenAI sync error: %s", exc)
        return _anthropic_error(502, str(exc))

    latency_ms = int((monotonic() - t_start) * 1000)
    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]

    result = {
        "id": meta.get("id", f"chatcmpl-{request_id[:12]}"),
        "object": "chat.completion",
        "created": meta.get("created", int(monotonic())),
        "model": meta.get("model", model),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
        }],
        "usage": {
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.output_tokens,
        },
    }

    background_tasks.add_task(
        audit_service.record,
        user_id=user.user_id,
        model=model,
        backend_provider=_backend_name(model),
        status_code=200,
        latency_ms=latency_ms,
        user_agent=user_agent,
        request_id=request_id,
        tool_source=user.tool_label,
        **usage.as_kwargs(),
    )
    return JSONResponse(content=result, headers=extra_headers)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _backend_name(model: str) -> str:
    model_l = (model or "").lower()
    if "claude" in model_l or "anthropic" in model_l:
        s = get_settings()
        return "bedrock" if s.llm_backend == "bedrock" else "anthropic"
    if "gemini" in model_l or "google" in model_l:
        return "google"
    return "openai"

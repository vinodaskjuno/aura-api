"""AURA AI Gateway — wire-protocol endpoints.

Exposes two protocol surfaces under /gateway:

  POST /gateway/v1/messages              Anthropic Messages API
  POST /gateway/v1/messages/count_tokens Anthropic token count pass-through
  POST /gateway/v1/chat/completions      OpenAI Chat Completions API
  GET  /gateway/v1/models                OpenAI model list

Developers point their tools here:
  Claude Code:  ANTHROPIC_BASE_URL=https://<host>/gateway
  Codex/Copilot: OPENAI_BASE_URL=https://<host>/gateway
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
    authenticate_request,
    check_policy,
    proxy_stream,
    proxy_anthropic_sync,
    available_models,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["gateway"])


# ─────────────────────────────────────────────────────────────────────────────
# Auth dependency (shared by all gateway routes)
# ─────────────────────────────────────────────────────────────────────────────

async def _get_gateway_user(request: Request) -> GatewayUser:
    return await authenticate_request(request)


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
    """
    Drop-in replacement for POST https://api.anthropic.com/v1/messages.
    Set ANTHROPIC_BASE_URL=https://<host>/gateway to redirect Claude Code here.
    """
    body = await request.json()
    model = body.get("model", get_settings().anthropic_model_id)
    stream = body.get("stream", False)

    check_policy(user, model)
    await rate_limiter.check(user.user_id)

    s = get_settings()
    resolved_model, budget_events = await check_and_enforce_budget(user.user_id, model, s)
    if resolved_model != model:
        log.info("Gateway: budget downgrade %s → %s for user %s", model, resolved_model, user.user_id)
        model = resolved_model
        body = {**body, "model": model}

    request_id = str(uuid.uuid4())
    user_agent = request.headers.get("User-Agent", "")

    if stream:
        t_start = monotonic()

        async def _stream_with_audit():
            input_tokens = 0
            output_tokens = 0
            status = 200
            try:
                async for chunk in proxy_stream(body, model, protocol="anthropic"):
                    yield chunk
                    # Parse token counts from stream events when available
                    if b'"input_tokens"' in chunk:
                        try:
                            import json as _json
                            for line in chunk.decode(errors="ignore").splitlines():
                                if line.startswith("data:"):
                                    evt = _json.loads(line[5:].strip())
                                    usage = evt.get("usage") or evt.get("message", {}).get("usage") or {}
                                    input_tokens = usage.get("input_tokens", input_tokens)
                                    output_tokens = usage.get("output_tokens", output_tokens)
                        except Exception:
                            pass
            except Exception as exc:
                status = 500
                log.error("Gateway stream error: %s", exc)
                raise
            finally:
                latency_ms = int((monotonic() - t_start) * 1000)
                background_tasks.add_task(
                    audit_service.record,
                    user_id=user.user_id,
                    model=model,
                    backend_provider=_backend_name(model),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    status_code=status,
                    latency_ms=latency_ms,
                    user_agent=user_agent,
                    request_id=request_id,
                    tool_source=user.tool_label,
                )

        return StreamingResponse(
            _stream_with_audit(),
            media_type="text/event-stream",
            headers={
                "X-Request-Id": request_id,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
    else:
        t_start = monotonic()
        try:
            resp = await proxy_anthropic_sync(body, model)
        except HTTPException:
            raise
        except Exception as exc:
            log.error("Gateway sync error: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc))
        latency_ms = int((monotonic() - t_start) * 1000)
        usage = resp.get("usage", {})
        background_tasks.add_task(
            audit_service.record,
            user_id=user.user_id,
            model=model,
            backend_provider=_backend_name(model),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            status_code=200,
            latency_ms=latency_ms,
            user_agent=user_agent,
            request_id=request_id,
        )
        return JSONResponse(content=resp, headers={"X-Request-Id": request_id})


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    user: GatewayUser = Depends(_get_gateway_user),
):
    """Pass-through for Anthropic token counting endpoint."""
    body = await request.json()
    model = body.get("model", get_settings().anthropic_model_id)
    check_policy(user, model)
    try:
        resp = await proxy_anthropic_sync(body, model)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
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
    """
    Drop-in replacement for POST https://api.openai.com/v1/chat/completions.
    Set OPENAI_BASE_URL=https://<host>/gateway to redirect Codex / Copilot here.
    Also accepts Anthropic Claude model IDs — the gateway translates the format.
    """
    body = await request.json()
    model = body.get("model", "gpt-4o")
    stream = body.get("stream", False)

    check_policy(user, model)
    await rate_limiter.check(user.user_id)

    s = get_settings()
    resolved_model, budget_events = await check_and_enforce_budget(user.user_id, model, s)
    if resolved_model != model:
        log.info("Gateway: budget downgrade %s → %s for user %s", model, resolved_model, user.user_id)
        model = resolved_model
        body = {**body, "model": model}

    request_id = str(uuid.uuid4())
    user_agent = request.headers.get("User-Agent", "")

    if stream:
        t_start = monotonic()

        async def _stream_with_audit():
            input_tokens = 0
            output_tokens = 0
            status = 200
            try:
                async for chunk in proxy_stream(body, model, protocol="openai"):
                    yield chunk
                    if b'"usage"' in chunk:
                        try:
                            import json as _json
                            for line in chunk.decode(errors="ignore").splitlines():
                                if line.startswith("data:") and "[DONE]" not in line:
                                    evt = _json.loads(line[5:].strip())
                                    usage = evt.get("usage") or {}
                                    input_tokens = usage.get("prompt_tokens", input_tokens)
                                    output_tokens = usage.get("completion_tokens", output_tokens)
                        except Exception:
                            pass
            except Exception as exc:
                status = 500
                log.error("Gateway OpenAI stream error: %s", exc)
                raise
            finally:
                latency_ms = int((monotonic() - t_start) * 1000)
                background_tasks.add_task(
                    audit_service.record,
                    user_id=user.user_id,
                    model=model,
                    backend_provider=_backend_name(model),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    status_code=status,
                    latency_ms=latency_ms,
                    user_agent=user_agent,
                    request_id=request_id,
                    tool_source=user.tool_label,
                )

        return StreamingResponse(
            _stream_with_audit(),
            media_type="text/event-stream",
            headers={
                "X-Request-Id": request_id,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
    else:
        # Non-streaming: collect the whole stream and return JSON
        t_start = monotonic()
        chunks: list[bytes] = []
        try:
            async for chunk in proxy_stream(body, model, protocol="openai"):
                chunks.append(chunk)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        latency_ms = int((monotonic() - t_start) * 1000)
        # Reconstruct a minimal OpenAI-format response from the SSE stream
        import json as _json
        last_data: dict = {}
        for chunk in chunks:
            for line in chunk.decode(errors="ignore").splitlines():
                if line.startswith("data:") and "[DONE]" not in line:
                    try:
                        last_data = _json.loads(line[5:].strip())
                    except Exception:
                        pass

        background_tasks.add_task(
            audit_service.record,
            user_id=user.user_id,
            model=model,
            backend_provider=_backend_name(model),
            input_tokens=last_data.get("usage", {}).get("prompt_tokens", 0),
            output_tokens=last_data.get("usage", {}).get("completion_tokens", 0),
            status_code=200,
            latency_ms=latency_ms,
            user_agent=user_agent,
            request_id=request_id,
        )
        return JSONResponse(content=last_data or {}, headers={"X-Request-Id": request_id})


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _backend_name(model: str) -> str:
    model_l = model.lower()
    if "claude" in model_l or "anthropic" in model_l:
        s = get_settings()
        return "bedrock" if s.llm_backend == "bedrock" else "anthropic"
    if "gemini" in model_l or "google" in model_l:
        return "google"
    return "openai"

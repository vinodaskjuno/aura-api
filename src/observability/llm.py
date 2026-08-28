"""The single LLM seam for observability agents.

Every obs_* agent calls `invoke_masked()` instead of constructing
`boto3.client("bedrock-runtime")` inline (which rca_agent, sop_agent,
remediation_agent and intent_classifier all still do). This is where the masking
guarantee actually lives, because it is the only layer that knows the investigation id.

Non-streaming by design in this phase. Streaming unmasking is genuinely hard — a
token can split across SSE deltas (AURA_ arrives in one chunk, POD_01 in the next),
so per-chunk regex silently fails to restore. That needs a carry-over buffer holding
trailing bytes matching a partial-token prefix; it is deferred, not faked.

Failure mode is deliberately asymmetric:
  masking raises  -> ABORT the call. Never send raw data because a regex blew up.
  unmask raises   -> return tokens intact and flag it. A visible AURA_POD_07 is a
                     bug report; a leaked pod name is an incident.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.config_settings import calculate_cost_v2, get_settings
from src.observability.masking import MaskingError, get_session, masking_enabled, persist_session

log = logging.getLogger(__name__)

# Seam S3: the eval harness swaps in a recorded/stub callable here.
_LLM_OVERRIDE: Callable[[str, str, str, int], tuple[str, dict]] | None = None


def set_llm_override(fn: Callable[[str, str, str, int], tuple[str, dict]] | None) -> None:
    global _LLM_OVERRIDE
    _LLM_OVERRIDE = fn


@dataclass
class LLMCallRecord:
    call_id: str
    agent: str
    stage: int
    model_id: str
    backend: str
    prompt_hash: str          # sha256 of the MASKED prompt — traces stay shareable
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    stop_reason: str = ""
    masked: bool = True
    mask_session_id: str = ""
    unmask_incomplete: bool = False
    error: str | None = None
    captured_prompt: str | None = None   # only when capture_prompts / development

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# Models that accept `thinking: {type: "adaptive"}`. Older models 400 on it, and this
# model id is operator-configurable, so gate rather than assume.
_ADAPTIVE_THINKING_MODELS = (
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
)

# Server-side refusal fallbacks are an Opus-5 / Fable-5 feature.
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-mythos-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _supports(model: str, allowed: tuple[str, ...]) -> bool:
    m = (model or "").lower()
    return any(m.startswith(a) for a in allowed)


def _describe_api_error(exc: Exception) -> str:
    """Extract the provider's own message.

    `httpx.raise_for_status()` produced 'Client error 400 for url ...' and threw the
    body away, so an operator reading the log had no idea the real cause was
    'Your credit balance is too low'. The SDK's typed errors carry the parsed body.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        msg = err.get("message") or body.get("message")
        kind = err.get("type") or ""
        if msg:
            return f"{kind}: {msg}" if kind else str(msg)
    return str(exc)


def profile_prefix(region: str) -> str:
    """Cross-region inference-profile prefix for an AWS region."""
    r = (region or "").lower()
    if r.startswith("eu-"):
        return "eu."
    if r.startswith("ap-"):
        return "apac."
    return "us."


def _bedrock_hint(code: str, message: str, model_id: str, region: str) -> str:
    """Turn AWS's two most common model-id rejections into an instruction.

    Both messages describe the problem accurately but never name the fix, and the
    fix is a config value the operator has to type exactly right.
    """
    if "inference profile" in message or "on-demand throughput" in message:
        if model_id.startswith(("us.", "eu.", "apac.")):
            # Already a profile id — the prefix is not the problem. Saying "bare id"
            # here and echoing the same value back would send the operator in circles.
            return ("\n  -> That inference profile is not usable in "
                    f"{region or 'this region'}. Run "
                    "`python -m src.scripts.check_llm` to list profiles that respond.")
        suggested = profile_prefix(region) + model_id.lstrip(".")
        return (f"\n  -> '{model_id}' is a bare foundation-model id. Current Anthropic "
                f"models are only invocable on-demand through an inference profile."
                f"\n     Set BEDROCK_MODEL_ID={suggested} in src/.env and restart.")
    if "Legacy" in message:
        return ("\n  -> That model is retired. Run "
                "`python -m src.scripts.check_llm` to list model ids that work "
                "on this account, then set BEDROCK_MODEL_ID in src/.env and restart.")
    if code == "AccessDeniedException":
        return ("\n  -> The model is not enabled for this account. Run "
                "`python -m src.scripts.check_llm` to see which ones are.")
    return ""


def _invoke_bedrock(system: str, user: str, model_id: str, max_tokens: int) -> tuple[str, dict]:
    import boto3
    from botocore.exceptions import ClientError

    s = get_settings()
    client = boto3.client("bedrock-runtime", region_name=s.bedrock_region)
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    })
    # Streaming, for the same reason the Anthropic path streams: a blocking
    # invoke_model at max_tokens=16000 can exceed the client read timeout on a long
    # reasoning turn. Deltas are accumulated and returned as ONE complete string, so
    # unmasking still operates on finished text — a mask token that splits across two
    # deltas (`AURA_` / `POD_01`) would be unrecoverable if we unmasked per chunk.
    try:
        resp = client.invoke_model_with_response_stream(
            modelId=model_id, contentType="application/json",
            accept="application/json", body=body)
    except ClientError as exc:
        err = exc.response.get("Error", {})
        code = err.get("Code", "error")
        message = err.get("Message", str(exc))
        # Deliberately NOT auto-prefixing the model id here: silently rewriting a
        # configured value hides a real misconfiguration and could pick the wrong
        # region's profile. Tell the operator what to set instead.
        raise RuntimeError(
            f"Bedrock {code}: {message}"
            f"{_bedrock_hint(code, message, model_id, s.bedrock_region)}") from exc

    parts: list[str] = []
    usage: dict = {}
    stop_reason = ""
    for event in resp.get("body") or []:
        chunk = event.get("chunk")
        if not chunk:
            continue
        data = json.loads(chunk["bytes"])
        etype = data.get("type")
        if etype == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") in (None, "text_delta"):
                parts.append(delta.get("text", ""))
        elif etype == "message_delta":
            stop_reason = (data.get("delta") or {}).get("stop_reason") or stop_reason
            usage.update(data.get("usage") or {})
        elif etype == "message_start":
            usage.update((data.get("message") or {}).get("usage") or {})

    return "".join(parts), {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "stop_reason": stop_reason,
    }


def _invoke_anthropic(system: str, user: str, model_id: str, max_tokens: int) -> tuple[str, dict]:
    """Call the Messages API through the official SDK.

    Streaming with `get_final_message()` rather than a plain create: it avoids HTTP
    timeouts on long reasoning turns while still handing back the COMPLETE message.
    That matters here specifically — unmasking a stream chunk-by-chunk is unsafe,
    because a token can split across deltas (`AURA_` in one, `POD_01` in the next).
    Unmasking one finished string sidesteps that entirely.
    """
    import anthropic

    s = get_settings()
    if not s.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    kwargs: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if _supports(model_id, _ADAPTIVE_THINKING_MODELS):
        # Root-cause analysis is exactly the kind of work adaptive thinking is for.
        kwargs["thinking"] = {"type": "adaptive"}

    use_fallbacks = _supports(model_id, _FALLBACK_MODELS)

    try:
        if use_fallbacks:
            # An RCA prompt is full of infrastructure error text; a safety classifier
            # could decline it, and a silent refusal would look like a broken feature.
            with client.beta.messages.stream(
                betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
            ) as stream:
                message = stream.get_final_message()
        else:
            with client.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise RuntimeError(f"Anthropic auth failed — check ANTHROPIC_API_KEY. "
                           f"{_describe_api_error(exc)}") from exc
    except anthropic.PermissionDeniedError as exc:
        raise RuntimeError(f"Anthropic denied this request (model access or billing). "
                           f"{_describe_api_error(exc)}") from exc
    except anthropic.NotFoundError as exc:
        raise RuntimeError(f"Anthropic rejected model '{model_id}'. "
                           f"{_describe_api_error(exc)}") from exc
    except anthropic.RateLimitError as exc:
        raise RuntimeError(f"Anthropic rate limit hit. {_describe_api_error(exc)}") from exc
    except anthropic.BadRequestError as exc:
        raise RuntimeError(f"Anthropic rejected the request. "
                           f"{_describe_api_error(exc)}") from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Anthropic returned HTTP {exc.status_code}. "
                           f"{_describe_api_error(exc)}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"Could not reach the Anthropic API: {exc}") from exc

    # A refusal is HTTP 200 with empty-ish content — always check before reading it.
    if getattr(message, "stop_reason", "") == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", "") or "unspecified"
        raise RuntimeError(f"Anthropic declined this request (refusal: {category})")

    text = "".join(getattr(b, "text", "") for b in message.content
                   if getattr(b, "type", "") == "text")
    usage = message.usage
    return text, {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "stop_reason": getattr(message, "stop_reason", "") or "",
    }


async def invoke_masked(
    *,
    system: str,
    user: str,
    investigation_id: str,
    agent: str,
    stage: int = 0,
    user_id: str = "",
    max_tokens: int = 4096,
    model_id: str | None = None,
    capture_prompt: bool = False,
) -> tuple[str, LLMCallRecord]:
    """Mask -> invoke -> unmask -> meter. Returns (text, record).

    Raises MaskingError if masking fails (fail-closed). Any other provider error is
    captured in the record and returned as empty text, so one bad call degrades an
    investigation instead of killing it.
    """
    import asyncio
    import uuid

    s = get_settings()
    backend = "bedrock" if s.llm_backend == "bedrock" else "anthropic"
    model = model_id or (s.bedrock_model_id if backend == "bedrock" else s.anthropic_model_id)

    do_mask = masking_enabled() and bool(investigation_id)
    ms = get_session(investigation_id) if do_mask else None

    # ── Mask: fail closed ────────────────────────────────────────────────────
    if ms is not None:
        try:
            system_out = ms.mask(system)
            user_out = ms.mask(user)
        except MaskingError:
            log.error("Masking failed for investigation %s — aborting LLM call",
                      investigation_id)
            raise
    else:
        system_out, user_out = system, user

    record = LLMCallRecord(
        call_id=str(uuid.uuid4()),
        agent=agent, stage=stage, model_id=model, backend=backend,
        prompt_hash=_hash(system_out + "\n" + user_out),
        masked=bool(ms), mask_session_id=investigation_id if ms else "",
    )
    if capture_prompt or s.app_env == "development":
        record.captured_prompt = system_out + "\n\n---\n\n" + user_out

    t0 = time.monotonic()
    try:
        if _LLM_OVERRIDE is not None:
            text, usage = _LLM_OVERRIDE(system_out, user_out, model, max_tokens)
        elif backend == "bedrock":
            text, usage = await asyncio.get_event_loop().run_in_executor(
                None, _invoke_bedrock, system_out, user_out, model, max_tokens)
        else:
            text, usage = await asyncio.get_event_loop().run_in_executor(
                None, _invoke_anthropic, system_out, user_out, model, max_tokens)
    except Exception as exc:  # noqa: BLE001
        record.latency_ms = int((time.monotonic() - t0) * 1000)
        record.error = str(exc)
        log.warning("LLM call failed (%s/%s): %s", agent, backend, exc)
        return "", record

    record.latency_ms = int((time.monotonic() - t0) * 1000)
    record.input_tokens = usage.get("input_tokens", 0)
    record.output_tokens = usage.get("output_tokens", 0)
    record.cache_read_tokens = usage.get("cache_read_tokens", 0)
    record.stop_reason = usage.get("stop_reason", "")

    # ── Unmask: fail open ────────────────────────────────────────────────────
    if ms is not None:
        text, complete = ms.unmask(text)
        record.unmask_incomplete = not complete
        persist_session(ms)

    # ── Meter: observability spend lands in the existing dashboards for free ──
    try:
        record.cost_usd = calculate_cost_v2(
            model, input_tokens=record.input_tokens, output_tokens=record.output_tokens,
            cache_read_tokens=record.cache_read_tokens,
        )
    except Exception:  # noqa: BLE001
        record.cost_usd = 0.0

    if user_id:
        try:
            from src.services.usage_rollup import bump
            bump(user_id, datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                 tool="observability", model=model,
                 deltas={"inputTokens": record.input_tokens,
                         "outputTokens": record.output_tokens,
                         "costUsd": record.cost_usd, "calls": 1})
        except Exception as exc:  # noqa: BLE001
            log.debug("usage rollup skipped: %s", exc)

    return text, record


def parse_json_block(text: str) -> Any:
    """Extract the first JSON object/array from a model response.

    Models wrap JSON in prose and fences no matter how firmly you ask them not to.
    """
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        end = t.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except Exception:  # noqa: BLE001
                continue
    return None

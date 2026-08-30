"""Translate OTLP/JSON trace payloads into Aura traces and spans.

Reads the OpenTelemetry GenAI semantic conventions where a client follows them
(`gen_ai.*`), and falls back to the conventions the popular instrumentors emit
(`llm.*`, `traceloop.*`, OpenInference's `input.value`/`output.value`). Clients
instrument with whatever library they already use; guessing across a handful of
known attribute spellings is the price of not dictating one.

Cost is computed from token counts via the same MODEL_PRICING table the gateway
and usage rollups use, so a span's cost and a user's bill cannot disagree.
"""
from __future__ import annotations

import logging
from typing import Any

from src.aiobs.types import (
    KIND_CHAIN, KIND_LLM, KIND_RETRIEVER, KIND_TOOL, KIND_UNKNOWN, Span, Trace,
)
# Reusing the existing OTLP primitives rather than writing a second decoder: these
# already handle AnyValue, KeyValue lists and UnixNano across the metrics and logs
# receivers, and a divergent copy would drift.
from src.services.otlp_ingest import _anyval, _attrs, _ns_to_iso  # noqa: PLC2701

log = logging.getLogger(__name__)

# Attribute spellings seen in the wild, most-specific first.
_MODEL_KEYS = ("gen_ai.request.model", "gen_ai.response.model", "llm.model_name",
               "llm.request.model", "model", "ai.model.id")
_PROVIDER_KEYS = ("gen_ai.system", "llm.provider", "llm.system", "ai.model.provider")
_IN_TOK_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens",
                "llm.token_count.prompt", "llm.usage.prompt_tokens")
_OUT_TOK_KEYS = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens",
                 "llm.token_count.completion", "llm.usage.completion_tokens")
_CACHE_TOK_KEYS = ("gen_ai.usage.cache_read_input_tokens", "llm.token_count.cache_read")
_INPUT_KEYS = ("gen_ai.prompt", "input.value", "traceloop.entity.input",
               "llm.input_messages", "llm.prompts")
_OUTPUT_KEYS = ("gen_ai.completion", "output.value", "traceloop.entity.output",
                "llm.output_messages", "llm.completions")
_THREAD_KEYS = ("session.id", "gen_ai.conversation.id", "thread.id",
                "traceloop.association.properties.session_id")

# OTel SpanKind ints: 0 UNSPECIFIED, 1 INTERNAL, 2 SERVER, 3 CLIENT, 4 PRODUCER, 5 CONSUMER
_SPAN_KIND_LLM_HINTS = ("chat", "completion", "embedding", "llm", "generate")


def _first_of(attrs: dict, keys: tuple[str, ...], default: Any = "") -> Any:
    for k in keys:
        v = attrs.get(k)
        if v not in (None, ""):
            return v
    return default


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    import json
    try:
        return json.dumps(v, ensure_ascii=False)[:200_000]
    except (TypeError, ValueError):
        return str(v)


def classify(name: str, attrs: dict) -> str:
    """LLM spans are what carry cost, so misclassifying one loses the money."""
    lowered = (name or "").lower()
    if any(k in attrs for k in _MODEL_KEYS + _IN_TOK_KEYS):
        return KIND_LLM
    if any(h in lowered for h in _SPAN_KIND_LLM_HINTS):
        return KIND_LLM
    if "tool" in lowered or "function" in lowered or attrs.get("tool.name"):
        return KIND_TOOL
    if "retriev" in lowered or "vector" in lowered or "rag" in lowered:
        return KIND_RETRIEVER
    if "chain" in lowered or "agent" in lowered or "workflow" in lowered:
        return KIND_CHAIN
    return KIND_UNKNOWN


def _latency_ms(start_ns: Any, end_ns: Any) -> int:
    try:
        delta = (int(end_ns) - int(start_ns)) / 1_000_000
        return max(0, int(delta))
    except (TypeError, ValueError):
        return 0


def parse_spans(payload: dict) -> list[tuple[Span, dict]]:
    """Flatten an OTLP ExportTraceServiceRequest into spans plus resource attrs."""
    out: list[tuple[Span, dict]] = []
    for resource_span in (payload or {}).get("resourceSpans", []) or []:
        resource = _attrs((resource_span.get("resource") or {}).get("attributes"))
        for scope_span in resource_span.get("scopeSpans", []) or []:
            for raw in scope_span.get("spans", []) or []:
                span = _parse_one(raw, resource)
                if span:
                    out.append((span, resource))
    return out


def _parse_one(raw: dict, resource: dict) -> Span | None:
    trace_id = raw.get("traceId") or ""
    span_id = raw.get("spanId") or ""
    if not trace_id or not span_id:
        return None

    attrs = _attrs(raw.get("attributes"))
    merged = {**resource, **attrs}
    name = raw.get("name") or "span"
    kind = classify(name, merged)

    start_ns, end_ns = raw.get("startTimeUnixNano"), raw.get("endTimeUnixNano")
    model = str(_first_of(merged, _MODEL_KEYS) or "")
    in_tok = _as_int(_first_of(merged, _IN_TOK_KEYS, 0))
    out_tok = _as_int(_first_of(merged, _OUT_TOK_KEYS, 0))
    cache_tok = _as_int(_first_of(merged, _CACHE_TOK_KEYS, 0))

    cost = 0.0
    if kind == KIND_LLM and model and (in_tok or out_tok):
        try:
            from src.config_settings import calculate_cost_v2
            cost = calculate_cost_v2(model, input_tokens=in_tok,
                                     output_tokens=out_tok,
                                     cache_read_tokens=cache_tok)
        except Exception as exc:      # noqa: BLE001 — an unknown model must not drop the span
            log.debug("cost calc failed for %r: %s", model, exc)

    # OTLP status: 0 UNSET, 1 OK, 2 ERROR.
    status_block = raw.get("status") or {}
    is_error = status_block.get("code") in (2, "STATUS_CODE_ERROR")

    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=raw.get("parentSpanId") or "",
        name=name,
        kind=kind,
        status="error" if is_error else "ok",
        error=str(status_block.get("message") or "") if is_error else "",
        start_time=_ns_to_iso(start_ns),
        end_time=_ns_to_iso(end_ns),
        latency_ms=_latency_ms(start_ns, end_ns),
        model=model,
        provider=str(_first_of(merged, _PROVIDER_KEYS) or ""),
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_tok,
        total_tokens=in_tok + out_tok,
        cost_usd=round(cost, 6),
        input_preview=_as_text(_first_of(merged, _INPUT_KEYS)),
        output_preview=_as_text(_first_of(merged, _OUTPUT_KEYS)),
        tags={k: v for k, v in attrs.items()
              if not k.startswith(("gen_ai.", "llm.", "traceloop."))},
    )


def assemble(spans: list[Span], project_id: str, tenant_id: str,
             thread_hint: str = "") -> list[tuple[Trace, list[Span]]]:
    """Group spans into traces and denormalise the aggregates onto each trace.

    Aggregates live on the trace row so the list view renders without fanning out
    to every span — the single most common read in the product.
    """
    by_trace: dict[str, list[Span]] = {}
    for span in spans:
        by_trace.setdefault(span.trace_id, []).append(span)

    out: list[tuple[Trace, list[Span]]] = []
    for trace_id, group in by_trace.items():
        group.sort(key=lambda s: s.start_time)
        ids = {s.span_id for s in group}
        # A root is a span whose parent is absent from this batch. Spans can arrive
        # split across exports, so "no parent id" alone is too strict.
        roots = [s for s in group if not s.parent_span_id or s.parent_span_id not in ids]
        root = roots[0] if roots else group[0]

        out.append((Trace(
            trace_id=trace_id,
            project_id=project_id,
            tenant_id=tenant_id,
            thread_id=thread_hint,
            name=root.name,
            status="error" if any(s.status == "error" for s in group) else "ok",
            start_time=min(s.start_time for s in group),
            end_time=max(s.end_time for s in group),
            latency_ms=root.latency_ms or sum(s.latency_ms for s in roots),
            span_count=len(group),
            total_tokens=sum(s.total_tokens for s in group),
            cost_usd=round(sum(s.cost_usd for s in group), 6),
            input_preview=root.input_preview,
            output_preview=root.output_preview,
            tags=root.tags,
        ), group))
    return out


def thread_id_from(spans: list[Span], payload: dict) -> str:
    """A conversation id if the client set one, else empty — threads are optional."""
    for resource_span in (payload or {}).get("resourceSpans", []) or []:
        resource = _attrs((resource_span.get("resource") or {}).get("attributes"))
        for scope_span in resource_span.get("scopeSpans", []) or []:
            for raw in scope_span.get("spans", []) or []:
                merged = {**resource, **_attrs(raw.get("attributes"))}
                found = _first_of(merged, _THREAD_KEYS)
                if found:
                    return str(found)
    return ""

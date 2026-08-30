"""Trace and span records for LLM-application observability.

Deliberately close to the OpenTelemetry span model rather than a bespoke shape.
Clients instrument with a standard OTel SDK, which means the same instrumentation
can be pointed at Opik or any other OTel backend instead of Aura — the build/buy
decision stays a configuration choice rather than a rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# OTLP span kinds we care about. Anything else is recorded verbatim.
KIND_LLM = "llm"
KIND_TOOL = "tool"
KIND_RETRIEVER = "retriever"
KIND_CHAIN = "chain"
KIND_UNKNOWN = "unknown"

# Payloads larger than this are offloaded to S3 and replaced with a pointer plus a
# preview. DynamoDB's hard item limit is 400 KB and a trace row carries more than
# just the payload, so the threshold sits well below it.
MAX_INLINE_PAYLOAD = 8 * 1024


@dataclass
class Span:
    """One unit of work inside a trace.

    `parent_span_id` is what makes the waterfall a tree; a root span has none.
    """
    trace_id: str
    span_id: str
    name: str
    start_time: str
    end_time: str
    parent_span_id: str = ""
    kind: str = KIND_UNKNOWN
    status: str = "ok"                 # ok | error
    error: str = ""
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    input_preview: str = ""
    output_preview: str = ""
    input_ref: str = ""                # S3 key when the payload was offloaded
    output_ref: str = ""
    tags: dict[str, Any] = field(default_factory=dict)

    def as_item(self) -> dict:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "startTime": self.start_time,
            "endTime": self.end_time,
            "latencyMs": self.latency_ms,
            "model": self.model,
            "provider": self.provider,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "totalTokens": self.total_tokens,
            "costUsd": self.cost_usd,
            "inputPreview": self.input_preview,
            "outputPreview": self.output_preview,
            "inputRef": self.input_ref,
            "outputRef": self.output_ref,
            "tags": self.tags,
        }


@dataclass
class Trace:
    """A whole request. Aggregates are denormalised onto the trace row because the
    list view must not fan out to every span to render a row."""
    trace_id: str
    project_id: str
    tenant_id: str
    name: str
    start_time: str
    end_time: str
    status: str = "ok"
    thread_id: str = ""
    span_count: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    input_preview: str = ""
    output_preview: str = ""
    tags: dict[str, Any] = field(default_factory=dict)

    def as_item(self) -> dict:
        return {
            # (projectId, <startTime>#<traceId>) sorts newest-last within a project,
            # which is what the list view pages backwards over.
            "projectId": self.project_id,
            "sortKey": f"{self.start_time}#{self.trace_id}",
            "traceId": self.trace_id,
            "tenantId": self.tenant_id,
            "threadId": self.thread_id,
            "name": self.name,
            "status": self.status,
            "startTime": self.start_time,
            "endTime": self.end_time,
            "latencyMs": self.latency_ms,
            "spanCount": self.span_count,
            "totalTokens": self.total_tokens,
            "costUsd": self.cost_usd,
            "inputPreview": self.input_preview,
            "outputPreview": self.output_preview,
            "tags": self.tags,
        }

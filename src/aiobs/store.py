"""Storage seam for traces and spans.

DynamoDB is a key-value store: every access pattern needs an index planned up
front, and anything else degrades to a scan. That is fine for fetching one trace,
listing a project newest-first, and filtering on a few pre-indexed dimensions —
and it is NOT enough for ad-hoc tag filtering or free-text search across millions
of spans, which is why Opik uses ClickHouse.

Rather than pretend otherwise, every caller goes through this protocol. A
deployment that outgrows DynamoDB gains a ClickHouse or OpenSearch implementation
without a single call site changing — the same shape as src/graph/dialects/, and
for the same reason: the storage engine is a deployment choice, not a code
dependency.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.aiobs.types import Span, Trace


@runtime_checkable
class TraceStore(Protocol):
    def write_trace(self, trace: Trace, spans: list[Span]) -> None:
        """Persist a trace and its spans. MUST be idempotent: OTLP exporters retry,
        so the same trace can arrive more than once."""
        ...

    def get_trace(self, project_id: str, trace_id: str) -> dict | None: ...

    def get_spans(self, trace_id: str, limit: int = 1000) -> list[dict]: ...

    def list_traces(self, project_id: str, limit: int = 50,
                    status: str = "", thread_id: str = "") -> list[dict]:
        """Newest first. `status`/`thread_id` are the only filters guaranteed to be
        index-backed; anything richer belongs in an analytics implementation."""
        ...

    def list_threads(self, project_id: str, limit: int = 50) -> list[dict]: ...

    def capabilities(self) -> dict:
        """What this implementation can actually do, so the UI can hide controls it
        cannot honour instead of offering a filter that silently scans."""
        ...

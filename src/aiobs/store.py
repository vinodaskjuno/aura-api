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
                    status: str = "", thread_id: str = "",
                    search: str = "", tenant_id: str = "") -> list[dict]:
        """Newest first.

        `status`/`thread_id` are the only filters every implementation can serve.
        `search` (free text over inputs/outputs) and `tenant_id` are OPTIONAL: an
        implementation that cannot index them must ignore them and say so through
        `capabilities()`, never silently turn them into a scan. The DynamoDB store
        ignores both; the Opik store pushes them down to ClickHouse.
        """
        ...

    def list_threads(self, project_id: str, limit: int = 50) -> list[dict]: ...

    def list_projects(self, tenant_id: str = "") -> list[dict]:
        """Projects that have received traces, newest activity first.

        Behind the protocol because "which projects exist" is engine-specific: Opik
        has a first-class projects API, while DynamoDB can only derive the answer.
        It used to be a `scan_items("ai-traces", limit=2000)` in the router with no
        tenant predicate — a cross-tenant enumeration of prompts dressed up as a
        list. `tenant_id` narrows it; an implementation that cannot index tenants
        must return only what the caller may see or nothing at all.
        """
        ...

    def record_scores(self, project_id: str, trace: dict, scores: list) -> bool:
        """Attach evaluation scores to a trace. Returns whether they were stored.

        Here rather than in `online_eval` because where a score LIVES is a property
        of the engine: DynamoDB stamps a JSON blob onto the trace row, while Opik has
        first-class feedback scores that are filterable and sortable. Keeping this
        behind the protocol is what stopped the online-eval sweep from silently
        writing to DynamoDB after the read path had already moved to Opik.
        """
        ...

    def capabilities(self) -> dict:
        """What this implementation can actually do, so the UI can hide controls it
        cannot honour instead of offering a filter that silently scans."""
        ...

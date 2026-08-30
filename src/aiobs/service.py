"""Ingest orchestration: parse -> offload payloads -> persist.

Kept apart from the router so the store can be swapped and the flow unit-tested
without FastAPI, and apart from `ingest` so parsing stays a pure function.
"""
from __future__ import annotations

import logging

from src.aiobs import ingest
from src.aiobs.dynamo_store import DynamoTraceStore, store_payload
from src.aiobs.store import TraceStore
from src.aiobs.types import Span

log = logging.getLogger(__name__)

DEFAULT_PROJECT = "default"

_store: TraceStore | None = None


def get_store() -> TraceStore:
    """The active store. A deployment that outgrows DynamoDB swaps this one line."""
    global _store
    if _store is None:
        _store = DynamoTraceStore()
    return _store


def set_store(store: TraceStore | None) -> None:
    """Test seam, mirroring observability.llm.set_llm_override."""
    global _store
    _store = store


def project_of(payload: dict) -> str:
    """Project comes from the client's own resource attributes.

    OTel already has `service.name` for this and every SDK sets it, so a client
    gets sensible grouping with no Aura-specific configuration.
    """
    from src.services.otlp_ingest import _attrs  # noqa: PLC2701
    for resource_span in (payload or {}).get("resourceSpans", []) or []:
        resource = _attrs((resource_span.get("resource") or {}).get("attributes"))
        for key in ("aura.project", "service.name", "service.namespace"):
            value = resource.get(key)
            if value:
                return str(value)[:120]
    return DEFAULT_PROJECT


def store_batch(spans: list[Span], payload: dict, tenant_id: str,
                thread_id: str = "") -> int:
    """Persist one export. Returns the number of spans stored."""
    project_id = project_of(payload)
    store = get_store()
    stored = 0

    for trace, group in ingest.assemble(spans, project_id, tenant_id, thread_id):
        for span in group:
            # Offload before writing: a large prompt would otherwise blow the
            # DynamoDB item limit and lose the whole span.
            span.input_preview, span.input_ref = store_payload(
                project_id, span.trace_id, span.span_id, "in", span.input_preview)
            span.output_preview, span.output_ref = store_payload(
                project_id, span.trace_id, span.span_id, "out", span.output_preview)
        try:
            store.write_trace(trace, group)
            stored += len(group)
        except Exception as exc:  # noqa: BLE001 — one bad trace must not drop the batch
            log.warning("trace %s not stored: %s", trace.trace_id, exc)
    return stored

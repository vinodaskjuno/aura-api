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
    """The active store, chosen by `settings.aiobs_store`.

    This is the swap the TraceStore protocol was written for. It is configuration
    rather than a code change specifically so the migration to Opik is reversible
    at runtime: if Opik misbehaves, set aiobs_store=dynamodb and the read path is
    back on DynamoDB without a deploy.

    An unknown value falls back to DynamoDB with a warning rather than raising —
    a typo in an env var must not take the whole API down.
    """
    global _store
    if _store is None:
        from src.config_settings import get_settings
        choice = (get_settings().aiobs_store or "dynamodb").strip().lower()
        if choice == "opik":
            from src.aiobs.opik_store import OpikTraceStore
            _store = OpikTraceStore()
        else:
            if choice != "dynamodb":
                log.warning("unknown aiobs_store %r; falling back to dynamodb", choice)
            _store = DynamoTraceStore()
        log.info("aiobs trace store: %s", getattr(_store, "name", "?"))
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
    mirror = _mirror_store(store)
    stored = 0

    for trace, group in ingest.assemble(spans, project_id, tenant_id, thread_id):
        for span in group:
            # Offload before writing: a large prompt would otherwise blow the
            # DynamoDB item limit and lose the whole span. Harmless under Opik,
            # which stores payloads itself — the preview is simply the full text.
            span.input_preview, span.input_ref = store_payload(
                project_id, span.trace_id, span.span_id, "in", span.input_preview)
            span.output_preview, span.output_ref = store_payload(
                project_id, span.trace_id, span.span_id, "out", span.output_preview)
        try:
            store.write_trace(trace, group)
            stored += len(group)
        except Exception as exc:  # noqa: BLE001 — one bad trace must not drop the batch
            log.warning("trace %s not stored: %s", trace.trace_id, exc)

        if mirror is not None:
            # Dual-write. Failures here are DEBUG, never warnings: the mirror is by
            # definition the store nobody is reading yet, so a problem with it must
            # not fill the logs of a healthy ingest path.
            try:
                mirror.write_trace(trace, group)
            except Exception as exc:  # noqa: BLE001
                log.debug("mirror write skipped for %s: %s", trace.trace_id, exc)

    return stored


def _mirror_store(active: TraceStore) -> TraceStore | None:
    """The secondary store to dual-write to during a migration, or None.

    Exists so Opik can be filled with real traffic and compared against DynamoDB
    BEFORE `aiobs_store` is flipped to read from it. Deliberately one-directional:
    whichever store is active is the source of truth, and the other only receives.
    """
    from src.config_settings import get_settings
    s = get_settings()
    if not s.aiobs_forward_otlp:
        return None
    try:
        if getattr(active, "name", "") == "opik":
            return DynamoTraceStore()
        from src.aiobs.opik_client import enabled
        if not enabled():
            return None
        from src.aiobs.opik_store import OpikTraceStore
        return OpikTraceStore()
    except Exception as exc:  # noqa: BLE001
        log.debug("mirror store unavailable: %s", exc)
        return None

"""OTLP/HTTP receiver — ingests Claude Code's native OpenTelemetry export.

Claude Code emits token and cost telemetry itself when CLAUDE_CODE_ENABLE_TELEMETRY=1.
Pointing it here captures per-model usage without sitting in the request path, so
AURA can never break a Claude Code chat the way proxying it did.

Client configuration (written by the AURA plugin into ~/.claude/settings.json,
which covers both the CLI and the VS Code extension):

    CLAUDE_CODE_ENABLE_TELEMETRY=1
    OTEL_METRICS_EXPORTER=otlp
    OTEL_LOGS_EXPORTER=otlp
    OTEL_EXPORTER_OTLP_PROTOCOL=http/json
    OTEL_EXPORTER_OTLP_ENDPOINT=https://<host>/otlp
    OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer gw-<virtual key>

OTLP/HTTP appends the signal path to the configured endpoint, which is why the
routes below are /v1/metrics and /v1/logs under the /otlp prefix.

We speak http/json rather than http/protobuf deliberately: the payload is then
plain JSON that FastAPI parses natively, with no protobuf dependency added to a
codebase that has none.

CONTRACT: these endpoints ALWAYS return 200. A non-2xx makes the exporter retry
in a loop and surfaces telemetry errors inside the user's Claude Code session —
exactly the failure mode this design exists to avoid. Ingest problems are logged
and reported in the response body via OTLP's partialSuccess field.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.config_settings import get_settings
from src.services import otlp_ingest, usage_rollup
from src.services.gateway_service import extract_credential, resolve_credential

log = logging.getLogger(__name__)
router = APIRouter(prefix="/otlp", tags=["otlp"])

# OTLP's success body. Callers treat a populated rejected_* count as a partial failure.
_OK: dict = {"partialSuccess": {}}


def _accepted(**extra) -> JSONResponse:
    return JSONResponse(status_code=200, content={**_OK, **extra})


def _partial(rejected: int, reason: str, kind: str = "dataPoints") -> JSONResponse:
    log.warning("otlp: rejected %d %s: %s", rejected, kind, reason)
    return JSONResponse(
        status_code=200,
        content={"partialSuccess": {f"rejected{kind[0].upper()}{kind[1:]}": rejected,
                                    "errorMessage": reason}},
    )


def _decode_protobuf(body: bytes) -> tuple[dict | None, str]:
    """Decode an OTLP/protobuf trace export into the same dict shape as OTLP/JSON.

    `preserving_proto_field_name=False` gives camelCase keys (traceId, spanId,
    startTimeUnixNano), which is exactly what the JSON encoding produces — so one
    parser serves both wire formats.

    Byte fields (trace and span ids) arrive base64-encoded from MessageToDict; they
    are converted to hex, because hex is what every OTel UI and the JSON exporter
    show, and an id that renders differently depending on the wire format is a bug
    waiting to be filed.
    """
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    except ImportError:
        return None, ("OTLP/protobuf received but opentelemetry-proto is not "
                      "installed on this server")
    try:
        req = trace_service_pb2.ExportTraceServiceRequest()
        req.ParseFromString(body)
        decoded = MessageToDict(req, preserving_proto_field_name=False)
    except Exception as exc:  # noqa: BLE001
        return None, f"Malformed protobuf payload: {exc}"
    _hexify_ids(decoded)
    return decoded, ""


def _hexify_ids(payload: dict) -> None:
    """Rewrite base64 trace/span ids to hex, in place."""
    import base64

    def to_hex(value: str) -> str:
        try:
            return base64.b64decode(value).hex()
        except Exception:  # noqa: BLE001 — already hex, or unusable; leave as-is
            return value

    for resource_span in payload.get("resourceSpans", []) or []:
        for scope_span in resource_span.get("scopeSpans", []) or []:
            for span in scope_span.get("spans", []) or []:
                for key in ("traceId", "spanId", "parentSpanId"):
                    if span.get(key):
                        span[key] = to_hex(span[key])


async def _read_payload(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Validate size / content type / auth. Returns (payload, error_response)."""
    s = get_settings()

    if not s.otlp_enabled:
        return None, _partial(0, "OTLP ingest is disabled on this server")

    content_type = request.headers.get("content-type", "")

    body = await request.body()
    if len(body) > s.otlp_max_body_bytes:
        return None, _partial(
            0, f"Payload {len(body)} bytes exceeds limit {s.otlp_max_body_bytes}"
        )

    if "protobuf" in content_type:
        # This receiver was JSON-only, because Claude Code speaks http/json and
        # avoiding protobuf kept a dependency out of the codebase. That trade no
        # longer holds: the Python OTLP HTTP exporter has NO JSON mode, so every
        # Python LLM application — which is most of them — could not send traces
        # at all. Decoding protobuf here is what keeps clients on a standard OTel
        # SDK instead of an Aura-specific one.
        payload, err = _decode_protobuf(body)
        if err:
            return None, _partial(0, err)
        return payload, None

    try:
        import json
        payload = json.loads(body or b"{}")
    except Exception as exc:
        return None, _partial(0, f"Malformed JSON payload: {exc}")

    if not isinstance(payload, dict):
        return None, _partial(0, "Payload must be a JSON object")

    return payload, None


def _resolve_user(request: Request) -> str | None:
    """Resolve the caller to a userId, or None when the credential is unusable.

    Auth failure is logged and swallowed rather than returned as 401: a retry
    loop against a revoked key would spam the user's Claude Code session with
    telemetry errors. The data is dropped; the session stays clean.
    """
    try:
        user = resolve_credential(extract_credential(request))
    except Exception as exc:
        log.warning("otlp: unauthenticated export rejected (%s)", exc)
        return None
    if not user.user_id:
        log.warning("otlp: credential resolved to an empty userId")
        return None
    return user.user_id


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/v1/metrics")
async def receive_metrics(request: Request):
    """Ingest claude_code.* metrics.

    Metrics are a CROSS-CHECK, never usage. Claude Code reports the same API
    call through both the api_request event and these metrics, so counting both
    would double every token and dollar figure. Events are authoritative (they
    carry per-request identity and the 1h/5m cache split); metric totals land in
    a separate reconciliation namespace used to detect dropped events.
    """
    payload, err = await _read_payload(request)
    if err is not None:
        return err

    user_id = _resolve_user(request)
    if user_id is None:
        return _accepted()

    try:
        records = otlp_ingest.parse_metrics(payload)
        summary = otlp_ingest.summarize_metrics(payload)
    except Exception as exc:
        log.exception("otlp: metrics parse failed")
        return _partial(1, f"Parse error: {exc}")

    # Reconciliation only -- NOT usage. Claude Code reports every API call
    # through both signals, so recording metrics as usage doubles the numbers.
    for rec in records:
        try:
            usage_rollup.record_reconciliation(user_id, rec)
        except Exception as exc:
            log.warning("otlp: metric reconciliation failed: %s", exc)

    log.info(
        "otlp/metrics: user=%s points=%d sessions=%d activeSec=%.1f (reconciliation only)",
        user_id, len(records),
        summary.get("sessions", 0), summary.get("activeSeconds", 0.0),
    )
    return _accepted()


# ─────────────────────────────────────────────────────────────────────────────
# Logs / events  (primary usage source)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/v1/logs")
async def receive_logs(request: Request):
    """Ingest claude_code.api_request / api_error events — the usage source of record.

    Each api_request event is one upstream API call and carries the model, all
    four token counts, Claude Code's own cost_usd, duration_ms, and a stable
    request_id used for deduplication.
    """
    payload, err = await _read_payload(request)
    if err is not None:
        return err

    user_id = _resolve_user(request)
    if user_id is None:
        return _accepted()

    try:
        records = otlp_ingest.parse_logs(payload)
    except Exception as exc:
        log.exception("otlp: logs parse failed")
        return _partial(1, f"Parse error: {exc}", kind="logRecords")

    recorded = 0
    for rec in records:
        try:
            if usage_rollup.record_usage(user_id, rec, source="claude-code"):
                recorded += 1
        except Exception as exc:
            log.warning("otlp: log record failed: %s", exc)

    if records:
        log.info(
            "otlp/logs: user=%s events=%d recorded=%d models=%s",
            user_id, len(records), recorded,
            sorted({r.model for r in records}),
        )
    return _accepted()


# ─────────────────────────────────────────────────────────────────────────────
# Traces — accepted and discarded
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/v1/traces")
async def receive_traces(request: Request):
    """Ingest LLM-application traces from any OpenTelemetry SDK.

    This used to accept and drop. It now persists, which is what makes the AI
    Observability surface possible — and because the wire format is standard OTLP,
    the same client instrumentation can be repointed at Opik or any other OTel
    backend without touching the application.

    The always-200 contract is kept deliberately. A non-2xx makes exporters retry
    in a loop and surfaces telemetry errors inside the caller's own application;
    an observability tool must never be able to break the thing it observes.
    Ingest problems are reported through OTLP's partialSuccess field instead.
    """
    payload, err = await _read_payload(request)
    if err is not None:
        return err

    user_id = _resolve_user(request)
    if not user_id:
        # Unauthenticated traces are dropped rather than stored against a guessed
        # tenant: this store holds one client's prompts, and mis-attributing them
        # is worse than losing them.
        return _partial(0, "unauthenticated: send a gateway key or JWT", kind="spans")

    try:
        from src.aiobs import ingest, service
        parsed = ingest.parse_spans(payload)
        if not parsed:
            return _accepted()
        spans = [span for span, _ in parsed]
        thread_id = ingest.thread_id_from(spans, payload)
        stored = service.store_batch(spans, payload, user_id, thread_id)
        return _accepted(stored=stored)
    except Exception as exc:  # noqa: BLE001 — never fail the caller's exporter
        log.exception("trace ingest failed")
        return _partial(0, f"ingest error: {exc}", kind="spans")

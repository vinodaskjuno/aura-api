"""The seam to the self-hosted Opik backend.

Two absolute rules, both learned from `routers/otlp.py`:

  1. NEVER raise into a caller. An observability tool that can break the thing it
     observes is worse than no observability tool. Every public function here
     swallows and logs. `emit_llm_span` returns None on any failure and the agent
     that called it never knows.
  2. NEVER send unmasked text. Aura's masking guarantee (observability/llm.py)
     is fail-closed on mask; a span carrying raw identifiers would route around it
     entirely. Callers pass already-masked payloads, and `_redact` is a second
     belt for the obvious cases.

Deliberately talks REST over httpx rather than using the `opik` SDK client for
span emission. The SDK is installed (it is what customers instrument WITH, and
what `opik_store` mirrors the shape of), but for Aura's own agents it brings a
background-thread batcher, a global singleton and its own retry policy — three
things that make failures asynchronous and hard to attribute. A short explicit
POST that we control the timeout of is easier to reason about, and httpx is
already a pinned dependency.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Opik keys everything on UUIDv7 and mints its own ids for OTLP-ingested traces
# (verified against 2.2.46: a 128-bit OTel hex id is NOT preserved). Aura therefore
# generates ids it controls and carries any foreign id in metadata instead.
#
# NO DOTS IN THIS NAME. Opik's metadata filter is a DICTIONARY field whose `key` is
# read as a JSON path, so "aura.otel_trace_id" is looked up as nested
# {"aura": {"otel_trace_id": ...}} and matches nothing — a filter that returns zero
# rows rather than erroring, which is the worst possible failure mode. Verified
# against a live 2.2.46: the dotted key matched 0, "tenantId" matched 1.
_OTEL_ID_KEY = "auraOtelTraceId"

# Payloads are already previews by the time they reach a span, but a runaway prompt
# should not become a multi-megabyte HTTP body on the agent's critical path.
_MAX_PAYLOAD_CHARS = 8_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid7() -> str:
    """A fresh time-ordered id Opik will accept.

    Opik REJECTS anything that is not a version 7 UUID on its write path —
    `{"code":400,"message":"Invalid UUID for id","details":"Trace id must be a
    version 7 UUID"}`, verified against 2.2.46. This is unconditional and unrelated
    to the UUID_VALIDATION_ENABLED setting, which governs the time window, not the
    version. uuid6 ships with the opik SDK; the fallback constructs one by hand
    rather than returning a uuid4 that the server would refuse.
    """
    try:
        import uuid6
        return str(uuid6.uuid7())
    except Exception:  # noqa: BLE001
        import time
        return deterministic_uuid7(int(time.time() * 1000), uuid.uuid4().hex)


def deterministic_uuid7(ts_ms: int, seed: str) -> str:
    """A UUIDv7 that is stable for the same (timestamp, seed).

    This is what lets Aura keep its own ids. Opik demands version 7, and a raw OTel
    trace id is not one — so instead of storing a mapping table, the id is DERIVED:
    the 48-bit timestamp comes from the trace's real start time (which also gives
    ClickHouse the time ordering its ORDER BY assumes) and the random bits come from
    a hash of the OTel id.

    Two consequences, both wanted:
      * `write_trace` is idempotent, because a retried OTLP export produces byte-identical
        ids and Opik answers the second POST with 409 "Trace already exists".
      * `get_trace(otel_id)` round-trips, because the id can be recomputed rather
        than looked up.

    Layout is RFC 9562 §5.7: 48 bits unix_ts_ms | 4 bits version=7 | 12 bits rand_a
    | 2 bits variant=0b10 | 62 bits rand_b.
    """
    import hashlib

    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    rand_a = int.from_bytes(digest[0:2], "big") & 0x0FFF
    rand_b = int.from_bytes(digest[2:10], "big") & ((1 << 62) - 1)
    value = (max(0, int(ts_ms)) & ((1 << 48) - 1)) << 80
    value |= 7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return str(uuid.UUID(int=value))


def write(path: str, body: Any, method: str = "POST") -> bool:
    """Idempotent write to Opik. Returns whether the data is now stored.

    OTLP exporters retry, and the TraceStore protocol requires `write_trace` to be
    idempotent. Because ids are derived deterministically a retry is a genuine
    duplicate, so Opik's 409 is the correct answer rather than an error to log
    loudly on every retry.

    `method` exists because Opik is not uniform: traces and spans are created with
    POST, but feedback scores are attached with **PUT** (`@PUT /feedback-scores`,
    204 No Content). Using POST there returns 405 Method Not Allowed — which cost a
    silent "scores not persisted" until it was tried against a live server.
    """
    from src.config_settings import get_settings
    try:
        import httpx
    except ImportError:      # pragma: no cover
        return False

    url = f"{_base_url()}/{path.lstrip('/')}"
    try:
        with httpx.Client(timeout=get_settings().opik_timeout_seconds) as client:
            resp = client.request(method, url, json=body, headers=_headers())
    except Exception as exc:  # noqa: BLE001
        log.warning("opik %s %s failed: %s", method, path, exc)
        return False

    if resp.status_code == 409:
        return True                    # already stored: an earlier attempt won
    if resp.status_code >= 400:
        log.warning("opik %s %s -> HTTP %s %s", method, path, resp.status_code,
                    resp.text[:200])
        return False
    return True


def post_idempotent(path: str, body: Any) -> bool:
    """POST alias for `write`, kept because it reads better at the call sites."""
    return write(path, body, method="POST")


def _redact(text: str) -> str:
    """Belt-and-braces guard, not the masking implementation.

    src/observability/masking.py is the real thing and runs before this. This only
    catches the case where a caller forgot, by refusing to pass through anything
    still holding an unmasked-looking AWS key or bearer token. It truncates rather
    than raising, because rule 1 above outranks completeness.
    """
    import re
    out = (text or "")[:_MAX_PAYLOAD_CHARS]
    out = re.sub(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b", "<redacted:aws-key>", out)
    out = re.sub(r"\b(gw-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+", r"\1<redacted>", out)
    out = re.sub(r"\bBearer\s+[A-Za-z0-9._-]{12,}", "Bearer <redacted>", out)
    return out


def enabled() -> bool:
    from src.config_settings import get_settings
    try:
        return bool(get_settings().opik_enabled)
    except Exception:  # noqa: BLE001
        return False


def _base_url() -> str:
    from src.config_settings import get_settings
    return get_settings().opik_url.rstrip("/")


def _headers() -> dict[str, str]:
    """Opik's workspace/auth headers.

    `Comet-Workspace` is read even when authentication is disabled — the request
    context populates workspaceId from it — so it is always sent. `Authorization`
    carries no `Bearer ` prefix, which is Opik's documented (and unusual) contract.
    """
    from src.config_settings import get_settings
    s = get_settings()
    h = {"Content-Type": "application/json",
         "Accept": "application/json",
         "Comet-Workspace": s.opik_workspace}
    if s.opik_api_key:
        h["Authorization"] = s.opik_api_key
    return h


def request(method: str, path: str, *, json_body: Any = None,
            params: dict | None = None) -> Any:
    """One HTTP call to Opik. Returns parsed JSON, or None on any failure.

    Shared by `opik_client` and `opik_store` so there is a single place where the
    timeout, the headers and the never-raise contract live.
    """
    from src.config_settings import get_settings
    try:
        import httpx
    except ImportError:          # pragma: no cover — httpx is pinned
        log.warning("httpx unavailable; Opik disabled for this process")
        return None

    url = f"{_base_url()}/{path.lstrip('/')}"
    try:
        with httpx.Client(timeout=get_settings().opik_timeout_seconds) as client:
            resp = client.request(method, url, json=json_body, params=params,
                                  headers=_headers())
    except Exception as exc:  # noqa: BLE001 — Opik being down must never propagate
        log.warning("opik %s %s failed: %s", method, path, exc)
        return None

    if resp.status_code >= 400:
        # Body is truncated: Opik echoes request content in validation errors, and
        # that content is prompt text.
        log.warning("opik %s %s -> HTTP %s %s", method, path, resp.status_code,
                    resp.text[:200])
        return None
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def health() -> bool:
    """Whether Opik is reachable. Used by the capabilities endpoint so the UI can
    say "degraded" instead of rendering an empty list as though it were no data."""
    return request("GET", "v1/private/projects", params={"page": 1, "size": 1}) is not None


# ── Span emission for Aura's own agents ─────────────────────────────────────────

def emit_llm_span(*, project: str, agent: str, model: str, provider: str,
                  masked_input: str, masked_output: str,
                  input_tokens: int = 0, output_tokens: int = 0,
                  total_tokens: int = 0, cost_usd: float = 0.0,
                  latency_ms: int = 0, error: str = "",
                  trace_id: str = "", parent_span_id: str = "",
                  thread_id: str = "", metadata: dict | None = None) -> dict | None:
    """Record one LLM call as an Opik trace+span pair.

    Returns {"traceId", "spanId"} so a caller can thread children underneath, or
    None if Opik is off or unreachable. A None return is not an error condition and
    callers must treat it as "no tracing this time".

    `masked_input`/`masked_output` MUST already be masked — see rule 2 in the module
    docstring. This function cannot verify that, only refuse the obvious leaks.
    """
    if not enabled():
        return None

    start = _now()
    span_id = _uuid7()
    # Opik has no notion of "attach to an existing trace by foreign id", so a caller
    # continuing a trace passes the Opik trace id it got back from the first call.
    own_trace = not trace_id
    trace_id = trace_id or _uuid7()

    meta = {"agent": agent, "backend": provider, **(metadata or {})}
    usage = {"prompt_tokens": int(input_tokens or 0),
             "completion_tokens": int(output_tokens or 0),
             "total_tokens": int(total_tokens or (input_tokens or 0) + (output_tokens or 0))}

    if own_trace:
        # The trace row is what the list view renders, so it carries the same
        # payloads as the span rather than being an empty shell.
        body = {"id": trace_id, "project_name": project, "name": agent,
                "start_time": start, "end_time": _now(),
                "input": {"prompt": _redact(masked_input)},
                "output": {"completion": _redact(masked_output)},
                "metadata": meta, "tags": ["aura", agent]}
        if thread_id:
            body["thread_id"] = thread_id
        if error:
            body["error_info"] = {"exception_type": "LLMError", "message": error[:500]}
        if request("POST", "v1/private/traces", json_body=body) is None:
            return None

    span = {"id": span_id, "trace_id": trace_id, "project_name": project,
            "name": f"{agent}:{model}", "type": "llm",
            "start_time": start, "end_time": _now(),
            "input": {"prompt": _redact(masked_input)},
            "output": {"completion": _redact(masked_output)},
            "metadata": {**meta, "latency_ms": int(latency_ms or 0)},
            "model": model, "provider": provider,
            "usage": usage,
            # Opik computes its own cost from its bundled price table. Aura's
            # calculate_cost_v2 is the billing figure (it is the same table the
            # gateway and invoices use), so it is sent explicitly rather than left
            # for Opik to infer and disagree about.
            "total_estimated_cost": round(float(cost_usd or 0.0), 10)}
    if parent_span_id:
        span["parent_span_id"] = parent_span_id
    if error:
        span["error_info"] = {"exception_type": "LLMError", "message": error[:500]}

    if request("POST", "v1/private/spans", json_body=span) is None:
        # The trace exists but the span does not. Better than nothing: the trace row
        # already carries the payloads and cost.
        return {"traceId": trace_id, "spanId": ""}

    return {"traceId": trace_id, "spanId": span_id}

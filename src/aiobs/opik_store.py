"""Opik implementation of the TraceStore protocol.

This is the whole point of the protocol in `store.py`: Aura's router, UI and tests
keep working unchanged while the engine underneath becomes ClickHouse-backed Opik.
The swap is one line in `service.py::get_store`.

Talks Opik's REST API over httpx rather than embedding the `opik` SDK client. The
SDK is for INSTRUMENTATION (customers use it, and `opik_client` emits Aura's own
spans); for a read path that must map field-by-field onto Aura's row shape, explicit
requests are clearer and their timeouts are ours to control.

Three things about Opik that shape the code below, all verified against a running
2.2.46 rather than read off the Java source:

  * The wire format is snake_case (`start_time`, `total_estimated_cost`,
    `parent_span_id`), which does NOT match the camelCase field names in Trace.java.
  * Trace and span ids MUST be version 7 UUIDs — the write path answers anything
    else with 400 "Trace id must be a version 7 UUID". Aura's ids come from client
    OTel SDKs as 128-bit hex, which are not.
    `opik_client.deterministic_uuid7` derives a stable v7 id from
    (start_time, otel_id) so writes stay idempotent and reads can recompute the id
    instead of consulting a mapping table.
  * `usage` is a map, not a scalar, and `duration` is fractional milliseconds.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from src.aiobs import opik_client
from src.aiobs.types import Span, Trace

log = logging.getLogger(__name__)

# Opik pages; Aura's callers ask for a flat "newest first" list. Cap the page size so
# a limit=200 request cannot become an unbounded ClickHouse scan.
_MAX_PAGE = 500

# Newest-first, matching the ordering DynamoTraceStore got from its sortKey.
_SORT_NEWEST = json.dumps([{"field": "start_time", "direction": "DESC"}])

# Where the originating OTel trace id is kept, so a customer's own logs and any
# existing Aura deep link still correlate after the id is rewritten.
_OTEL_KEY = opik_client._OTEL_ID_KEY          # noqa: SLF001


def _is_uuid(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4


# ── Mapping helpers ────────────────────────────────────────────────────────────

def _ms(value: Any) -> int:
    """Opik reports `duration` as fractional milliseconds; Aura's rows are ints."""
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _text(value: Any, limit: int = 512) -> str:
    """Flatten an Opik JSON payload into the preview string Aura rows carry.

    Opik stores `input`/`output` as arbitrary JSON. Aura's contract is a short
    string, so a dict with one obvious text field is unwrapped and anything else is
    serialised — losing structure rather than losing the preview entirely.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        for key in ("preview", "prompt", "input", "question", "text",
                    "completion", "output", "answer"):
            inner = value.get(key)
            if isinstance(inner, str) and inner:
                return inner[:limit]
    try:
        return json.dumps(value)[:limit]
    except (TypeError, ValueError):
        return str(value)[:limit]


def _usage_total(usage: Any) -> int:
    """Total tokens out of Opik's usage map.

    Prefers the explicit total and falls back to prompt+completion, because the OTLP
    path populates `total_tokens` while some SDK integrations do not.
    """
    if not isinstance(usage, dict):
        return 0
    if usage.get("total_tokens") is not None:
        try:
            return int(usage["total_tokens"])
        except (TypeError, ValueError):
            pass
    total = 0
    for key in ("prompt_tokens", "completion_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _tags_of(row: dict) -> dict:
    """Aura's `tags` is a dict; Opik splits labels (`tags`, a list) from structured
    data (`metadata`). Merge both back, with metadata authoritative."""
    out: dict[str, Any] = {}
    meta = row.get("metadata")
    if isinstance(meta, dict):
        out.update(meta)
    labels = row.get("tags")
    if isinstance(labels, list) and labels:
        out["labels"] = labels
    return out


def _trace_row(row: dict, project_id: str) -> dict:
    """One Opik trace -> one Aura trace row (`types.Trace.as_item()` shape)."""
    start = row.get("start_time", "") or ""
    trace_id = row.get("id", "") or ""
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    scores = [s for s in (row.get("feedback_scores") or []) if isinstance(s, dict)]
    return {
        "projectId": project_id or row.get("project_name", "") or "",
        # Kept for shape compatibility: the UI sorts on it and online-eval read it.
        "sortKey": f"{start}#{trace_id}",
        "traceId": trace_id,
        # Opik's internal project UUID, present on a direct id fetch (which returns
        # project_id but not project_name). get_spans falls back to it.
        "opikProjectId": row.get("project_id", "") or "",
        "otelTraceId": meta.get(_OTEL_KEY, ""),
        "tenantId": meta.get("tenantId", ""),
        "threadId": row.get("thread_id") or "",
        "name": row.get("name") or "",
        # Opik has no scalar status; failure is modelled as error_info presence.
        "status": "error" if row.get("error_info") else "ok",
        "startTime": start,
        "endTime": row.get("end_time", "") or "",
        "latencyMs": _ms(row.get("duration")),
        "spanCount": int(row.get("span_count") or 0),
        "totalTokens": _usage_total(row.get("usage")),
        "costUsd": float(row.get("total_estimated_cost") or 0.0),
        "inputPreview": _text(row.get("input")),
        "outputPreview": _text(row.get("output")),
        "tags": _tags_of(row),
        # Opik-native extras DynamoDB could not provide. Additive: the UI ignores
        # keys it does not know.
        "providers": row.get("providers") or [],
        "llmSpanCount": int(row.get("llm_span_count") or 0),
        "feedbackScores": scores,
        # `onlineScores` is PROJECTED from feedback scores rather than stored twice,
        # so a human annotation and a judge score cannot disagree about a trace.
        "onlineScores": [{"name": s.get("name", ""),
                          "value": s.get("value", 0),
                          "passed": float(s.get("value") or 0) >= 0.7,
                          "reason": s.get("reason", "")} for s in scores],
        # Load-bearing: online_eval.run_sweep skips traces that already carry
        # `onlineScoredAt`. Opik has no such column, so it is derived from the
        # presence of a machine-sourced feedback score. Without this the sweep would
        # re-judge the same traces on every run and bill for it every time.
        "onlineScoredAt": next(
            (s.get("last_updated_at") or s.get("created_at") or row.get("last_updated_at", "")
             for s in scores if s.get("source") in ("online_scoring", "sdk")), ""),
    }


# Opik's span `type` vocabulary -> Aura's `kind`. Opik's default is "general", which
# Aura calls "unknown".
_KIND = {"llm": "llm", "tool": "tool", "retriever": "retriever",
         "guardrail": "tool", "general": "unknown"}


def _span_row(row: dict) -> dict:
    """One Opik span -> one Aura span row (`types.Span.as_item()` shape)."""
    start = row.get("start_time", "") or ""
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    err = row.get("error_info") if isinstance(row.get("error_info"), dict) else {}
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return {
        "traceId": row.get("trace_id", "") or "",
        "spanId": row.get("id", "") or "",
        "spanSortKey": f"{start}#{row.get('id', '')}",
        "parentSpanId": row.get("parent_span_id") or "",
        "name": row.get("name") or "",
        "kind": _KIND.get((row.get("type") or "").lower(), "unknown"),
        "status": "error" if err else "ok",
        "error": err.get("message", "") or "",
        "startTime": start,
        "endTime": row.get("end_time", "") or "",
        "latencyMs": _ms(row.get("duration")),
        "model": row.get("model") or "",
        "provider": row.get("provider") or "",
        "inputTokens": int(usage.get("prompt_tokens") or 0),
        "outputTokens": int(usage.get("completion_tokens") or 0),
        "cacheReadTokens": int(meta.get("cacheReadTokens")
                               or usage.get("cache_read_input_tokens") or 0),
        "totalTokens": _usage_total(usage),
        "costUsd": float(row.get("total_estimated_cost") or 0.0),
        "inputPreview": _text(row.get("input")),
        "outputPreview": _text(row.get("output")),
        # Opik holds the whole payload itself, so there is no S3 offload to point at.
        # The router falls back to the preview when these are empty, which is right:
        # under Opik the "preview" already IS the full text.
        "inputRef": "",
        "outputRef": "",
        "tags": _tags_of(row),
    }


def _to_ms(iso: str) -> int:
    """ISO-8601 -> epoch milliseconds, for the deterministic id's timestamp half."""
    from datetime import datetime, timezone
    if not iso:
        return 0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def opik_trace_id(trace_id: str, start_time: str) -> str:
    """The Opik id for an Aura trace. Pure, so reads can recompute rather than look up."""
    return opik_client.deterministic_uuid7(_to_ms(start_time), trace_id)


def opik_span_id(trace_id: str, span_id: str, start_time: str) -> str:
    return opik_client.deterministic_uuid7(_to_ms(start_time), f"{trace_id}:{span_id}")


# ── The store ──────────────────────────────────────────────────────────────────

class OpikTraceStore:
    """TraceStore backed by self-hosted Opik.

    Every read returns [] or None on failure rather than raising, matching
    DynamoTraceStore. A page that shows "no traces" during an outage is a bug worth
    fixing, but a smaller one than a 500 across the whole workspace — and
    `capabilities()` reports `degraded` so the UI can tell the difference.
    """

    name = "opik"

    # ── Write ───────────────────────────────────────────────────────────────

    def write_trace(self, trace: Trace, spans: list[Span]) -> None:
        """Persist a trace and its spans. Idempotent, via derived v7 ids.

        Spans go through the batch endpoint: one OTLP export can carry hundreds and
        a POST each would multiply ingest latency by the round-trip count.
        """
        tid = opik_trace_id(trace.trace_id, trace.start_time)
        meta = dict(trace.tags or {})
        meta[_OTEL_KEY] = trace.trace_id
        if trace.tenant_id:
            meta["tenantId"] = trace.tenant_id

        body: dict[str, Any] = {
            "id": tid,
            "project_name": trace.project_id,
            "name": trace.name or "trace",
            "start_time": trace.start_time,
            "end_time": trace.end_time or trace.start_time,
            "input": {"preview": trace.input_preview},
            "output": {"preview": trace.output_preview},
            "metadata": meta,
            "tags": ["aura"],
        }
        if trace.thread_id:
            body["thread_id"] = trace.thread_id
        if trace.status == "error":
            body["error_info"] = {"exception_type": "TraceError", "message": "see spans"}

        if not opik_client.post_idempotent("v1/private/traces", body):
            # Mirrors DynamoTraceStore: a failed TRACE write raises, because losing
            # the trace row loses the whole thing from the list view.
            raise RuntimeError(f"opik trace write failed for {trace.trace_id}")

        payload = [self._span_body(trace, span, tid) for span in spans]
        if payload and not opik_client.post_idempotent(
                "v1/private/spans/batch", {"spans": payload}):
            # Logged, not raised: the trace row already carries the aggregates, so a
            # partial write still renders in the list.
            log.warning("opik span batch failed for trace %s (%d spans)",
                        trace.trace_id, len(payload))

    def _span_body(self, trace: Trace, span: Span, opik_tid: str) -> dict:
        body: dict[str, Any] = {
            "id": opik_span_id(trace.trace_id, span.span_id, span.start_time),
            "trace_id": opik_tid,
            "project_name": trace.project_id,
            "name": span.name or "span",
            "type": span.kind if span.kind in ("llm", "tool", "guardrail") else "general",
            "start_time": span.start_time,
            "end_time": span.end_time or span.start_time,
            "input": {"preview": span.input_preview},
            "output": {"preview": span.output_preview},
            "metadata": {**(span.tags or {}),
                         "auraSpanId": span.span_id,
                         "cacheReadTokens": span.cache_read_tokens},
            "usage": {"prompt_tokens": int(span.input_tokens or 0),
                      "completion_tokens": int(span.output_tokens or 0),
                      "total_tokens": int(span.total_tokens or 0)},
            # Aura's calculate_cost_v2 figure, sent explicitly so the number in the
            # UI is the same number the invoice uses.
            "total_estimated_cost": round(float(span.cost_usd or 0.0), 10),
        }
        if span.model:
            body["model"] = span.model
        if span.provider:
            body["provider"] = span.provider
        if span.parent_span_id:
            # The parent's id is derived from the TRACE start time, matching how the
            # parent span itself was keyed when it was written.
            body["parent_span_id"] = opik_span_id(
                trace.trace_id, span.parent_span_id, span.start_time)
        if span.status == "error" or span.error:
            body["error_info"] = {"exception_type": "SpanError",
                                  "message": (span.error or "error")[:500]}
        return body

    # ── Read ────────────────────────────────────────────────────────────────

    def get_trace(self, project_id: str, trace_id: str) -> dict | None:
        """Fetch one trace by either an Opik id or the originating OTel id.

        A 36-char UUID is fetched directly. Anything else is found by the
        `aura.otel_trace_id` it was stored under — the derived id cannot be
        recomputed here because that needs the start time, which the caller does not
        have.
        """
        if not trace_id:
            return None

        if _is_uuid(trace_id):
            row = opik_client.request("GET", f"v1/private/traces/{trace_id}")
            if not isinstance(row, dict) or not row.get("id"):
                return None
            # The id alone is global, so scope to the caller's project rather than
            # trusting it — the same reasoning as DynamoTraceStore.get_trace.
            if project_id and row.get("project_name") not in (None, "", project_id):
                return None
            return _trace_row(row, project_id)

        filters = json.dumps([{"field": "metadata", "key": _OTEL_KEY,
                               "operator": "=", "value": trace_id}])
        page = opik_client.request("GET", "v1/private/traces", params={
            "project_name": project_id, "page": 1, "size": 1, "filters": filters})
        rows = (page or {}).get("content") or []
        return _trace_row(rows[0], project_id) if rows else None

    def get_spans(self, trace_id: str, limit: int = 1000,
                  project_id: str = "") -> list[dict]:
        """Spans of one trace, oldest first — the waterfall's render order.

        `project_id` is REQUIRED by Opik: `GET /v1/private/spans` answers
        `400 "Either 'project_name' or 'project_id' query params must be provided"`
        without it. The DynamoDB store needed no project because trace_id was its
        partition key, so the parameter is optional here and resolved from the trace
        when the caller does not supply it — at the cost of one extra request.
        """
        opik_tid = trace_id
        project_name = project_id
        opik_project_uuid = ""

        if not _is_uuid(trace_id) or not project_name:
            found = self.get_trace(project_id, trace_id)
            if not found:
                return []
            opik_tid = found["traceId"]
            project_name = project_name or found.get("projectId") or ""
            opik_project_uuid = found.get("opikProjectId") or ""

        params: dict[str, Any] = {"trace_id": opik_tid, "page": 1,
                                  "size": min(limit, _MAX_PAGE)}
        # A direct id fetch returns Opik's project UUID but not its name, so prefer
        # whichever identifier we actually have.
        if project_name:
            params["project_name"] = project_name
        elif opik_project_uuid:
            params["project_id"] = opik_project_uuid
        else:
            return []

        page = opik_client.request("GET", "v1/private/spans", params=params)
        rows = [_span_row(r) for r in ((page or {}).get("content") or [])]
        rows.sort(key=lambda r: r.get("spanSortKey", ""))
        return rows

    def list_traces(self, project_id: str, limit: int = 50,
                    status: str = "", thread_id: str = "",
                    search: str = "", tenant_id: str = "") -> list[dict]:
        """Newest first.

        Unlike the DynamoDB store, `thread_id` and `tenant_id` are pushed DOWN to
        the engine and `search` is supported at all — which is the entire reason for
        moving the read path here.
        """
        filters: list[dict] = []
        if thread_id:
            filters.append({"field": "thread_id", "operator": "=", "value": thread_id})
        if tenant_id:
            filters.append({"field": "metadata", "key": "tenantId",
                            "operator": "=", "value": tenant_id})

        params: dict[str, Any] = {
            "project_name": project_id,
            "page": 1,
            "size": min(max(limit, 1), _MAX_PAGE),
            "sorting": _SORT_NEWEST,
            # The list view renders previews, never full prompts.
            "truncate": "true",
        }
        if filters:
            params["filters"] = json.dumps(filters)
        if search:
            params["search"] = search

        page = opik_client.request("GET", "v1/private/traces", params=params)
        rows = [_trace_row(r, project_id) for r in ((page or {}).get("content") or [])]
        if status:
            # Still a post-filter, because Opik models failure as error_info presence
            # rather than a status enum. Unlike the DynamoDB store this narrows an
            # ENGINE-SORTED page rather than whatever a partition query returned.
            rows = [r for r in rows if r.get("status") == status]
        return rows[:limit]

    def list_threads(self, project_id: str, limit: int = 50) -> list[dict]:
        """Threads, most recently active first.

        Opik aggregates threads server-side, replacing the DynamoDB store's fan-out
        over `limit * 10` traces with a single request.
        """
        page = opik_client.request("GET", "v1/private/traces/threads", params={
            "project_name": project_id, "page": 1,
            "size": min(max(limit, 1), _MAX_PAGE), "truncate": "true"})
        out = []
        for row in ((page or {}).get("content") or []):
            out.append({
                "threadId": row.get("thread_model_id") or row.get("id") or "",
                "projectId": project_id,
                "traceCount": int(row.get("number_of_messages") or 0),
                "totalTokens": _usage_total(row.get("usage")),
                "costUsd": float(row.get("total_estimated_cost") or 0.0),
                "firstSeen": row.get("start_time", "") or "",
                "lastSeen": row.get("end_time") or row.get("last_updated_at") or "",
                "lastInput": _text(row.get("first_message") or row.get("input")),
                "status": row.get("status", ""),
            })
        return out[:limit]

    def list_projects(self, tenant_id: str = "") -> list[dict]:
        """Opik's own project list, enriched with per-project activity.

        No scan: projects are a first-class entity here. `last_updated_trace_at`
        comes back on the project row, so ordering needs no extra request.

        `tenant_id` is applied by asking each project whether it holds a trace for
        that tenant. That is one cheap indexed query per project rather than a scan,
        and it is only paid for when a tenant scope was actually requested.
        """
        page = opik_client.request("GET", "v1/private/projects", params={
            "page": 1, "size": 200,
            "sorting": json.dumps([{"field": "last_updated_trace_at",
                                    "direction": "DESC"}])})
        rows = (page or {}).get("content") or []

        out = []
        for row in rows:
            name = row.get("name") or ""
            if not name:
                continue
            if tenant_id and not self._project_has_tenant(name, tenant_id):
                continue
            out.append({
                "projectId": name,
                "opikProjectId": row.get("id", ""),
                "traceCount": int(row.get("trace_count") or 0),
                "costUsd": float(row.get("total_estimated_cost") or 0.0),
                "lastSeen": (row.get("last_updated_trace_at")
                             or row.get("last_updated_at") or ""),
                "description": row.get("description", ""),
            })
        return out

    def _project_has_tenant(self, project_name: str, tenant_id: str) -> bool:
        filters = json.dumps([{"field": "metadata", "key": "tenantId",
                               "operator": "=", "value": tenant_id}])
        page = opik_client.request("GET", "v1/private/traces", params={
            "project_name": project_name, "page": 1, "size": 1, "filters": filters})
        return bool((page or {}).get("total"))

    # ── Scores ──────────────────────────────────────────────────────────────

    def record_scores(self, project_id: str, trace: dict, scores: list) -> bool:
        """Persist judge scores as native Opik feedback scores.

        Native storage rather than a bag of JSON on the row: scores written here are
        filterable and sortable (`feedback_scores.*` is in Opik's sortable_by), show
        up in its dashboards, and land in the same place a human annotation would —
        which is what makes online eval and human review directly comparable.
        """
        trace_id = trace.get("traceId") or ""
        if not trace_id or not scores:
            return False
        if not _is_uuid(trace_id):
            resolved = self.get_trace(project_id, trace_id)
            if not resolved:
                return False
            trace_id = resolved["traceId"]

        payload = []
        for score in scores:
            item = score.as_item() if hasattr(score, "as_item") else dict(score)
            payload.append({
                "id": trace_id,
                "project_name": project_id,
                "name": item.get("name", "score"),
                "value": float(item.get("value") or 0.0),
                # Opik's own vocabulary for machine scoring, so Aura's sampled sweep
                # sits alongside Opik's automation rules instead of looking like a
                # hand annotation.
                "source": "online_scoring",
                "reason": (item.get("reason") or "")[:1000],
            })
        # PUT, not POST: Opik declares `@PUT /feedback-scores` and answers a POST
        # with 405 Method Not Allowed. Caught only by running it against a real
        # server — the stubbed test could not have found this.
        return opik_client.write(
            "v1/private/traces/feedback-scores", {"scores": payload}, method="PUT")

    # ── Capabilities ────────────────────────────────────────────────────────

    def capabilities(self) -> dict:
        """What this store can actually do — here the honest answer is "much more".

        The UI reads these to decide which controls to render, so flipping them on is
        what makes the richer filters appear. `degraded` is reported separately so an
        Opik outage renders as an outage rather than as an empty project.
        """
        return {
            "store": self.name,
            "indexedFilters": ["projectId", "threadId", "traceId", "tenantId"],
            "pageFilters": ["status"],
            "fullTextSearch": True,
            "tagFilter": True,
            "aggregations": True,
            "feedbackScores": True,
            "degraded": not opik_client.health(),
            "note": ("Opik on ClickHouse: free-text search over inputs/outputs, "
                     "metadata filtering and server-side thread aggregation are all "
                     "index-backed. Trace ids are Opik UUIDv7 derived from the "
                     "originating OTel id, which is preserved in metadata as "
                     "otelTraceId."),
        }

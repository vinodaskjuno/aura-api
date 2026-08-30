"""DynamoDB implementation of TraceStore.

Honest about its own limits: `capabilities()` reports exactly which filters are
index-backed, so the UI can hide controls this store cannot serve rather than
offering a filter that quietly turns into a table scan.
"""
from __future__ import annotations

import logging
from typing import Any

from src.aiobs.types import MAX_INLINE_PAYLOAD, Span, Trace

log = logging.getLogger(__name__)

TRACES = "ai-traces"
SPANS = "ai-spans"
_S3_BUCKET = "analysis"          # reuses the existing analysis bucket


class DynamoTraceStore:
    name = "dynamodb"

    # ── Write ───────────────────────────────────────────────────────────────

    def write_trace(self, trace: Trace, spans: list[Span]) -> None:
        from src.database import dynamo_client as db

        for span in spans:
            item = span.as_item()
            item["spanSortKey"] = f"{span.start_time}#{span.span_id}"
            item["projectId"] = trace.project_id
            item["tenantId"] = trace.tenant_id
            try:
                db.put_item(SPANS, item)
            except Exception as exc:      # noqa: BLE001 — one bad span must not lose the trace
                log.warning("span write failed %s/%s: %s", trace.trace_id, span.span_id, exc)

        try:
            db.put_item(TRACES, trace.as_item())
        except Exception as exc:          # noqa: BLE001
            log.error("trace write failed %s: %s", trace.trace_id, exc)
            raise

    # ── Read ────────────────────────────────────────────────────────────────

    def get_trace(self, project_id: str, trace_id: str) -> dict | None:
        from src.database import dynamo_client as db
        try:
            rows = db.query_items(TRACES, "traceId", trace_id,
                                  index_name="traceId-index", limit=1)
        except Exception as exc:          # noqa: BLE001
            log.warning("get_trace %s: %s", trace_id, exc)
            return None
        for row in rows or []:
            # The GSI is global, so scope the result to the caller's project rather
            # than trusting the id alone — trace ids come from client SDKs.
            if not project_id or row.get("projectId") == project_id:
                return row
        return None

    def get_spans(self, trace_id: str, limit: int = 1000) -> list[dict]:
        from src.database import dynamo_client as db
        try:
            rows = db.query_items(SPANS, "traceId", trace_id, limit=limit)
        except Exception as exc:          # noqa: BLE001
            log.warning("get_spans %s: %s", trace_id, exc)
            return []
        return sorted(rows or [], key=lambda r: r.get("spanSortKey", ""))

    def list_traces(self, project_id: str, limit: int = 50,
                    status: str = "", thread_id: str = "") -> list[dict]:
        from src.database import dynamo_client as db
        try:
            if thread_id:
                rows = db.query_items(TRACES, "threadId", thread_id,
                                      index_name="threadId-index", limit=limit * 2)
            else:
                rows = db.query_items(TRACES, "projectId", project_id, limit=limit * 2)
        except Exception as exc:          # noqa: BLE001
            log.warning("list_traces %s: %s", project_id, exc)
            return []
        rows = rows or []
        if status:
            # Applied after the query, so it narrows a page rather than searching the
            # table. capabilities() says as much.
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: r.get("sortKey", ""), reverse=True)
        return rows[:limit]

    def list_threads(self, project_id: str, limit: int = 50) -> list[dict]:
        """Threads are derived from traces rather than stored separately, so a
        thread cannot drift out of sync with the traces it groups."""
        threads: dict[str, dict] = {}
        for row in self.list_traces(project_id, limit=limit * 10):
            tid = row.get("threadId") or ""
            if not tid:
                continue
            t = threads.setdefault(tid, {
                "threadId": tid, "projectId": project_id, "traceCount": 0,
                "totalTokens": 0, "costUsd": 0.0,
                "firstSeen": row.get("startTime", ""), "lastSeen": row.get("startTime", ""),
                "lastInput": row.get("inputPreview", ""),
            })
            t["traceCount"] += 1
            t["totalTokens"] += int(row.get("totalTokens") or 0)
            t["costUsd"] += float(row.get("costUsd") or 0)
            start = row.get("startTime", "")
            if start and start < t["firstSeen"]:
                t["firstSeen"] = start
            if start and start > t["lastSeen"]:
                t["lastSeen"] = start
                t["lastInput"] = row.get("inputPreview", "")
        return sorted(threads.values(), key=lambda t: t["lastSeen"], reverse=True)[:limit]

    def capabilities(self) -> dict:
        return {
            "store": self.name,
            "indexedFilters": ["projectId", "threadId", "traceId"],
            "pageFilters": ["status"],       # narrows a page, does not search
            "fullTextSearch": False,
            "tagFilter": False,
            "aggregations": False,
            "note": ("DynamoDB indexes fixed access patterns. Free-text search over "
                     "inputs/outputs and ad-hoc tag filtering need an analytics store; "
                     "add one behind TraceStore rather than scanning this table."),
        }


# ── Payload offload ─────────────────────────────────────────────────────────

def store_payload(project_id: str, trace_id: str, span_id: str, kind: str,
                  text: str) -> tuple[str, str]:
    """Return (preview, s3_ref) for a payload, offloading it when large.

    Prompts and completions are unbounded; DynamoDB items are capped at 400 KB.
    Keeping a preview inline means the list and waterfall views never need S3,
    and only opening a span pays for the fetch.
    """
    text = text or ""
    preview = text[:512]
    if len(text.encode("utf-8")) <= MAX_INLINE_PAYLOAD:
        return preview, ""
    key = f"aiobs/{project_id}/{trace_id}/{span_id}.{kind}.txt"
    try:
        from src.storage import s3_client
        s3_client.put_object(_S3_BUCKET, key, text.encode("utf-8"))
        return preview, key
    except Exception as exc:              # noqa: BLE001 — a lost payload must not lose the span
        log.warning("payload offload failed %s: %s", key, exc)
        return preview, ""


def load_payload(ref: str) -> str:
    if not ref:
        return ""
    try:
        from src.storage import s3_client
        raw = s3_client.get_object(_S3_BUCKET, ref)
        return raw.decode("utf-8", errors="replace") if raw else ""
    except Exception as exc:              # noqa: BLE001
        log.warning("payload fetch failed %s: %s", ref, exc)
        return ""

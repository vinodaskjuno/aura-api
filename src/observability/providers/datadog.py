"""Datadog — logs, metrics, traces and deploy events."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from src.observability.base import ObservabilityProvider, ProviderError, ProviderNotConfigured
from src.observability.providers._http import HttpMixin
from src.observability.types import (
    EventQuery, EventRecord, LogPage, LogQuery, LogRecord, MetricPoint, MetricQuery,
    MetricSeries, ProviderHealth, Signal, SpanSummary, TraceQuery, TraceSummary,
)

log = logging.getLogger(__name__)


class DatadogProvider(HttpMixin, ObservabilityProvider):
    provider_type = "datadog"
    display_name = "Datadog"
    capabilities: frozenset[Signal] = frozenset({"logs", "metrics", "traces", "events"})

    @property
    def site(self) -> str:
        return self._cfg("site", default="datadoghq.com")

    @property
    def api_base(self) -> str:
        return f"https://api.{self.site}"

    @property
    def app_base(self) -> str:
        return f"https://app.{self.site}"

    def _headers(self) -> dict:
        api_key = self._cfg("api_key", "apiKey")
        app_key = self._cfg("app_key", "appKey")
        if not api_key:
            raise ProviderNotConfigured("datadog: api_key is not configured",
                                        provider_id=self.provider_id)
        h = {"DD-API-KEY": api_key, "Content-Type": "application/json"}
        if app_key:
            h["DD-APPLICATION-KEY"] = app_key
        return h

    def _build_dql(self, service: str, filter_text: str = "", levels=()) -> str:
        parts = []
        if service:
            parts.append(f"service:{service}")
        if levels:
            parts.append("(" + " OR ".join(f"status:{l.lower()}" for l in levels) + ")")
        if filter_text:
            parts.append(f'"{filter_text}"')
        return " ".join(parts) or "*"

    async def health(self) -> ProviderHealth:
        try:
            headers = self._headers()
        except ProviderNotConfigured as exc:
            return ProviderHealth(self.provider_id, self.provider_type,
                                  "not_configured", message=str(exc))
        ok, ms, msg = await self._probe(f"{self.api_base}/api/v1/validate", headers)
        return ProviderHealth(self.provider_id, self.provider_type,
                              "connected" if ok else "failed", latency_ms=ms, message=msg)

    async def query_logs(self, q: LogQuery) -> LogPage:
        t0 = time.monotonic()
        dql = q.raw_query or self._build_dql(q.service, q.filter, q.levels)
        page: dict = {"limit": min(q.limit, 1000)}
        if q.cursor:
            page["cursor"] = q.cursor
        try:
            data, _ = await self._request(
                "POST", f"{self.api_base}/api/v2/logs/events/search",
                headers=self._headers(),
                json_body={"filter": {"query": dql, "from": q.window.start, "to": q.window.end},
                           "page": page, "sort": "-timestamp"},
            )
        except ProviderError as exc:
            return LogPage(error=str(exc), query_ms=int((time.monotonic() - t0) * 1000))

        url = f"{self.app_base}/logs?query={dql}"
        records = []
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            inner = attrs.get("attributes") or {}
            records.append(LogRecord.make(
                self.provider_id, self.provider_type,
                attrs.get("timestamp", q.window.start),
                (attrs.get("status") or "UNKNOWN").upper(),
                attrs.get("service") or q.service,
                attrs.get("message", ""),
                labels={k: str(v) for k, v in inner.items()
                        if isinstance(v, (str, int, float)) and k != "message"},
                trace_id=str(inner.get("dd", {}).get("trace_id", "")),
                source_url=url,
            ))
        cursor = ((data.get("meta") or {}).get("page") or {}).get("after")
        return LogPage(records=records, cursor=cursor, total_estimate=len(records),
                       query_ms=int((time.monotonic() - t0) * 1000))

    async def query_metrics(self, q: MetricQuery) -> list[MetricSeries]:
        metric = q.metric or "trace.http.request.duration"
        query = q.raw_query or f"avg:{metric}{{service:{q.service}}}"
        start_s, end_s = q.window.epoch_s()
        try:
            data, _ = await self._request(
                "GET", f"{self.api_base}/api/v1/query", headers=self._headers(),
                params={"from": start_s, "to": end_s, "query": query})
        except ProviderError as exc:
            log.debug("datadog metrics failed: %s", exc)
            return []

        out = []
        for i, series in enumerate(data.get("series") or []):
            points = []
            for pair in series.get("pointlist") or []:
                if len(pair) < 2 or pair[1] is None:
                    continue
                ts = datetime.fromtimestamp(pair[0] / 1000.0, tz=timezone.utc).isoformat()
                points.append(MetricPoint(ts, float(pair[1])))
            if not points:
                continue
            s = MetricSeries(
                series_id=f"{self.provider_id}:{metric}:{i}",
                provider_id=self.provider_id, provider_type=self.provider_type,
                metric=series.get("metric", metric), service=q.service,
                unit=(series.get("unit") or [{}])[0].get("short_name", "") if series.get("unit") else "",
                points=points,
                source_url=f"{self.app_base}/metric/explorer?exp_metric={metric}",
            )
            out.append(s.compute_stats())
        return out

    async def query_traces(self, q: TraceQuery) -> list[TraceSummary]:
        dql = q.raw_query or (f"service:{q.service}" + (" status:error" if q.errors_only else ""))
        try:
            data, _ = await self._request(
                "POST", f"{self.api_base}/api/v2/spans/events/search",
                headers=self._headers(),
                json_body={"data": {"attributes": {
                    "filter": {"query": dql, "from": q.window.start, "to": q.window.end},
                    "page": {"limit": min(q.limit * 10, 500)},
                    "sort": "-@duration"}}},
            )
        except ProviderError as exc:
            log.debug("datadog traces failed: %s", exc)
            return []

        by_trace: dict[str, TraceSummary] = {}
        for item in data.get("data") or []:
            a = item.get("attributes") or {}
            tid = str(a.get("trace_id") or a.get("custom", {}).get("trace_id", ""))
            if not tid:
                continue
            dur_ms = float(a.get("duration", 0) or 0) / 1e6
            svc = a.get("service", q.service)
            is_err = str(a.get("status", "")).lower() == "error"
            tr = by_trace.get(tid)
            if tr is None:
                tr = TraceSummary(
                    trace_id=tid, provider_id=self.provider_id,
                    provider_type=self.provider_type, root_service=svc,
                    root_operation=a.get("resource_name", ""),
                    start_time=a.get("start_timestamp", q.window.start),
                    duration_ms=dur_ms,
                    source_url=f"{self.app_base}/apm/trace/{tid}",
                )
                by_trace[tid] = tr
            tr.span_count += 1
            tr.error_count += 1 if is_err else 0
            tr.duration_ms = max(tr.duration_ms, dur_ms)
            if svc not in tr.services_touched:
                tr.services_touched.append(svc)
            if len(tr.slowest_spans) < 5:
                tr.slowest_spans.append(SpanSummary(
                    span_id=str(a.get("span_id", "")), operation=a.get("resource_name", ""),
                    service=svc, duration_ms=round(dur_ms, 2),
                    status="error" if is_err else "ok"))
        for tr in by_trace.values():
            tr.status = "error" if tr.error_count else "ok"
            tr.slowest_spans.sort(key=lambda s: s.duration_ms, reverse=True)
        return sorted(by_trace.values(), key=lambda t: t.duration_ms, reverse=True)[: q.limit]

    async def recent_deploys(self, q: EventQuery) -> list[EventRecord]:
        start_s, end_s = q.window.epoch_s()
        try:
            data, _ = await self._request(
                "GET", f"{self.api_base}/api/v1/events", headers=self._headers(),
                params={"start": start_s, "end": end_s, "sources": "deploy,kubernetes"})
        except ProviderError as exc:
            log.debug("datadog events failed: %s", exc)
            return []

        out = []
        for ev in (data.get("events") or [])[: q.limit]:
            tags = {t.split(":", 1)[0]: t.split(":", 1)[1]
                    for t in (ev.get("tags") or []) if ":" in t}
            svc = tags.get("service", q.service)
            if q.service and svc != q.service:
                continue
            out.append(EventRecord.make(
                self.provider_id, self.provider_type,
                "deploy" if ev.get("source_type_name") == "deploy" else "k8s_event",
                datetime.fromtimestamp(ev.get("date_happened", start_s),
                                       tz=timezone.utc).isoformat(),
                svc, ev.get("title", ""),
                description=(ev.get("text") or "")[:500],
                version=tags.get("version", ""), actor=tags.get("user", ""),
                labels=tags, source_url=ev.get("url", ""),
            ))
        return out

    async def list_services(self) -> list[str]:
        try:
            data, _ = await self._request(
                "GET", f"{self.api_base}/api/v2/services", headers=self._headers())
            return [s.get("attributes", {}).get("name", "") for s in data.get("data") or []]
        except ProviderError:
            return []

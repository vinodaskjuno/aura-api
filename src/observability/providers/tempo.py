"""Grafana Tempo — traces."""
from __future__ import annotations

import logging

from src.observability.base import ProviderError
from src.observability.providers._grafana import GrafanaProvider
from src.observability.types import Signal, SpanSummary, TraceQuery, TraceSummary

log = logging.getLogger(__name__)


class TempoProvider(GrafanaProvider):
    provider_type = "tempo"
    display_name = "Grafana Tempo"
    capabilities: frozenset[Signal] = frozenset({"traces"})
    datasource_kind = "tempo"
    health_path = "/ready"

    async def query_traces(self, q: TraceQuery) -> list[TraceSummary]:
        start_s, end_s = q.window.epoch_s()
        tags = f"service.name={q.service}" if q.service else ""
        params: dict = {"start": start_s, "end": end_s, "limit": q.limit}
        if tags:
            params["tags"] = tags
        if q.min_duration_ms:
            params["minDuration"] = f"{q.min_duration_ms}ms"
        try:
            data, _ = await self._request("GET", f"{self.base_url}/api/search",
                                          headers=self._headers(), auth=self._auth(),
                                          params=params)
        except ProviderError as exc:
            log.debug("tempo search failed: %s", exc)
            return []

        out: list[TraceSummary] = []
        for tr in (data.get("traces") or [])[: q.limit]:
            trace_id = tr.get("traceID", "")
            from datetime import datetime, timezone
            try:
                start_iso = datetime.fromtimestamp(
                    int(tr.get("startTimeUnixNano", 0)) / 1e9, tz=timezone.utc).isoformat()
            except Exception:  # noqa: BLE001
                start_iso = q.window.start
            summary = TraceSummary(
                trace_id=trace_id, provider_id=self.provider_id,
                provider_type=self.provider_type,
                root_service=tr.get("rootServiceName", q.service),
                root_operation=tr.get("rootTraceName", ""),
                start_time=start_iso,
                duration_ms=float(tr.get("durationMs", 0) or 0),
                span_count=int(tr.get("spanSet", {}).get("matched", 0) or 0),
                source_url=(f"{self.grafana_url}/explore?traceId={trace_id}"
                            if self.grafana_url else ""),
            )
            out.append(summary)

        # Only the slowest few get a detail fetch — full span trees are expensive
        # and the model only ever reasons over the top offenders.
        for tr in sorted(out, key=lambda t: t.duration_ms, reverse=True)[:3]:
            await self._enrich(tr)

        if q.errors_only:
            out = [t for t in out if t.status == "error"]
        return out

    async def _enrich(self, tr: TraceSummary) -> None:
        try:
            data, _ = await self._request("GET", f"{self.base_url}/api/traces/{tr.trace_id}",
                                          headers=self._headers(), auth=self._auth())
        except ProviderError:
            return
        spans: list[SpanSummary] = []
        services: set[str] = set()
        errors = 0
        for batch in (data.get("batches") or []):
            svc = ""
            for attr in (batch.get("resource") or {}).get("attributes") or []:
                if attr.get("key") == "service.name":
                    svc = (attr.get("value") or {}).get("stringValue", "")
            if svc:
                services.add(svc)
            for scope in batch.get("scopeSpans") or []:
                for sp in scope.get("spans") or []:
                    dur_ms = (int(sp.get("endTimeUnixNano", 0)) -
                              int(sp.get("startTimeUnixNano", 0))) / 1e6
                    code = (sp.get("status") or {}).get("code", 0)
                    is_err = code == 2
                    errors += 1 if is_err else 0
                    spans.append(SpanSummary(
                        span_id=sp.get("spanId", ""), operation=sp.get("name", ""),
                        service=svc, duration_ms=round(dur_ms, 2),
                        status="error" if is_err else "ok",
                        error_message=(sp.get("status") or {}).get("message", ""),
                    ))
        tr.span_count = len(spans) or tr.span_count
        tr.error_count = errors
        tr.status = "error" if errors else "ok"
        tr.services_touched = sorted(services)
        tr.slowest_spans = sorted(spans, key=lambda s: s.duration_ms, reverse=True)[:5]

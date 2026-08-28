"""Grafana Mimir / Prometheus — metrics."""
from __future__ import annotations

import logging

from src.observability.base import ProviderError
from src.observability.providers._grafana import GrafanaProvider
from src.observability.types import MetricPoint, MetricQuery, MetricSeries, Signal

log = logging.getLogger(__name__)

# Sensible defaults so an operator who names no metric still gets the vitals.
_DEFAULT_METRICS = [
    ("http_request_duration_seconds", "s"),
    ("http_requests_total", "count"),
    ("container_memory_working_set_bytes", "bytes"),
    ("container_cpu_usage_seconds_total", "s"),
]


class MimirProvider(GrafanaProvider):
    provider_type = "mimir"
    display_name = "Grafana Mimir"
    capabilities: frozenset[Signal] = frozenset({"metrics"})
    datasource_kind = "prometheus"
    health_path = "/ready"

    def _build_promql(self, q: MetricQuery, metric: str) -> str:
        if q.raw_query:
            return q.raw_query
        sel = self._selector(q.service, q.labels).strip("{}")
        inner = f"{metric}{{{sel}}}" if sel else metric
        if metric.endswith("_total"):
            return f"sum(rate({inner}[5m]))"
        if "duration" in metric or "latency" in metric:
            return f"histogram_quantile(0.99, sum(rate({metric}_bucket{{{sel}}}[5m])) by (le))"
        return f"avg({inner})"

    async def query_metrics(self, q: MetricQuery) -> list[MetricSeries]:
        metrics = [(q.metric, "")] if q.metric else _DEFAULT_METRICS
        start_s, end_s = q.window.epoch_s()
        out: list[MetricSeries] = []
        for metric, unit in metrics:
            promql = self._build_promql(q, metric)
            try:
                data, _ = await self._request(
                    "GET", f"{self.base_url}/prometheus/api/v1/query_range",
                    headers=self._headers(), auth=self._auth(),
                    params={"query": promql, "start": start_s, "end": end_s,
                            "step": max(1, q.step_s)},
                )
            except ProviderError as exc:
                log.debug("mimir %s failed: %s", metric, exc)
                continue

            url = self.explore_url(promql, q.window)
            for res in (data.get("data") or {}).get("result") or []:
                labels = res.get("metric") or {}
                points = []
                for pair in res.get("values") or []:
                    if len(pair) < 2:
                        continue
                    try:
                        from datetime import datetime, timezone
                        ts = datetime.fromtimestamp(float(pair[0]), tz=timezone.utc).isoformat()
                        points.append(MetricPoint(ts, float(pair[1])))
                    except (TypeError, ValueError):
                        continue
                if not points:
                    continue
                series = MetricSeries(
                    series_id=f"{self.provider_id}:{metric}:{hash(str(sorted(labels.items()))) & 0xffff:04x}",
                    provider_id=self.provider_id, provider_type=self.provider_type,
                    metric=metric or promql, service=q.service, unit=unit,
                    labels=labels, points=points, source_url=url,
                )
                out.append(series.compute_stats())
        return out

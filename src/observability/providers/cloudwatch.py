"""AWS CloudWatch — logs, metrics, and alarms-as-events.

The alarm-fetching logic here is the same shape as
`routers/aiops.py::_fetch_cloudwatch_alarms`, lifted so both surfaces can share one
implementation. AIOps is repointed at this provider during phase-3 convergence.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from src.observability.base import ObservabilityProvider, ProviderError
from src.observability.types import (
    EventQuery, EventRecord, LogPage, LogQuery, LogRecord, MetricPoint, MetricQuery,
    MetricSeries, ProviderHealth, Signal,
)

log = logging.getLogger(__name__)


class CloudWatchProvider(ObservabilityProvider):
    provider_type = "cloudwatch"
    display_name = "AWS CloudWatch"
    capabilities: frozenset[Signal] = frozenset({"logs", "metrics", "events"})

    @property
    def region(self) -> str:
        from src.config_settings import get_settings
        return self._cfg("region") or get_settings().aws_region

    def _client(self, service: str):
        import boto3
        return boto3.client(service, region_name=self.region)

    def _console(self, path: str) -> str:
        return f"https://{self.region}.console.aws.amazon.com/cloudwatch/home?region={self.region}#{path}"

    async def health(self) -> ProviderHealth:
        t0 = time.monotonic()
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._client("cloudwatch").describe_alarms(MaxRecords=1))
            return ProviderHealth(self.provider_id, self.provider_type, "connected",
                                  latency_ms=int((time.monotonic() - t0) * 1000))
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(self.provider_id, self.provider_type, "failed",
                                  latency_ms=int((time.monotonic() - t0) * 1000),
                                  message=str(exc)[:200])

    # ── Logs (CloudWatch Logs Insights) ──────────────────────────────────────

    def _log_group(self, service: str) -> str:
        explicit = self._cfg("log_group", "logGroup")
        if explicit:
            return explicit
        prefix = self._cfg("log_group_prefix", "logGroupPrefix", default="/aws/ecs/")
        return f"{prefix}{service}" if service else prefix

    def _run_insights(self, q: LogQuery) -> list[LogRecord]:
        client = self._client("logs")
        group = self._log_group(q.service)
        levels = "|".join(q.levels) if q.levels else "ERROR|WARN"
        query = f"fields @timestamp, @message | filter @message like /{levels}/"
        if q.filter:
            query += f" | filter @message like /{q.filter}/"
        query += f" | sort @timestamp desc | limit {min(q.limit, 1000)}"
        if q.raw_query:
            query = q.raw_query

        start_s, end_s = q.window.epoch_s()
        started = client.start_query(logGroupName=group, startTime=start_s,
                                     endTime=end_s, queryString=query)
        qid = started["queryId"]
        for _ in range(60):                     # Insights is async; poll ~30s max
            res = client.get_query_results(queryId=qid)
            if res.get("status") in ("Complete", "Failed", "Cancelled"):
                break
            time.sleep(0.5)
        else:
            res = client.get_query_results(queryId=qid)

        url = self._console("logsV2:logs-insights")
        out = []
        for row in res.get("results") or []:
            fields = {f["field"]: f["value"] for f in row}
            out.append(LogRecord.make(
                self.provider_id, self.provider_type,
                fields.get("@timestamp", q.window.end),
                _sniff(fields.get("@message", "")), q.service,
                fields.get("@message", ""),
                labels={"logGroup": group}, source_url=url))
        return out

    async def query_logs(self, q: LogQuery) -> LogPage:
        t0 = time.monotonic()
        try:
            records = await asyncio.get_event_loop().run_in_executor(
                None, self._run_insights, q)
        except Exception as exc:  # noqa: BLE001
            return LogPage(error=str(exc)[:300],
                           query_ms=int((time.monotonic() - t0) * 1000))
        return LogPage(records=records, total_estimate=len(records),
                       query_ms=int((time.monotonic() - t0) * 1000))

    # ── Metrics ──────────────────────────────────────────────────────────────

    def _fetch_metrics(self, q: MetricQuery) -> list[MetricSeries]:
        client = self._client("cloudwatch")
        namespace = self._cfg("namespace", default="AWS/ECS")
        metrics = [q.metric] if q.metric else ["CPUUtilization", "MemoryUtilization"]
        dim_name = self._cfg("dimension_name", "dimensionName", default="ServiceName")

        out = []
        for m in metrics:
            resp = client.get_metric_statistics(
                Namespace=namespace, MetricName=m,
                Dimensions=[{"Name": dim_name, "Value": q.service}] if q.service else [],
                StartTime=q.window.start_dt, EndTime=q.window.end_dt,
                Period=max(60, q.step_s), Statistics=["Average"],
            )
            pts = sorted(resp.get("Datapoints") or [], key=lambda d: d["Timestamp"])
            if not pts:
                continue
            series = MetricSeries(
                series_id=f"{self.provider_id}:{m}",
                provider_id=self.provider_id, provider_type=self.provider_type,
                metric=m, service=q.service, unit=pts[0].get("Unit", ""),
                points=[MetricPoint(p["Timestamp"].astimezone(timezone.utc).isoformat(),
                                    float(p["Average"])) for p in pts],
                source_url=self._console(f"metricsV2:graph=~()"),
            )
            out.append(series.compute_stats())
        return out

    async def query_metrics(self, q: MetricQuery) -> list[MetricSeries]:
        try:
            return await asyncio.get_event_loop().run_in_executor(None, self._fetch_metrics, q)
        except Exception as exc:  # noqa: BLE001
            log.debug("cloudwatch metrics failed: %s", exc)
            return []

    # ── Alarms as events ─────────────────────────────────────────────────────

    def _fetch_alarms(self, q: EventQuery) -> list[EventRecord]:
        from src.observability.service_resolver import resolve
        client = self._client("cloudwatch")
        resp = client.describe_alarms(MaxRecords=100)
        out = []
        for a in resp.get("MetricAlarms", []):
            ts = a.get("StateUpdatedTimestamp")
            ts_iso = (ts.astimezone(timezone.utc).isoformat()
                      if hasattr(ts, "astimezone") else str(ts or ""))
            if ts_iso and not q.window.contains(ts_iso):
                continue
            svc = resolve(a.get("AlarmName", ""))
            if q.service and svc != q.service:
                continue
            out.append(EventRecord.make(
                self.provider_id, self.provider_type, "alert", ts_iso, svc,
                a.get("AlarmName", ""),
                description=a.get("AlarmDescription") or a.get("StateReason", ""),
                labels={"state": a.get("StateValue", ""),
                        "namespace": a.get("Namespace", "")},
                source_url=self._console(f"alarmsV2:alarm/{a.get('AlarmName','')}"),
            ))
        return out

    async def recent_deploys(self, q: EventQuery) -> list[EventRecord]:
        try:
            return await asyncio.get_event_loop().run_in_executor(None, self._fetch_alarms, q)
        except Exception as exc:  # noqa: BLE001
            log.debug("cloudwatch alarms failed: %s", exc)
            return []


def _sniff(body: str) -> str:
    upper = (body or "")[:200].upper()
    for lvl in ("FATAL", "ERROR", "WARN", "INFO", "DEBUG"):
        if lvl in upper:
            return lvl
    return "UNKNOWN"

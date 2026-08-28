"""Grafana Loki — logs."""
from __future__ import annotations

import logging
import time

from src.observability.base import ProviderError
from src.observability.providers._grafana import GrafanaProvider
from src.observability.types import LogPage, LogQuery, LogRecord, Signal

log = logging.getLogger(__name__)


class LokiProvider(GrafanaProvider):
    provider_type = "loki"
    display_name = "Grafana Loki"
    capabilities: frozenset[Signal] = frozenset({"logs"})
    datasource_kind = "loki"

    def _build_logql(self, q: LogQuery) -> str:
        if q.raw_query:
            return q.raw_query          # explicitly non-portable escape hatch
        expr = self._selector(q.service, q.labels)
        if q.filter:
            expr += f' |= "{q.filter}"'
        if q.levels:
            expr += f' | level =~ "{"|".join(q.levels)}"'
        return expr

    async def query_logs(self, q: LogQuery) -> LogPage:
        t0 = time.monotonic()
        logql = self._build_logql(q)
        start_ns, end_ns = q.window.epoch_ns()
        try:
            data, _ = await self._request(
                "GET", f"{self.base_url}/loki/api/v1/query_range",
                headers=self._headers(), auth=self._auth(),
                params={"query": logql, "start": start_ns, "end": end_ns,
                        "limit": q.limit, "direction": "backward"},
            )
        except ProviderError as exc:
            return LogPage(unsupported=False, error=str(exc),
                           query_ms=int((time.monotonic() - t0) * 1000))

        url = self.explore_url(logql, q.window)
        records = self._parse_streams((data.get("data") or {}).get("result") or [], q, url)
        return LogPage(records=records[: q.limit],
                       total_estimate=len(records),
                       query_ms=int((time.monotonic() - t0) * 1000))

    def _parse_streams(self, streams: list[dict], q: LogQuery, url: str) -> list[LogRecord]:
        out: list[LogRecord] = []
        for stream in streams:
            labels = stream.get("stream") or {}
            service = labels.get(self.service_label) or q.service
            level = (labels.get("level") or labels.get("detected_level") or "").upper()
            for entry in stream.get("values") or []:
                if len(entry) < 2:
                    continue
                ts_ns, body = entry[0], entry[1]
                try:
                    from datetime import datetime, timezone
                    ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc).isoformat()
                except Exception:  # noqa: BLE001
                    ts = str(ts_ns)
                out.append(LogRecord.make(
                    self.provider_id, self.provider_type, ts,
                    level or _sniff_level(body), service, body,
                    labels=labels, source_url=url,
                ))
        out.sort(key=lambda r: r.timestamp, reverse=True)
        return out

    async def list_services(self) -> list[str]:
        try:
            data, _ = await self._request(
                "GET", f"{self.base_url}/loki/api/v1/label/{self.service_label}/values",
                headers=self._headers(), auth=self._auth())
            return list(data.get("data") or [])
        except ProviderError:
            return []


def _sniff_level(body: str) -> str:
    upper = (body or "")[:200].upper()
    for lvl in ("FATAL", "ERROR", "WARN", "INFO", "DEBUG"):
        if lvl in upper:
            return lvl
    return "UNKNOWN"

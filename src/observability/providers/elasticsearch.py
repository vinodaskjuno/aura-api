"""Elasticsearch / OpenSearch — logs."""
from __future__ import annotations

import logging
import time

from src.observability.base import ObservabilityProvider, ProviderError, ProviderNotConfigured
from src.observability.providers._http import HttpMixin
from src.observability.types import LogPage, LogQuery, LogRecord, ProviderHealth, Signal

log = logging.getLogger(__name__)


class ElasticsearchProvider(HttpMixin, ObservabilityProvider):
    provider_type = "elasticsearch"
    display_name = "Elasticsearch"
    capabilities: frozenset[Signal] = frozenset({"logs"})

    @property
    def base_url(self) -> str:
        url = self._cfg("base_url", "baseUrl", "url")
        if not url:
            raise ProviderNotConfigured("elasticsearch: base_url is not configured",
                                        provider_id=self.provider_id)
        return url.rstrip("/")

    @property
    def index_pattern(self) -> str:
        return self._cfg("index_pattern", "indexPattern", default="logs-*")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        api_key = self._cfg("api_key", "apiKey")
        if api_key:
            h["Authorization"] = f"ApiKey {api_key}"
        return h

    def _auth(self):
        u, p = self._cfg("username"), self._cfg("password")
        return (u, p) if u and p else None

    def _service_field(self) -> str:
        return self._cfg("service_field", "serviceField", default="service.name")

    def _build_query(self, q: LogQuery) -> dict:
        must: list[dict] = [{"range": {"@timestamp": {
            "gte": q.window.start, "lte": q.window.end}}}]
        if q.service:
            must.append({"match_phrase": {self._service_field(): q.service}})
        if q.filter:
            must.append({"query_string": {"query": f'"{q.filter}"'}})
        if q.levels:
            must.append({"terms": {"log.level": [l.lower() for l in q.levels]}})
        for k, v in (q.labels or {}).items():
            if v:
                must.append({"match_phrase": {k: v}})
        return {"bool": {"must": must}}

    async def health(self) -> ProviderHealth:
        try:
            url = self.base_url
        except ProviderNotConfigured as exc:
            return ProviderHealth(self.provider_id, self.provider_type,
                                  "not_configured", message=str(exc))
        ok, ms, msg = await self._probe(f"{url}/_cluster/health", self._headers(), self._auth())
        return ProviderHealth(self.provider_id, self.provider_type,
                              "connected" if ok else "failed", latency_ms=ms, message=msg)

    async def query_logs(self, q: LogQuery) -> LogPage:
        t0 = time.monotonic()
        body: dict = {
            "size": min(q.limit, 1000),
            "sort": [{"@timestamp": "desc"}],
            "query": self._build_query(q),
        }
        if q.raw_query:
            body["query"] = {"query_string": {"query": q.raw_query}}
        if q.cursor:
            try:
                body["search_after"] = [int(q.cursor)]
            except ValueError:
                pass
        try:
            data, _ = await self._request(
                "POST", f"{self.base_url}/{self.index_pattern}/_search",
                headers=self._headers(), auth=self._auth(), json_body=body)
        except ProviderError as exc:
            return LogPage(error=str(exc), query_ms=int((time.monotonic() - t0) * 1000))

        hits = (data.get("hits") or {}).get("hits") or []
        records = []
        for h in hits:
            src = h.get("_source") or {}
            msg = (src.get("message") or src.get("log", {}).get("message")
                   or src.get("msg") or "")
            level = (src.get("log", {}).get("level") or src.get("level")
                     or src.get("severity") or "UNKNOWN")
            svc = _dig(src, self._service_field()) or q.service
            records.append(LogRecord.make(
                self.provider_id, self.provider_type,
                src.get("@timestamp", q.window.end), str(level).upper(), svc, str(msg),
                labels={k: str(v) for k, v in src.items()
                        if isinstance(v, (str, int, float)) and k != "message"},
                trace_id=str(_dig(src, "trace.id") or ""),
                source_url=f"{self.base_url}/app/discover",
            ))
        total = ((data.get("hits") or {}).get("total") or {})
        cursor = str(hits[-1].get("sort", [""])[0]) if hits else None
        return LogPage(records=records, cursor=cursor,
                       total_estimate=total.get("value") if isinstance(total, dict) else None,
                       query_ms=int((time.monotonic() - t0) * 1000))


def _dig(doc: dict, dotted: str):
    """Read `service.name` whether it is nested or a flat dotted key."""
    if dotted in doc:
        return doc[dotted]
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur

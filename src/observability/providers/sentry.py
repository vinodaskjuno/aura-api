"""Sentry — error events (mapped onto the `logs` signal) and releases (`events`)."""
from __future__ import annotations

import logging
import time

from src.observability.base import ObservabilityProvider, ProviderError, ProviderNotConfigured
from src.observability.providers._http import HttpMixin
from src.observability.types import (
    EventQuery, EventRecord, LogPage, LogQuery, LogRecord, ProviderHealth, Signal,
)

log = logging.getLogger(__name__)


class SentryProvider(HttpMixin, ObservabilityProvider):
    provider_type = "sentry"
    display_name = "Sentry"
    capabilities: frozenset[Signal] = frozenset({"logs", "events"})

    @property
    def base_url(self) -> str:
        return self._cfg("base_url", "baseUrl", default="https://sentry.io").rstrip("/")

    @property
    def org(self) -> str:
        return self._cfg("org", "organization")

    def _headers(self) -> dict:
        token = self._cfg("auth_token", "authToken", "token")
        if not token:
            raise ProviderNotConfigured("sentry: auth_token is not configured",
                                        provider_id=self.provider_id)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _project(self, service: str) -> str:
        return self._cfg("project") or service

    async def health(self) -> ProviderHealth:
        try:
            headers = self._headers()
        except ProviderNotConfigured as exc:
            return ProviderHealth(self.provider_id, self.provider_type,
                                  "not_configured", message=str(exc))
        if not self.org:
            return ProviderHealth(self.provider_id, self.provider_type,
                                  "not_configured", message="sentry: org is not configured")
        ok, ms, msg = await self._probe(
            f"{self.base_url}/api/0/organizations/{self.org}/", headers)
        return ProviderHealth(self.provider_id, self.provider_type,
                              "connected" if ok else "failed", latency_ms=ms, message=msg)

    async def query_logs(self, q: LogQuery) -> LogPage:
        """Sentry issues surface as ERROR-level records so they merge with real logs."""
        t0 = time.monotonic()
        project = self._project(q.service)
        if not (self.org and project):
            return LogPage(unsupported=True)
        query = q.raw_query or (f"{q.filter} is:unresolved" if q.filter else "is:unresolved")
        try:
            data, _ = await self._request(
                "GET", f"{self.base_url}/api/0/projects/{self.org}/{project}/issues/",
                headers=self._headers(),
                params={"query": query, "statsPeriod": "", "limit": min(q.limit, 100),
                        "start": q.window.start, "end": q.window.end},
            )
        except ProviderError as exc:
            return LogPage(error=str(exc), query_ms=int((time.monotonic() - t0) * 1000))

        records = []
        for issue in (data if isinstance(data, list) else []):
            meta = issue.get("metadata") or {}
            body = f"{issue.get('title','')} — {meta.get('value','')} " \
                   f"({issue.get('count', 0)} events, {issue.get('userCount', 0)} users)"
            records.append(LogRecord.make(
                self.provider_id, self.provider_type,
                issue.get("lastSeen") or q.window.end, "ERROR",
                issue.get("project", {}).get("slug", q.service), body,
                labels={"culprit": issue.get("culprit", ""),
                        "issueId": issue.get("id", ""),
                        "level": issue.get("level", "error"),
                        "type": meta.get("type", "")},
                source_url=issue.get("permalink", ""),
            ))
        return LogPage(records=records, total_estimate=len(records),
                       query_ms=int((time.monotonic() - t0) * 1000))

    async def recent_deploys(self, q: EventQuery) -> list[EventRecord]:
        if not self.org:
            return []
        try:
            data, _ = await self._request(
                "GET", f"{self.base_url}/api/0/organizations/{self.org}/releases/",
                headers=self._headers(), params={"per_page": min(q.limit, 100)})
        except ProviderError as exc:
            log.debug("sentry releases failed: %s", exc)
            return []

        out = []
        for rel in (data if isinstance(data, list) else []):
            ts = rel.get("dateReleased") or rel.get("dateCreated") or ""
            if ts and not q.window.contains(ts):
                continue
            authors = rel.get("authors") or []
            out.append(EventRecord.make(
                self.provider_id, self.provider_type, "release", ts, q.service,
                f"Release {rel.get('shortVersion') or rel.get('version','')}",
                description=(rel.get("ref") or ""),
                version=rel.get("version", ""),
                actor=(authors[0].get("email", "") if authors else ""),
                source_url=f"{self.base_url}/organizations/{self.org}/releases/{rel.get('version','')}/",
            ))
        return out

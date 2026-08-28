"""PagerDuty — incidents and their log entries, as `events`."""
from __future__ import annotations

import logging

from src.observability.base import ObservabilityProvider, ProviderError, ProviderNotConfigured
from src.observability.providers._http import HttpMixin
from src.observability.types import EventQuery, EventRecord, ProviderHealth, Signal

log = logging.getLogger(__name__)

_BASE = "https://api.pagerduty.com"


class PagerDutyProvider(HttpMixin, ObservabilityProvider):
    provider_type = "pagerduty"
    display_name = "PagerDuty"
    capabilities: frozenset[Signal] = frozenset({"events"})

    def _headers(self) -> dict:
        token = self._cfg("api_token", "apiToken", "token")
        if not token:
            raise ProviderNotConfigured("pagerduty: api_token is not configured",
                                        provider_id=self.provider_id)
        return {"Authorization": f"Token token={token}",
                "Accept": "application/vnd.pagerduty+json;version=2"}

    async def health(self) -> ProviderHealth:
        try:
            headers = self._headers()
        except ProviderNotConfigured as exc:
            return ProviderHealth(self.provider_id, self.provider_type,
                                  "not_configured", message=str(exc))
        ok, ms, msg = await self._probe(f"{_BASE}/abilities", headers)
        return ProviderHealth(self.provider_id, self.provider_type,
                              "connected" if ok else "failed", latency_ms=ms, message=msg)

    async def fetch_incidents(self, window=None, statuses=("triggered", "acknowledged"),
                              limit: int = 100) -> list[dict]:
        params: dict = {"limit": limit, "statuses[]": list(statuses),
                        "sort_by": "created_at:desc"}
        if window:
            params["since"] = window.start
            params["until"] = window.end
        svc_ids = self.config.get("service_ids") or self.config.get("serviceIds")
        if svc_ids:
            params["service_ids[]"] = svc_ids if isinstance(svc_ids, list) else [svc_ids]
        try:
            data, _ = await self._request("GET", f"{_BASE}/incidents",
                                          headers=self._headers(), params=params)
            return data.get("incidents") or []
        except ProviderError as exc:
            log.debug("pagerduty incidents failed: %s", exc)
            return []

    async def recent_deploys(self, q: EventQuery) -> list[EventRecord]:
        incidents = await self.fetch_incidents(
            window=q.window, statuses=("triggered", "acknowledged", "resolved"),
            limit=min(q.limit, 100))
        out = []
        for inc in incidents:
            svc = (inc.get("service") or {}).get("summary", "") or q.service
            if q.service and q.service.lower() not in svc.lower():
                continue
            assignments = inc.get("assignments") or []
            out.append(EventRecord.make(
                self.provider_id, self.provider_type, "incident",
                inc.get("created_at", q.window.start), svc,
                inc.get("title", inc.get("summary", "")),
                description=inc.get("description", "")[:500],
                actor=(assignments[0].get("assignee", {}).get("summary", "")
                       if assignments else ""),
                labels={"status": inc.get("status", ""),
                        "urgency": inc.get("urgency", ""),
                        "incidentNumber": str(inc.get("incident_number", "")),
                        "incidentId": inc.get("id", "")},
                source_url=inc.get("html_url", ""),
            ))
        return out

    async def resolution_notes(self, incident_id: str) -> dict:
        """Resolver + resolution text — a 0.5-weight outcome signal for the learning loop."""
        try:
            data, _ = await self._request(
                "GET", f"{_BASE}/incidents/{incident_id}/log_entries",
                headers=self._headers(), params={"limit": 100})
        except ProviderError:
            return {}
        for entry in reversed(data.get("log_entries") or []):
            if entry.get("type", "").startswith("resolve_log_entry"):
                agent = entry.get("agent") or {}
                return {"resolved_by": agent.get("summary", ""),
                        "note": entry.get("channel", {}).get("summary", "")
                                or entry.get("summary", ""),
                        "resolved_at": entry.get("created_at", "")}
        return {}

    async def list_services(self) -> list[str]:
        try:
            data, _ = await self._request("GET", f"{_BASE}/services",
                                          headers=self._headers(), params={"limit": 100})
            return [s.get("name", "") for s in data.get("services") or []]
        except ProviderError:
            return []

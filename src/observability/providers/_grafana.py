"""Shared base for the Grafana stack (Loki / Mimir / Tempo).

One auth scheme, one timeout policy, one deep-link builder — the three adapters are
thin subclasses over a single HTTP client.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote, urlencode

from src.observability.base import ObservabilityProvider, ProviderNotConfigured
from src.observability.providers._http import HttpMixin
from src.observability.types import ProviderHealth, TimeWindow

log = logging.getLogger(__name__)


class GrafanaProvider(HttpMixin, ObservabilityProvider):
    """Common config: base_url, api_token | (username,password), org_id, grafana_url."""

    health_path = "/ready"
    datasource_kind = ""     # "loki" | "prometheus" | "tempo" — for Explore links

    @property
    def base_url(self) -> str:
        url = self._cfg("base_url", "baseUrl", "url")
        if not url:
            raise ProviderNotConfigured(
                f"{self.provider_type}: base_url is not configured",
                provider_id=self.provider_id)
        return url.rstrip("/")

    @property
    def grafana_url(self) -> str:
        return self._cfg("grafana_url", "grafanaUrl").rstrip("/")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        token = self._cfg("api_token", "apiToken", "token")
        if token:
            h["Authorization"] = f"Bearer {token}"
        org = self._cfg("org_id", "orgId")
        if org:
            h["X-Scope-OrgID"] = org
        return h

    def _auth(self):
        user = self._cfg("username", "user")
        pwd = self._cfg("password")
        return (user, pwd) if user and pwd else None

    @property
    def service_label(self) -> str:
        return self._cfg("service_label", "serviceLabel", default="service_name")

    def _selector(self, service: str, labels: dict | None = None) -> str:
        parts = []
        if service:
            parts.append(f'{self.service_label}="{service}"')
        for k, v in (labels or {}).items():
            if v:
                parts.append(f'{k}="{v}"')
        for k, v in (self.config.get("default_labels") or {}).items():
            if v and not any(p.startswith(f"{k}=") for p in parts):
                parts.append(f'{k}="{v}"')
        return "{" + ", ".join(parts) + "}" if parts else '{job=~".+"}'

    def explore_url(self, expr: str, window: TimeWindow) -> str:
        """A deep link a human can actually click. Empty when Grafana isn't configured."""
        if not self.grafana_url:
            return ""
        uid = self._cfg("datasource_uid", "datasourceUid")
        left = {
            "datasource": uid or self.datasource_kind,
            "queries": [{"refId": "A", "expr": expr,
                         "datasource": {"type": self.datasource_kind, "uid": uid}}],
            "range": {"from": window.start, "to": window.end},
        }
        return f"{self.grafana_url}/explore?" + urlencode({"left": json.dumps(left)})

    async def health(self) -> ProviderHealth:
        try:
            url = self.base_url + self.health_path
        except ProviderNotConfigured as exc:
            return ProviderHealth(self.provider_id, self.provider_type,
                                  "not_configured", message=str(exc))
        ok, ms, msg = await self._probe(url, self._headers(), self._auth())
        return ProviderHealth(self.provider_id, self.provider_type,
                              "connected" if ok else "failed", latency_ms=ms, message=msg)

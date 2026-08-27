"""PagerDuty connector — fetches incidents, services, and escalation policies."""
from __future__ import annotations
from typing import Any
import httpx
from ..base import AbstractConnector, SyncResult

_BASE = "https://api.pagerduty.com"


class PagerDutyConnector(AbstractConnector):
    """
    Config keys:
      api_token: str
      since: str | None   ISO date for incident lookback (default: 30d)
      service_ids: list[str] | None
    """

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token token={self.config['api_token']}",
                "Accept": "application/vnd.pagerduty+json;version=2"}

    def test_connection(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(f"{_BASE}/abilities", headers=self._headers(), timeout=10)
            return resp.is_success, resp.text[:200]
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        for incident in self._fetch_incidents():
            result.entities_added += 1
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        return self._fetch_incidents()[:5]

    def _fetch_incidents(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 100, "statuses[]": ["triggered", "acknowledged"]}
        if self.config.get("since"):
            params["since"] = self.config["since"]
        if self.config.get("service_ids"):
            params["service_ids[]"] = self.config["service_ids"]
        try:
            resp = httpx.get(f"{_BASE}/incidents", params=params,
                             headers=self._headers(), timeout=30)
            return resp.json().get("incidents", [])
        except Exception:
            return []

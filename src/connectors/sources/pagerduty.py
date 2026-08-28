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
        """Persist incidents as Incident nodes linked to their Service.

        Previously this only incremented a counter and wrote nothing, which is why
        the connector was never registered anywhere.
        """
        result = SyncResult()
        try:
            from src.graph import neo4j_client as graph
            from src.ontology.schema import KNOWN_SOURCES  # noqa: F401  (vocabulary check)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"graph unavailable: {exc}")
            return result

        for incident in self._fetch_incidents():
            try:
                eid = f"pagerduty:{incident.get('id','')}"
                service = (incident.get("service") or {}).get("summary", "") or "UnknownService"
                props = {
                    "name": incident.get("title") or incident.get("summary", ""),
                    "title": incident.get("title") or incident.get("summary", ""),
                    "status": incident.get("status", ""),
                    "urgency": incident.get("urgency", ""),
                    "startedAt": incident.get("created_at", ""),
                    "resolvedAt": incident.get("resolved_at", "") or "",
                    "serviceName": service,
                    "sourceUrl": incident.get("html_url", ""),
                    "source": "pagerduty",
                    "incidentNumber": str(incident.get("incident_number", "")),
                }
                existing = graph.upsert_node("Incident", eid, props)
                graph.upsert_node("Service", f"service:{service}", {"name": service})
                graph.upsert_relationship(
                    "Service", f"service:{service}", "Incident", eid, "HAS_INCIDENT",
                    provenance={"source": "pagerduty",
                                "sourceRecordId": incident.get("id", ""),
                                "discoveredBy": "pagerduty_connector",
                                "confidence": 1.0, "factType": "known"},
                )
                if existing:
                    result.entities_updated += 1
                else:
                    result.entities_added += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append(str(exc))
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

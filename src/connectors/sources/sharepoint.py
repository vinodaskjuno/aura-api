"""SharePoint / Microsoft Graph connector — fetches documents and site pages."""
from __future__ import annotations
from typing import Any
import httpx
from ..base import AbstractConnector, SyncResult

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class SharePointConnector(AbstractConnector):
    """
    Config keys:
      tenant_id: str
      client_id: str
      client_secret: str
      site_id: str          SharePoint site ID
      drive_id: str | None  optional
    """

    def _get_token(self) -> str:
        resp = httpx.post(
            f"https://login.microsoftonline.com/{self.config['tenant_id']}/oauth2/v2.0/token",
            data={
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        return resp.json()["access_token"]

    def test_connection(self) -> tuple[bool, str]:
        try:
            token = self._get_token()
            resp = httpx.get(
                f"{_GRAPH_BASE}/sites/{self.config['site_id']}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            return resp.is_success, resp.text[:200]
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        try:
            token = self._get_token()
            items = self._list_drive_items(token)
            for _ in items:
                result.entities_added += 1
        except Exception as exc:
            result.errors.append(str(exc))
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        try:
            token = self._get_token()
            return self._list_drive_items(token)[:5]
        except Exception:
            return []

    def _list_drive_items(self, token: str) -> list[dict[str, Any]]:
        drive_id = self.config.get("drive_id", "root")
        resp = httpx.get(
            f"{_GRAPH_BASE}/sites/{self.config['site_id']}/drives/{drive_id}/root/children",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        return resp.json().get("value", [])

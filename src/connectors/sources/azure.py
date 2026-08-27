"""Azure connector — discovers VMs, AKS clusters, App Services, and Azure AD."""
from __future__ import annotations
from typing import Any
import httpx
from ..base import AbstractConnector, SyncResult

_ARM_BASE = "https://management.azure.com"
_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


class AzureConnector(AbstractConnector):
    """
    Config keys:
      tenant_id: str
      client_id: str
      client_secret: str
      subscription_id: str
      resource_groups: list[str] | None
    """

    def _get_token(self) -> str:
        resp = httpx.post(
            _TOKEN_URL.format(tenant_id=self.config["tenant_id"]),
            data={
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "scope": "https://management.azure.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        return resp.json()["access_token"]

    def test_connection(self) -> tuple[bool, str]:
        try:
            token = self._get_token()
            sub = self.config["subscription_id"]
            resp = httpx.get(
                f"{_ARM_BASE}/subscriptions/{sub}?api-version=2022-12-01",
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
            resources = self._list_resources(token)
            result.entities_added = len(resources)
        except Exception as exc:
            result.errors.append(str(exc))
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        try:
            token = self._get_token()
            return self._list_resources(token)[:5]
        except Exception:
            return []

    def _list_resources(self, token: str) -> list[dict[str, Any]]:
        sub = self.config["subscription_id"]
        resp = httpx.get(
            f"{_ARM_BASE}/subscriptions/{sub}/resources?api-version=2021-04-01",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        return resp.json().get("value", [])

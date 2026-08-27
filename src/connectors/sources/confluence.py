"""Confluence connector — fetches pages, spaces, and wiki articles."""
from __future__ import annotations
from typing import Any
import httpx
from ..base import AbstractConnector, SyncResult


class ConfluenceConnector(AbstractConnector):
    """
    Config keys:
      base_url: str       e.g. https://myorg.atlassian.net/wiki
      username: str
      api_token: str
      space_keys: list[str]  optional filter
    """

    def test_connection(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                f"{self.config['base_url']}/rest/api/space",
                auth=(self.config["username"], self.config["api_token"]),
                timeout=10,
            )
            return resp.is_success, resp.text[:200]
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        pages = self._fetch_all_pages()
        for page in pages:
            # TODO: write to Neo4j via file_load_service or graph_builder
            result.entities_added += 1
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        return self._fetch_all_pages()[:5]

    def _fetch_all_pages(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 50, "expand": "body.view,metadata.labels"}
        if self.config.get("space_keys"):
            params["spaceKey"] = ",".join(self.config["space_keys"])
        try:
            resp = httpx.get(
                f"{self.config['base_url']}/rest/api/content",
                params=params,
                auth=(self.config["username"], self.config["api_token"]),
                timeout=30,
            )
            data = resp.json()
            return data.get("results", [])
        except Exception:
            return []

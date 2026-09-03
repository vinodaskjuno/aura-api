"""Confluence connector — fetches pages, spaces, and wiki articles."""
from __future__ import annotations

import re
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

    pipeline = "api"

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

    # Pages that look like operational documentation become Runbook nodes; the rest
    # stay WikiArticle. This is the connector path for runbook ingestion.
    _RUNBOOK_LABELS = {"runbook", "sop", "playbook", "incident", "oncall", "on-call"}
    _RUNBOOK_TITLE_RE = re.compile(r"^(runbook|playbook|sop|on[- ]?call)\b", re.IGNORECASE)

    def _classify(self, page: dict) -> str:
        labels = {
            (lbl.get("name") or "").lower()
            for lbl in ((page.get("metadata") or {}).get("labels") or {}).get("results", [])
        }
        if labels & self._RUNBOOK_LABELS:
            return "Runbook"
        if self._RUNBOOK_TITLE_RE.match(page.get("title", "") or ""):
            return "Runbook"
        return "WikiArticle"

    def sync(self) -> SyncResult:
        """Persist pages into the graph.

        Previously a literal `# TODO: write to Neo4j` with a counter increment, which
        is why this connector was never registered anywhere.
        """
        result = SyncResult()
        try:
            from src.graph import neo4j_client as graph
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"graph unavailable: {exc}")
            return result

        base = str(self.config.get("base_url", "")).rstrip("/")
        for page in self._fetch_all_pages():
            try:
                label = self._classify(page)
                pid = page.get("id", "")
                eid = f"confluence:{pid}"
                body_html = (((page.get("body") or {}).get("view") or {}).get("value") or "")
                body_text = re.sub(r"<[^>]+>", " ", body_html)
                body_text = re.sub(r"\s+", " ", body_text).strip()
                labels = [
                    (lbl.get("name") or "")
                    for lbl in ((page.get("metadata") or {}).get("labels") or {}).get("results", [])
                ]
                props = {
                    "name": page.get("title", ""),
                    "title": page.get("title", ""),
                    "sourceType": "confluence",
                    "sourceUrl": f"{base}/pages/{pid}" if base else "",
                    "bodySnippet": body_text[:8000],
                    "tags": [l for l in labels if l],
                    "origin": "confluence",
                    "status": "active",
                    "source": "confluence",
                    "spaceKey": (page.get("space") or {}).get("key", ""),
                }
                graph.upsert_node(label, eid, props)
                result.entities_added += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append(str(exc))
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

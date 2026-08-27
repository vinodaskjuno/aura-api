"""Jira connector — fetches issues, epics, and project metadata."""
from __future__ import annotations
from typing import Any
import httpx
from ..base import AbstractConnector, SyncResult


class JiraConnector(AbstractConnector):
    """
    Config keys:
      base_url: str         e.g. https://myorg.atlassian.net
      username: str
      api_token: str
      project_keys: list[str]
      max_results: int      default 100
    """

    def test_connection(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(
                f"{self.config['base_url']}/rest/api/3/myself",
                auth=(self.config["username"], self.config["api_token"]),
                timeout=10,
            )
            return resp.is_success, resp.text[:200]
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        for issue in self._fetch_issues():
            # TODO: write Ticket node to Neo4j
            result.entities_added += 1
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        return self._fetch_issues()[:5]

    def _fetch_issues(self) -> list[dict[str, Any]]:
        jql = "ORDER BY updated DESC"
        if self.config.get("project_keys"):
            projects = " OR ".join(f'project = "{k}"' for k in self.config["project_keys"])
            jql = f"({projects}) {jql}"
        try:
            resp = httpx.get(
                f"{self.config['base_url']}/rest/api/3/search",
                params={"jql": jql, "maxResults": self.config.get("max_results", 100),
                        "fields": "summary,status,priority,assignee,issuetype,components"},
                auth=(self.config["username"], self.config["api_token"]),
                timeout=30,
            )
            return resp.json().get("issues", [])
        except Exception:
            return []

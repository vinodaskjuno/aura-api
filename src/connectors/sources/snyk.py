"""Snyk connector — fetches vulnerability findings and license issues."""
from __future__ import annotations
from typing import Any
import httpx
from ..base import AbstractConnector, SyncResult

_BASE = "https://api.snyk.io/v1"


class SnykConnector(AbstractConnector):
    """
    Config keys:
      api_token: str
      org_id: str
      project_ids: list[str] | None
    """

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.config['api_token']}",
                "Content-Type": "application/json"}

    def test_connection(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(f"{_BASE}/orgs/{self.config['org_id']}/projects",
                             headers=self._headers(), timeout=10)
            return resp.is_success, resp.text[:200]
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        for issue in self._fetch_issues():
            result.entities_added += 1
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        return self._fetch_issues()[:5]

    def _fetch_issues(self) -> list[dict[str, Any]]:
        org = self.config["org_id"]
        project_ids = self.config.get("project_ids", [])
        if not project_ids:
            try:
                resp = httpx.get(f"{_BASE}/orgs/{org}/projects",
                                 headers=self._headers(), timeout=15)
                project_ids = [p["id"] for p in resp.json().get("projects", [])[:20]]
            except Exception:
                return []
        all_issues: list[dict] = []
        for pid in project_ids:
            try:
                resp = httpx.post(
                    f"{_BASE}/org/{org}/project/{pid}/issues",
                    json={"filters": {"severities": ["critical", "high", "medium"]}},
                    headers=self._headers(),
                    timeout=30,
                )
                issues = resp.json().get("issues", {})
                all_issues.extend(issues.get("vulnerabilities", [])[:50])
            except Exception:
                pass
        return all_issues

import httpx
from src.connectors.base import AbstractConnector, SyncResult

CORE_NS = "http://ontology.aura.com/core#"


class McpConnector(AbstractConnector):
    """MCP (Model Context Protocol) connector — supports both SSE endpoints and local mock."""

    def _base_url(self) -> str:
        return self.config.get("endpoint", "").rstrip("/")

    def _probe_url(self) -> str:
        """Return the URL to probe for liveness (strip /sse suffix to get the root)."""
        url = self._base_url()
        if url.endswith("/sse"):
            url = url[:-4]
        return url

    def _is_local_mock(self) -> bool:
        url = self._base_url()
        return "localhost" in url or "127.0.0.1" in url

    def test_connection(self) -> tuple[bool, str]:
        endpoint = self._base_url()
        if not endpoint:
            return False, "No endpoint configured"

        probe = self._probe_url()
        try:
            response = httpx.get(probe, timeout=10.0, follow_redirects=True)
            # Any non-5xx response means the server is up
            if response.status_code < 500:
                return True, f"Server reachable at {probe} (HTTP {response.status_code})"
            return False, f"Server error: HTTP {response.status_code}"
        except httpx.TimeoutException:
            return False, f"Connection timed out connecting to {probe}"
        except httpx.RequestError as exc:
            return False, f"Cannot reach {probe}: {exc}"
        except Exception as exc:
            return False, f"Unexpected error: {exc}"

    def sync(self) -> SyncResult:
        """For MCP connectors, sync returns summary counts from the mock source.
        Actual Neo4j ingestion is done via POST /connectors/{id}/ingest → run_full_load().
        """
        result = SyncResult()
        endpoint = self._base_url()
        if not endpoint:
            result.errors.append("No endpoint configured")
            return result

        if self._is_local_mock():
            # Call mock tools directly (same-process, no HTTP round-trip)
            try:
                from src.connectors.mock_mcp.server import (
                    git_list_repos,
                    servicenow_list_cmdb_items,
                    servicenow_list_incidents,
                    wiz_list_findings,
                )
                repos = git_list_repos()
                cmdb = servicenow_list_cmdb_items()
                incidents = servicenow_list_incidents()
                findings = wiz_list_findings()
                result.entities_added = len(repos) + len(cmdb) + len(incidents) + len(findings)
                return result
            except Exception as exc:
                result.errors.append(f"Mock data fetch error: {exc}")
                return result

        # External MCP server: only probe liveness, no sync (use ingest instead)
        ok, msg = self.test_connection()
        if not ok:
            result.errors.append(msg)
        return result

    def get_metadata(self) -> list[dict]:
        if self._is_local_mock():
            try:
                from src.connectors.mock_mcp.server import get_all_data_summary
                summary = get_all_data_summary()
                return [{"source": k, "count": v} for k, v in summary.items() if isinstance(v, (int, float))]
            except Exception as exc:
                return [{"error": str(exc)}]

        probe = self._probe_url()
        return [{"endpoint": probe, "protocol": "MCP/SSE"}]

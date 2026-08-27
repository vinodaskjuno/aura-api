"""Mock MCP HTTP server — exposes enterprise mock data via MCP protocol over HTTP/SSE.

Mount this on the main FastAPI app:
    app.mount("/mock-mcp", mock_mcp_asgi_app())

Endpoint for connectors: http://localhost:8000/mock-mcp/sse
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .server import (
    git_list_repos,
    git_list_services,
    servicenow_list_incidents,
    servicenow_list_cmdb_items,
    servicenow_list_changes,
    wiz_list_findings,
    wiz_get_finding_detail,
    get_all_data_summary,
)

mcp = FastMCP(name="Aura Mock Enterprise MCP")


@mcp.tool(description="List all Git repositories in the organisation")
def tool_git_list_repos() -> list[dict]:
    return git_list_repos()


@mcp.tool(description="List services/microservices in a given repository")
def tool_git_list_services(repo_name: str) -> list[dict]:
    return git_list_services(repo_name)


@mcp.tool(description="List recent ServiceNow incidents (last N days)")
def tool_servicenow_list_incidents(days: int = 30) -> list[dict]:
    return servicenow_list_incidents(days)


@mcp.tool(description="List CMDB configuration items by type (server|database|network|all)")
def tool_servicenow_list_cmdb_items(type: str = "all") -> list[dict]:
    return servicenow_list_cmdb_items(type)


@mcp.tool(description="List recent ServiceNow change requests (last N days)")
def tool_servicenow_list_changes(days: int = 30) -> list[dict]:
    return servicenow_list_changes(days)


@mcp.tool(description="List Wiz security findings filtered by severity (CRITICAL|HIGH|MEDIUM|LOW|all)")
def tool_wiz_list_findings(severity: str = "all") -> list[dict]:
    return wiz_list_findings(severity)


@mcp.tool(description="Get full details of a Wiz security finding by ID")
def tool_wiz_get_finding_detail(finding_id: str) -> dict:
    return wiz_get_finding_detail(finding_id)


@mcp.tool(description="Get a summary of all available mock data (counts per source)")
def tool_get_summary() -> dict:
    return get_all_data_summary()


def mock_mcp_asgi_app():
    """Return the ASGI app to mount on FastAPI (SSE transport)."""
    return mcp.sse_app()

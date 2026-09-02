"""The MCP connector surface: what is registered, what it exposes, does it work.

Reads the SAME `user-connectors` rows as the Connectors page — `type="mcp"`. There is
deliberately no second table: an MCP server is a connector, and giving endpoints two
homes is how they drift apart.

What this adds over the Connectors page is the part that makes MCP legible: the tools a
server actually exposes, and a way to call one and see the result. A list of URLs tells
you nothing; a list of tools tells you what a model can now do.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.mcp_client import bundle_for_user, invalidate, mcp_rows, to_server
from src.routers.auth import require_permission

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["mcp"])

_READ = require_permission("connectors")
_WRITE = require_permission("connectors")


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = {}


@router.get("/servers")
async def list_servers(user: dict = Depends(_READ)):
    """Every MCP connector this user has registered, with its discovered tools.

    EVERY one — including a connector with no endpoint filled in yet. Those used to be
    dropped, so a half-configured server showed on the Connectors page and then simply
    was not here, leaving the user to compare two lists and guess which of their
    servers vanished. Three states now, and they are different problems:

        connected     dialled, tools discovered
        failed        has an endpoint, did not answer
        unconfigured  no endpoint yet — finish it on the Connectors page

    Tool discovery is cached, so this is cheap to poll.
    """
    user_id = user.get("userId", "")
    rows = mcp_rows(user_id)
    if not rows:
        return {"servers": [], "toolCount": 0, "degraded": []}

    servers = [s for s in (to_server(r) for r in rows) if s is not None]
    unconfigured = [r for r in rows if to_server(r) is None]

    bundle = await bundle_for_user(user_id)

    # Group discovered tools back under the server that supplied them.
    tools_by_server: dict[str, list[dict]] = {s.connector_id: [] for s in servers}
    for definition in bundle.defs:
        prefix = f"mcp__"
        for server in servers:
            if definition["name"].startswith(f"{prefix}{server.slug}__"):
                tools_by_server[server.connector_id].append({
                    "name": definition["name"],
                    "remoteName": definition["name"].split("__", 2)[-1],
                    "description": definition["description"],
                    "inputSchema": definition["input_schema"],
                })
                break

    degraded = set(bundle.degraded)
    listed = [{
        "connectorId": s.connector_id,
        "name": s.name,
        "slug": s.slug,
        "url": s.url,
        "transport": s.transport,
        "status": "failed" if s.name in degraded else "connected",
        "tools": tools_by_server.get(s.connector_id, []),
    } for s in servers]

    listed += [{
        "connectorId": r.get("connectorId", ""),
        "name": r.get("name") or "MCP server",
        "slug": "",
        "url": "",
        "transport": "",
        "status": "unconfigured",
        "tools": [],
    } for r in unconfigured]

    return {
        "servers": listed,
        "toolCount": len(bundle.defs),
        "degraded": sorted(degraded),
    }


@router.post("/servers/{connector_id}/refresh")
async def refresh_server(connector_id: str, user: dict = Depends(_WRITE)):
    """Drop the discovery cache and re-dial. The button for 'I just changed something'."""
    user_id = user.get("userId", "")
    invalidate(user_id)
    bundle = await bundle_for_user(user_id)
    return {"toolCount": len(bundle.defs), "degraded": bundle.degraded}


@router.post("/tools/call")
async def call_tool(body: ToolCallRequest, user: dict = Depends(_WRITE)):
    """Call one tool and return its raw result — the 'Try it' panel.

    Scoped to the caller's own servers by construction: the dispatch map is built from
    their connector rows, so a name they do not own simply is not in it.
    """
    import asyncio

    user_id = user.get("userId", "")
    bundle = await bundle_for_user(user_id)
    fn = bundle.dispatch.get(body.name)
    if fn is None:
        raise HTTPException(status_code=404,
                            detail=f"No MCP tool {body.name!r} for this user")

    # The dispatch callables are sync by design (the ReAct loop calls them from a
    # worker thread), so keep them off the event loop here too.
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: fn(**(body.arguments or {})))
    return {"name": body.name, "result": result}

"""Aura as an MCP client.

Public surface:
    bundle_for_user(user_id)  -> tool definitions + dispatch for a chat turn
    invalidate(user_id)       -> drop the discovery cache after a connector changes
    health_check(row)         -> (ok, message) for the connector Test button
"""
from __future__ import annotations

from src.mcp_client.registry import (
    Bundle, EMPTY, bundle_for_user, endpoint_of, invalidate, mcp_rows,
    servers_for_user, to_server,
)

__all__ = ["Bundle", "EMPTY", "bundle_for_user", "endpoint_of", "invalidate",
           "mcp_rows", "servers_for_user", "to_server", "health_check"]


def health_check(row: dict) -> tuple[bool, str]:
    """Test one connector row by doing a real MCP handshake.

    Sync, because the connectors router is a sync `def` route that FastAPI runs in a
    threadpool — so there is no running loop here and `asyncio.run` is safe.

    A liveness ping would not do: a URL that answers 200 and a URL that speaks MCP are
    different things, and the old generic branch could not tell them apart.
    """
    import asyncio

    from src.mcp_client import client

    # Built through to_server so the auth-token handling and the endpoint/baseUrl
    # fallback are defined in exactly one place. This used to construct its own
    # Server and so quietly ignored config.token.
    server = to_server(row)
    if server is None:
        return False, "No endpoint configured"

    try:
        tools = asyncio.run(
            asyncio.wait_for(client.list_tools(server), client.LIST_TIMEOUT))
    except asyncio.TimeoutError:
        return False, f"Timed out after {client.LIST_TIMEOUT:.0f}s"
    except Exception as exc:                          # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"

    if not tools:
        # Reachable and speaking MCP, but with nothing to offer. Worth reporting as a
        # success with a caveat rather than a failure — the connection is fine.
        return True, "Connected — but the server exposes no tools"

    names = ", ".join(t["name"] for t in tools[:4])
    more = f" (+{len(tools) - 4} more)" if len(tools) > 4 else ""
    return True, f"Connected — {len(tools)} tools: {names}{more}"

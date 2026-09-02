"""Per-user MCP tool discovery, caching, and dispatch.

Reads the caller's `type="mcp"` rows from `user-connectors` and turns them into
something a ReAct loop can use: a list of tool definitions and a dispatch map.

Two things worth knowing before changing anything here.

**Scoping is fail-closed.** No user id means no MCP tools. Connectors are per-user rows
(PK `userId`) whose configs are masked on read but stored in the clear, so there is
deliberately no "fall back to everyone's servers" path.

**Discovery is cached; dispatch is not.** `tools/list` is a network round trip per
server, and a chat turn must never pay for it — so it is cached with a short TTL and
served stale while it refreshes. Actual tool CALLS are never cached: they are the
user-visible action, and a stale answer there would be a lie.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from src.mcp_client import adapter, naming
from src.mcp_client.client import CALL_TIMEOUT, LIST_TIMEOUT, Server

log = logging.getLogger(__name__)

TTL_SECONDS = 60.0
DISCOVERY_BUDGET = 8.0        # hard ceiling on how long a cold turn may block
MAX_TOOLS_PER_SERVER = 40     # token-budget guard

_cache: dict[str, tuple[float, "Bundle"]] = {}
_lock = threading.Lock()


class Bundle:
    """One user's MCP tools, pinned for the life of a request.

    Definitions and routes travel together on purpose: if the cache refreshed midway
    through a turn, a name the model was just offered might no longer resolve.
    """

    __slots__ = ("defs", "dispatch", "servers", "degraded")

    def __init__(self, defs=None, dispatch=None, servers=None, degraded=None):
        self.defs: list[dict] = defs or []
        self.dispatch: dict[str, Callable[..., Any]] = dispatch or {}
        self.servers: dict[str, Server] = servers or {}
        self.degraded: list[str] = degraded or []

    def __bool__(self) -> bool:
        return bool(self.defs)


EMPTY = Bundle()


# ── Reading connectors ───────────────────────────────────────────────────────

def mcp_rows(user_id: str) -> list[dict]:
    """Every `type="mcp"` connector row for this user, configured or not.

    Deliberately unfiltered. A connector someone created and has not finished filling
    in must still be VISIBLE — it exists on the Connectors page, so a screen that
    silently omits it leaves the user comparing two lists and guessing which of their
    servers disappeared and why. `to_server` is what decides usability; this decides
    existence.
    """
    if not user_id:
        return []
    try:
        from src.database import dynamo_client as db
        # Signature is (table, pk_name, pk_value, ...) — see routers/connectors.py:98.
        rows = db.query_items("user-connectors", "userId", user_id, limit=200) or []
    except Exception as exc:                          # noqa: BLE001
        log.warning("MCP: could not read connectors for %s: %s", user_id, exc)
        return []
    return [r for r in rows if (r.get("type") or "") == "mcp"]


def endpoint_of(row: dict) -> str:
    """`endpoint` is what the API and McpConnector have always used; `baseUrl` is what
    the Connectors form actually writes (ConnectorsPage.tsx). Both are accepted, or
    a row created either way would be invisible to the other."""
    config = row.get("config") or {}
    return (config.get("endpoint") or config.get("baseUrl") or "").strip()


def to_server(row: dict) -> Server | None:
    """Build a usable Server, or None if the row is not configured enough to dial."""
    url = endpoint_of(row)
    if not url:
        return None

    config = row.get("config") or {}
    # The Connectors form offers an optional "Auth Token" and writes it to
    # config.token. Nothing was reading it, so a connector configured with a token in
    # the UI would authenticate with nothing at all and fail with a bare 401 from the
    # remote server — with the token sitting right there in the form.
    headers = dict(config.get("headers") or {})
    token = (config.get("token") or "").strip()
    if token and not any(k.lower() == "authorization" for k in headers):
        headers["Authorization"] = token if token.lower().startswith("bearer ") \
            else f"Bearer {token}"

    connector_id = row.get("connectorId", "")
    return Server(
        connector_id=connector_id,
        slug=row.get("mcpSlug") or naming.slug_for(row.get("name", ""), connector_id),
        name=row.get("name") or "MCP server",
        url=url,
        transport=(config.get("transport") or "auto"),
        headers=headers,
    )


def servers_for_user(user_id: str) -> list[Server]:
    """The user's DIALLABLE MCP servers. Never raises.

    Excludes rows with no endpoint — see mcp_rows() for the full list, which is what
    the UI should render.
    """
    return [s for s in (to_server(r) for r in mcp_rows(user_id)) if s is not None]


# ── Discovery ────────────────────────────────────────────────────────────────

async def _discover(server: Server) -> list[dict]:
    from src.mcp_client import client
    return await asyncio.wait_for(client.list_tools(server), LIST_TIMEOUT)


def _make_caller(server: Server, remote: str) -> Callable[..., Any]:
    """A SYNC callable, because that is what the ReAct loop needs.

    `react_orchestrator._call_tool` is sync and is invoked through
    `run_in_executor`, so this runs on a worker thread with no event loop of its own —
    which is precisely the situation `asyncio.run` is for. Everything the MCP SDK
    creates (transport, session, task groups) is created and destroyed inside this one
    call, so nothing is ever shared across event loops. That is what makes the
    connect-per-call design in client.py safe as well as simple.
    """
    def call(**kwargs) -> dict:
        from src.mcp_client import client
        try:
            result = asyncio.run(
                asyncio.wait_for(client.call_tool(server, remote, kwargs), CALL_TIMEOUT))
        except asyncio.TimeoutError:
            return {"error": f"{server.name}: '{remote}' timed out after {CALL_TIMEOUT:.0f}s"}
        except Exception as exc:                      # noqa: BLE001
            # Every failure reaches the model as a normal tool result. A remote server
            # being down must degrade the answer, not end the conversation.
            return {"error": f"{server.name}: {type(exc).__name__}: {str(exc)[:300]}"}
        if not result.get("ok"):
            return {"error": f"{remote} reported an error",
                    "detail": (result.get("text") or "")[:4000]}
        return {"result": (result.get("text") or "")[:12000]}

    return call


async def _build(user_id: str, taken: set[str]) -> Bundle:
    servers = servers_for_user(user_id)
    if not servers:
        return EMPTY

    results = await asyncio.gather(*(_discover(s) for s in servers),
                                   return_exceptions=True)

    defs: list[dict] = []
    dispatch: dict[str, Callable[..., Any]] = {}
    by_id: dict[str, Server] = {}
    degraded: list[str] = []

    for server, result in zip(servers, results):
        if isinstance(result, BaseException):
            log.info("MCP %s unreachable: %s", server.name, result)
            degraded.append(server.name)
            continue
        by_id[server.connector_id] = server

        for tool in result[:MAX_TOOLS_PER_SERVER]:
            remote = tool.get("name") or ""
            name = naming.tool_name(server.slug, remote)
            if not name or name in taken:
                log.info("MCP %s: dropping %r (unnameable or duplicate)",
                         server.name, remote)
                continue
            definition = adapter.to_tool_def(
                server.name, name, remote,
                tool.get("description", ""), tool.get("inputSchema"))
            if definition is None:
                continue
            taken.add(name)
            defs.append(definition)
            dispatch[name] = _make_caller(server, remote)

    return Bundle(defs, dispatch, by_id, degraded)


async def bundle_for_user(user_id: str, reserved: set[str] | None = None) -> Bundle:
    """The user's MCP tools. Never raises, never blocks past DISCOVERY_BUDGET.

    `reserved` is the set of names already in use by built-in tools, so an MCP server
    cannot shadow one.
    """
    if not user_id:
        return EMPTY

    now = time.monotonic()
    with _lock:
        hit = _cache.get(user_id)
    if hit and (now - hit[0]) < TTL_SECONDS:
        return hit[1]

    try:
        bundle = await asyncio.wait_for(
            _build(user_id, set(reserved or ())), DISCOVERY_BUDGET)
    except Exception as exc:                          # noqa: BLE001
        log.warning("MCP discovery for %s failed: %s", user_id, exc)
        bundle = hit[1] if hit else EMPTY             # serve stale rather than nothing
    with _lock:
        _cache[user_id] = (time.monotonic(), bundle)
    return bundle


def invalidate(user_id: str = "") -> None:
    """Called whenever a connector is created, updated, deleted or re-tested."""
    with _lock:
        _cache.pop(user_id, None) if user_id else _cache.clear()

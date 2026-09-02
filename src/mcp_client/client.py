"""The MCP wire client — the only module in Aura that imports `mcp`.

That constraint is load-bearing. `src/main.py` imports `advisor.orchestrator`, which
imports `tools.registry`, at module scope; anything on an import chain like that which
touches `mcp` makes the whole app fail to boot wherever the package is missing. So the
import here lives inside a function, and nothing else in the codebase imports `mcp`.

Connect-per-call, not a cached session. A live `ClientSession` is an async context
manager over anyio task groups: keeping one alive across calls means entering it in a
task that then parks, and handing the session to callers on other tasks and possibly
other event loops. It is the most bug-prone pattern in this ecosystem, and the thing it
would save — the handshake — is only paid on actual tool calls, because `tools/list` is
cached in registry.py. The trade-off is that a server storing state across calls will
misbehave; ours do not, and that is documented.
"""
from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import urlparse

log = logging.getLogger(__name__)

LIST_TIMEOUT = 10.0
CALL_TIMEOUT = 30.0


@dataclass(frozen=True)
class Server:
    connector_id: str
    slug: str
    name: str
    url: str
    transport: str = "auto"          # auto | streamable-http | sse
    headers: dict[str, str] = field(default_factory=dict)


class McpUnavailable(RuntimeError):
    """The `mcp` package is not installed, or no transport worked."""


class McpBlocked(RuntimeError):
    """The endpoint is refused by the SSRF guard."""


# ── SSRF guard ───────────────────────────────────────────────────────────────
# The endpoint is a user-supplied URL that the BACKEND dereferences, from inside the
# VPC. That is a genuine new attack surface: 169.254.169.254 is the ECS task-role
# credential endpoint, and a stored connector row would reach it on every chat turn.
#
# Private ranges are deliberately ALLOWED — the sample servers are VPC-internal and
# reached by Service Connect alias, so blocking RFC1918 would block the feature. The
# range that actually matters is link-local, and that is blocked unconditionally.

def _guard_url(url: str) -> None:
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise McpBlocked(f"unsupported scheme {parsed.scheme!r}; use http or https")
    host = parsed.hostname or ""
    if not host:
        raise McpBlocked("no host in endpoint URL")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise McpBlocked(f"cannot resolve {host!r}: {exc}") from exc

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_link_local:
            raise McpBlocked(
                f"{host!r} resolves to link-local {addr} — that is the instance "
                f"metadata endpoint, refusing")


@contextlib.asynccontextmanager
async def _session(server: Server) -> AsyncIterator[Any]:
    _guard_url(server.url)
    try:
        from mcp import ClientSession
    except ImportError as exc:                       # local dev without the package
        raise McpUnavailable("the `mcp` package is not installed") from exc

    order = (["streamable-http", "sse"] if server.transport in ("", "auto")
             else [server.transport])
    last: Exception | None = None

    for transport in order:
        try:
            if transport == "streamable-http":
                from mcp.client.streamable_http import streamablehttp_client
                cm = streamablehttp_client(server.url, headers=server.headers or None)
            else:
                from mcp.client.sse import sse_client
                cm = sse_client(server.url, headers=server.headers or None)
        except ImportError as exc:                   # SDK older than the transport
            last = exc
            continue

        try:
            async with cm as streams:
                # streamablehttp_client yields (read, write, get_session_id);
                # sse_client yields (read, write). Index rather than unpack so an
                # SDK minor bump that adds a third element does not break this.
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session
                    return
        except Exception as exc:                     # noqa: BLE001 — try the next one
            last = exc
            log.info("MCP %s: %s failed (%s)", server.slug, transport, _explain(exc))
            continue

    raise McpUnavailable(f"cannot reach {server.name}: {_explain(last)}")


def _explain(exc: BaseException | None) -> str:
    """Unwrap anyio's ExceptionGroup to the cause a human can act on.

    The MCP SDK runs its transport in an anyio task group, so a plain HTTP 503 surfaces
    as "unhandled errors in a TaskGroup (1 sub-exception)" — which tells an operator
    nothing at all, and is what they will see in the connector Test result and in the
    model's tool output. Dig out the innermost real exception instead.
    """
    seen = 0
    while exc is not None and seen < 5:
        inner = getattr(exc, "exceptions", None)          # ExceptionGroup
        if not inner:
            break
        exc = inner[0]
        seen += 1
    if exc is None:
        return "unknown error"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def list_tools(server: Server) -> list[dict]:
    """Raises on failure. The caller decides that a failure means 'zero tools'."""
    async with _session(server) as session:
        result = await session.list_tools()
    return [{"name": t.name,
             "description": t.description or "",
             "inputSchema": t.inputSchema or {}} for t in result.tools]


async def call_tool(server: Server, tool: str, args: dict) -> dict:
    """{"ok": bool, "text": str, "blocks": list[str]}.

    `blocks` is kept separately from `text`, and that is not redundancy. A tool that
    returns a LIST comes back as one content block per item — FastMCP does this, so
    every Python MCP server does — and joining those with newlines produces a run of
    concatenated JSON objects that no JSON parser will accept. Callers that want data
    (the graph loader) read `blocks`; callers that want prose (the model) read `text`.
    """
    async with _session(server) as session:
        result = await session.call_tool(tool, args or {})

    blocks: list[str] = []
    for block in (result.content or []):
        text = getattr(block, "text", None)
        if text:
            blocks.append(text)
        else:
            # Images and embedded resources. Describing them beats dropping them
            # silently — the model can then say what it could not read.
            blocks.append(f"[unsupported MCP content: {type(block).__name__}]")

    return {"ok": not getattr(result, "isError", False),
            "text": "\n".join(blocks), "blocks": blocks}

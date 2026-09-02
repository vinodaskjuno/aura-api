"""Tool naming for MCP tools merged into a model's tool list.

This module exists because of one constraint that is easy to miss and expensive to get
wrong. Model providers limit tool names:

    Bedrock   ToolName: [a-zA-Z][a-zA-Z0-9_]*   max 64   -- NO HYPHENS
    Anthropic                                    max 64

and **the client library does not check them**. botocore validates only minimum string
length; the pattern and the maximum are enforced by the service. So an illegal name is
not a local error you can catch around one tool — it is a ValidationException on the
whole request, which means the turn dies before a single token streams and the caller
loses every built-in tool too.

The obvious scheme, `mcp__{connectorId}__{tool}`, is illegal on both counts: a
connectorId is a uuid4, so 36 characters with four hyphens, and
`mcp__<uuid>__list_deploys` is 55-75 characters of hyphenated string.

Hence a short SLUG per server, allocated once at registration and stored on the
connector row. Names built from it are checked here and dropped if they cannot be made
legal — a missing tool is a small disappointment, a rejected request is an outage.
"""
from __future__ import annotations

import hashlib
import re

PREFIX = "mcp__"

# The intersection of what Bedrock and Anthropic accept.
LEGAL_TOOL = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
LEGAL_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,11}$")

MAX_TOOL_NAME = 64
MAX_SLUG = 12


def slug_for(name: str, connector_id: str) -> str:
    """A short, stable, legal identifier for one server.

    Deterministic so that re-registering the same server produces the same tool names,
    and suffixed with three hex of the connector id so two servers a user happens to
    call the same thing cannot collide.
    """
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:8]
    if not base or not base[0].isalpha():
        base = f"s{base}"[:8]
    suffix = re.sub(r"[^a-f0-9]", "", (connector_id or "").lower())[:3] or "000"
    slug = f"{base}_{suffix}"[:MAX_SLUG]
    return slug if LEGAL_SLUG.match(slug) else f"srv_{suffix}"


def tool_name(slug: str, remote: str) -> str:
    """Namespaced, sanitised, length-capped. Empty string means "unusable — drop it".

    Lossy on purpose: hyphens and dots become underscores and long names are truncated
    with a hash suffix. That is safe because dispatch goes through a route map, not by
    reversing this transformation.
    """
    body = re.sub(r"[^a-zA-Z0-9_]+", "_", remote or "").strip("_") or "tool"
    name = f"{PREFIX}{slug}__{body}"

    if len(name) > MAX_TOOL_NAME:
        digest = hashlib.sha1((remote or "").encode("utf-8")).hexdigest()[:6]
        room = MAX_TOOL_NAME - len(PREFIX) - len(slug) - 2 - 1 - len(digest)
        name = f"{PREFIX}{slug}__{body[:max(room, 1)]}_{digest}"

    return name if LEGAL_TOOL.match(name) else ""


def is_mcp_tool(name: str) -> bool:
    return bool(name) and name.startswith(PREFIX)

"""Turn an MCP `tools/list` entry into a tool definition a model will accept.

Two jobs, and the second matters more than it looks.

1. Shape. MCP and Anthropic are almost the same — `{name, description, inputSchema}`
   versus `{name, description, input_schema}` — so this is a rename.

2. Sanitising. MCP servers emit whatever JSON Schema their framework generates, and
   FastMCP (which most Python servers use, including Aura's own mock) generates
   pydantic schemas full of `title` and `$defs`. Some of that is merely wasteful; some
   of it is rejected. A rejected tool definition fails the WHOLE request, taking every
   other tool with it, so anything this module cannot express confidently is dropped
   rather than forwarded.

The empty-description rule is the sharpest edge here: MCP declares `description` as
optional, botocore declares it `NonEmptyString`, and a server that omits one would
otherwise raise ParamValidationError locally and kill the turn.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

MAX_SCHEMA_BYTES = 16_384
MAX_DEPTH = 6
MAX_DESCRIPTION = 2048

# Dropped because they are noise (pure token cost) or because they are structurally
# risky across providers. Anything NOT listed is passed through: type, properties,
# required, items, enum, const, default, description, minimum/maximum, minLength/
# maxLength, additionalProperties, anyOf/oneOf/allOf.
_DROP_KEYS = {
    "$schema", "$id", "$anchor", "$comment", "$defs", "definitions",
    "title", "examples",
    "unevaluatedProperties", "unevaluatedItems",
    "dependentSchemas", "dependentRequired",
    "if", "then", "else", "patternProperties", "propertyNames",
    "contentEncoding", "contentMediaType",
    "readOnly", "writeOnly", "deprecated",
}


def sanitise_schema(raw: dict | None) -> dict | None:
    """Return a schema safe to send, or None meaning "drop this tool".

    None rather than a fallback empty schema on purpose: a tool advertised with the
    wrong arguments is worse than a tool that is absent, because the model will call it
    and get errors it cannot diagnose.
    """
    if raw is not None and not isinstance(raw, dict):
        return None

    raw = raw or {}
    defs = {**(raw.get("$defs") or {}), **(raw.get("definitions") or {})}
    cleaned = _walk(raw, defs, 0, frozenset())

    # Providers require the top level to be an object with properties.
    if not isinstance(cleaned, dict) or cleaned.get("type") != "object":
        cleaned = {"type": "object", "properties": {}}
    cleaned.setdefault("properties", {})
    if not cleaned.get("required"):
        cleaned.pop("required", None)

    try:
        size = len(json.dumps(cleaned, default=str))
    except (TypeError, ValueError):
        return None
    if size > MAX_SCHEMA_BYTES:
        return None
    return cleaned


def _walk(node, defs: dict, depth: int, seen: frozenset) -> dict:
    if not isinstance(node, dict):
        return {}
    if depth > MAX_DEPTH:
        return {"type": "object"}

    # Inline local $refs and then drop $defs entirely. Support for $ref varies by
    # provider and by model; inlining removes the question. A ref we have already
    # followed is a recursive schema, which cannot be inlined at all.
    ref = node.get("$ref")
    if isinstance(ref, str):
        key = ref.rsplit("/", 1)[-1]
        target = defs.get(key)
        if key in seen or not isinstance(target, dict):
            return {"type": "object"}
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return _walk(merged, defs, depth + 1, seen | {key})

    out: dict = {}
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        if key == "type":
            # `type: ["string", "null"]` — union types are not reliably accepted.
            if isinstance(value, list):
                concrete = [t for t in value if t != "null"]
                out["type"] = concrete[0] if concrete else "string"
            else:
                out["type"] = value
        elif key == "description":
            if isinstance(value, str) and value.strip():
                out["description"] = value[:MAX_DESCRIPTION]
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {
                k: _walk(v, defs, depth + 1, seen)
                for k, v in value.items() if isinstance(v, dict)
            }
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            out[key] = _walk(value, defs, depth + 1, seen)
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            out[key] = [_walk(v, defs, depth + 1, seen)
                        for v in value if isinstance(v, dict)]
        else:
            out[key] = value
    return out


def to_tool_def(server_name: str, tool_name_: str, remote: str,
                description: str, input_schema: dict | None) -> dict | None:
    """One Anthropic-format tool definition, or None if it must be dropped."""
    schema = sanitise_schema(input_schema)
    if schema is None:
        log.info("MCP %s: dropping %r — unusable inputSchema", server_name, remote)
        return None

    # NEVER "". Empty is a client-side validation error that fails the whole request,
    # and MCP servers are entitled to omit a description.
    text = (description or "").strip() or f"{remote} — a tool on the {server_name} MCP server"

    return {
        "name": tool_name_,
        "description": text[:MAX_DESCRIPTION],
        "input_schema": schema,
    }

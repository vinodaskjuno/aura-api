"""Load a knowledge graph from registered MCP servers.

This replaces what the Data Loader's "MCP Connector" tab used to do, which was call a
`random.seed()` generator and label the result "MCP". The tab had eight hardcoded source
names and the selection was never even passed to the loader — the tick-boxes were
decorative. Now the sources are the user's real servers and the data is really theirs.

**The mapping contract.** Turning arbitrary MCP output into graph nodes needs a rule, and
there are two layers so this works against servers we do not control:

1. *Inferred* — any tool returning a list of objects becomes nodes. The label comes from
   the tool name (`list_services` -> `Service`) when that matches Aura's ontology
   vocabulary; the external id from `id`, then `externalId`, then `name`.
2. *Declared* — a record may carry `_label` and `_links`, which win over inference. Aura's
   own sample servers declare them, which is how the demo graph gets real relationships
   instead of disconnected islands.

Anything that fits neither is **skipped and counted**, never silently dropped. A loader
that quietly discards half its input is worse than one that fails, because nobody finds
out until someone asks the graph a question and gets a confident wrong answer.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

log = logging.getLogger(__name__)

# Tools whose names suggest they enumerate entities. Calling everything would be wrong:
# a `delete_order` tool is not an ingestion source, and MCP gives no machine-readable
# way to tell a reader from a writer.
_LISTER = re.compile(r"^(list|get_all|search|fetch|export)_(.+)$")

_SKIP_KEYS = {"_label", "_links"}


def _labels() -> set[str]:
    try:
        from src.ontology.schema import ALL_LABELS
        return set(ALL_LABELS)
    except Exception:                                     # noqa: BLE001
        return set()


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses") or word.endswith("shes"):
        return word[:-2]
    return word[:-1] if word.endswith("s") else word


def infer_label(tool_name: str, known: set[str]) -> str:
    """`list_services` -> `Service`, if the ontology knows that label. Else "".

    Returning "" rather than inventing a label is deliberate: an unrecognised label
    would create node types nothing else in Aura can query or render.
    """
    match = _LISTER.match(tool_name or "")
    if not match:
        return ""
    candidate = _singular(match.group(2))
    pascal = "".join(part.capitalize() for part in candidate.split("_"))
    if pascal in known:
        return pascal
    lowered = {label.lower(): label for label in known}
    return lowered.get(pascal.lower(), "")


def _external_id(record: dict, label: str) -> str:
    for key in ("externalId", "id", "name"):
        value = record.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return f"{label.lower()}:{str(value).strip()}"
    return ""


def _props(record: dict) -> dict[str, Any]:
    """Flatten to scalars. Neo4j properties cannot hold nested maps or lists of maps,
    and a silent driver error here would abort the whole load."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key in _SKIP_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list) and all(
                isinstance(v, (str, int, float, bool)) for v in value):
            out[key] = value
        # anything else (nested dicts, lists of dicts) is dropped on purpose
    return out


def ingest_from_mcp(
    user_id: str,
    connector_ids: list[str] | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Pull every listable tool from the selected servers into the graph.

    Returns the same stat keys the Data Loader already expects, plus `skipped` and
    `sources` so the UI can say what it could not map.
    """
    import asyncio

    from src.graph import neo4j_client as neo4j
    from src.mcp_client import client, servers_for_user

    # Sync by design: it drives the async MCP client with asyncio.run() and writes to
    # Neo4j with blocking calls, so it belongs on a worker thread. Called from a
    # coroutine it would fail deep inside the first tool call with "asyncio.run()
    # cannot be called from a running event loop", reported per-server as if the
    # SERVER had failed — which is exactly how this was found. Fail clearly instead.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "ingest_from_mcp is synchronous — call it via run_in_executor, "
            "not directly from a coroutine (see routers/ontology_loader.py)")

    known = _labels()
    servers = servers_for_user(user_id)
    if connector_ids:
        wanted = set(connector_ids)
        servers = [s for s in servers if s.connector_id in wanted]
    if not servers:
        return {"nodes_added": 0, "rels_added": 0, "skipped": 0,
                "sources": [], "errors": ["no MCP servers selected"]}

    from src.graph import provenance

    # ensure_run: the Data Loader route already opens a run and should own these
    # writes; anything calling this directly must still be attributed.
    server_names = ", ".join(sv.name for sv in servers)
    with provenance.ensure_run(
        provenance.PIPELINE_MCP,
        trigger=provenance.TRIGGER_MANUAL,
        source="mcp",
        sourceDetail=server_names,
        connectorIds=[sv.connector_id for sv in servers],
        writtenBy="mcp_client.ingest",
    ):
        return _ingest_servers(servers, known, emit_target=progress)


def _ingest_servers(servers, known, emit_target=None) -> dict:
    """The load itself. Split out so `ingest_from_mcp` is just guards + the run."""
    import asyncio
    import re

    from src.graph import neo4j_client as neo4j
    from src.graph import provenance
    from src.mcp_client import client
    progress = emit_target

    nodes = rels = skipped = 0
    sources: list[dict] = []
    errors: list[str] = []
    pending_links: list[tuple[str, str, str]] = []

    def emit(event: dict) -> None:
        if progress:
            try:
                progress(event)
            except Exception:                             # noqa: BLE001
                pass

    for server in servers:
        # refine, not a nested run: the servers are one load, and giving each its
        # own runId would break "show me everything this run wrote". The node still
        # records which server it came from.
        with provenance.refine(source=f"mcp:{server.slug}",
                               sourceDetail=server.name,
                               sourceRecordId=server.connector_id):
            emit({"server": server.name, "phase": "discovering"})
            try:
                tools = asyncio.run(
                    asyncio.wait_for(client.list_tools(server), client.LIST_TIMEOUT))
            except Exception as exc:                          # noqa: BLE001
                errors.append(f"{server.name}: {type(exc).__name__}: {str(exc)[:160]}")
                sources.append({"server": server.name, "nodes": 0, "skipped": 0,
                                "error": str(exc)[:160]})
                continue

            server_nodes = server_skipped = 0

            for tool in tools:
                remote = tool.get("name") or ""
                inferred = infer_label(remote, known)
                # Only tools that take no required arguments are safe to call blind.
                required = (tool.get("inputSchema") or {}).get("required") or []
                if required:
                    continue
                if not inferred and not _LISTER.match(remote):
                    continue

                emit({"server": server.name, "phase": "reading", "tool": remote})
                try:
                    result = asyncio.run(
                        asyncio.wait_for(client.call_tool(server, remote, {}),
                                         client.CALL_TIMEOUT))
                except Exception as exc:                      # noqa: BLE001
                    errors.append(f"{server.name}/{remote}: {str(exc)[:120]}")
                    continue
                if not result.get("ok"):
                    continue

                records = _parse(result.get("blocks") or [result.get("text", "")])
                if records is None:
                    continue

                for record in records:
                    if not isinstance(record, dict):
                        server_skipped += 1
                        continue
                    label = record.get("_label") or inferred
                    if not label or label not in known:
                        server_skipped += 1
                        continue
                    eid = _external_id(record, label)
                    if not eid:
                        server_skipped += 1
                        continue

                    props = _props(record)
                    props["name"] = props.get("name") or props.get("id") or eid
                    # Tagged so a later wipe can find exactly what MCP put here, the same
                    # way the synthetic loader tags its own nodes with `source`.
                    props["source"] = f"mcp:{server.slug}"
                    props["mcpServer"] = server.name
                    try:
                        # traced_upsert so an MCP-loaded node gets a CREATE entry
                        # in its timeline, not just a lineage ribbon.
                        provenance.traced_upsert(label, eid, props)
                        nodes += 1
                        server_nodes += 1
                    except Exception as exc:                  # noqa: BLE001
                        errors.append(f"{label} {eid}: {str(exc)[:120]}")
                        continue

                    for link in (record.get("_links") or []):
                        target_label = link.get("to_label", "")
                        target = link.get("to", "")
                        rel = re.sub(r"[^A-Z_]", "", (link.get("type") or "").upper())
                        if target_label and target and rel:
                            pending_links.append(
                                (eid, f"{target_label.lower()}:{target}", rel))

                emit({"server": server.name, "phase": "wrote", "tool": remote,
                      "nodes": server_nodes})

            skipped += server_skipped
            sources.append({"server": server.name, "nodes": server_nodes,
                            "skipped": server_skipped})

    # Relationships last: a link can point at a node another tool has not written yet,
    # and MATCH on both ends only succeeds once both exist.
    for from_eid, to_eid, rel in pending_links:
        try:
            if neo4j.link_nodes_by_eid(from_eid, to_eid, rel):
                rels += 1
        except Exception:                                 # noqa: BLE001
            continue

    emit({"phase": "done", "nodes": nodes, "rels": rels, "skipped": skipped})
    return {"nodes_added": nodes, "nodes_updated": 0, "rels_added": rels,
            "total_nodes": nodes, "skipped": skipped, "sources": sources,
            "errors": errors[:20]}


def _parse(blocks: list[str]) -> list | None:
    """MCP returns tool output as text blocks. Ours is JSON; not everyone's will be.

    A list-returning tool arrives as one JSON object PER BLOCK, so each block is parsed
    on its own. A single block may still hold a JSON array, or an envelope like
    {"items": [...]}, so both are handled too.
    """
    import json

    records: list = []
    for block in blocks:
        if not isinstance(block, str) or not block.strip():
            continue
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue                       # prose, not data — not this tool's job
        if isinstance(data, list):
            records.extend(data)
        elif isinstance(data, dict):
            for key in ("items", "results", "data", "records"):
                if isinstance(data.get(key), list):
                    records.extend(data[key])
                    break
            else:
                records.append(data)
    return records or None

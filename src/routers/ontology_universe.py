"""Enterprise Ontology Universe API — Neo4j-backed endpoints.

Routes:
  POST /api/ontology/load                        Trigger full MCP ingestion
  GET  /api/ontology/org-graph                   Full org graph (filterable)
  GET  /api/ontology/project/{name}              Project subgraph (2-hop BFS)
  GET  /api/ontology/search                      Full-text node search
  POST /api/ontology/nodes                       Create new node (maintainer)
  PUT  /api/ontology/nodes/{id}                  Update node property (maintainer)
  POST /api/ontology/nodes/{id}/relationships    Add relationship (maintainer)
  POST /api/ontology/relationships/{id}/archive  Archive relationship (maintainer)
  GET  /api/ontology/audit-log                   Paginated Neo4j audit history (maintainer)
  GET  /api/ontology/changelog                   DynamoDB versioning log (maintainer)
  GET  /api/ontology/nodes/{id}/changelog        Per-node version history
  GET  /api/ontology/relationships/{id}/changelog  Per-relationship version history
  WS   /api/ontology/ws/chat                     Maintainer chat with human-in-the-loop
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from src.routers.auth import get_current_user, require_permission
from src.graph import neo4j_client as neo4j
from src.database import dynamo_client as dynamo

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ontology", tags=["ontology-universe"])


# ── Models ─────────────────────────────────────────────────────────────────────

class LoadRequest(BaseModel):
    delta_since: str | None = None


class UpdateNodeRequest(BaseModel):
    prop: str
    value: Any


class AddRelationshipRequest(BaseModel):
    to_label: str
    to_external_id: str
    rel_type: str
    props: dict[str, Any] | None = None


class CreateNodeRequest(BaseModel):
    label: str
    name: str
    props: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Moved to dynamo_client so services can write history without importing a router.
# Re-exported under the original name to keep this module's call sites unchanged.
_build_changelog_entry = dynamo.build_changelog_entry


# ── Load endpoint ──────────────────────────────────────────────────────────────

@router.post("/load")
def load_ontology(
    body: LoadRequest | None = None,
    user: dict = Depends(require_permission("ontology_maintain")),
):
    """Trigger full MCP ingestion into Neo4j.  Long-running — runs synchronously for now."""
    if not neo4j.is_available():
        raise HTTPException(
            status_code=503,
            detail="Neo4j is not available. Ensure neo4j_enabled=true in .env and Neo4j is running."
        )
    from src.graph import provenance
    delta_since = (body.delta_since if body else None)
    with provenance.trace_run(
        provenance.PIPELINE_MCP,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"], actorId=user.get("userId", ""),
        source="bulk_load",
        sourceDetail=f"full load, delta since {delta_since or 'never'}",
        writtenBy="ontology_universe.load_ontology",
    ):
        from src.connectors.ingestion_service import run_full_load
        try:
            result = run_full_load(delta_since=delta_since)
        except Exception as exc:
            log.exception("Ontology load failed")
            raise HTTPException(status_code=500, detail=str(exc))

        # Version as one BULK_LOAD event — not per-entity (Option A decision)
        try:
            node_count = len(result.get("nodes", result)) if isinstance(result, dict) else 0
            dynamo.write_changelog(_build_changelog_entry(
                entity_id="bulk-load",
                entity_type="BulkLoad",
                entity_label="BulkLoad",
                entity_name="Full MCP Ingestion",
                change_type="BULK_LOAD",
                actor=user["username"],
                before=None,
                after={"node_count": node_count, "delta_since": delta_since},
                source="bulk_load",
                notes=f"Full MCP ingestion triggered by {user['username']}",
            ))
        except Exception:
            pass
        return result


# ── Graph query endpoints ──────────────────────────────────────────────────────

@router.get("/org-graph")
def get_org_graph(
    types: str | None = None,
    sources: str | None = None,
    limit: int = 5000,
    _: dict = Depends(get_current_user),
):
    """Return full org graph nodes + links.  Filterable by type and source."""
    if not neo4j.is_available():
        return {"nodes": [], "links": [], "warning": "Neo4j not available — no data"}
    type_filter = [t.strip() for t in types.split(",")] if types else None
    source_filter = [s.strip() for s in sources.split(",")] if sources else None
    try:
        return neo4j.get_org_graph(type_filter=type_filter, source_filter=source_filter, limit=limit)
    except ValueError as exc:
        # Unrecognised label in ?types= — client error, not a server fault.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("get_org_graph failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/project/{name}")
def get_project_subgraph(name: str, hops: int = 2, _: dict = Depends(get_current_user)):
    """Return a 2-hop subgraph rooted at the named project."""
    if not neo4j.is_available():
        return {"nodes": [], "links": [], "warning": "Neo4j not available"}
    try:
        return neo4j.get_project_subgraph(project_name=name, hops=hops)
    except Exception as exc:
        log.exception("get_project_subgraph failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/nodes/{node_id}/subgraph")
def get_node_subgraph(node_id: str, hops: int = 2, _: dict = Depends(get_current_user)):
    """Return a subgraph centered on a specific node by ID."""
    if not neo4j.is_available():
        return {"nodes": [], "links": [], "warning": "Neo4j not available"}
    try:
        return neo4j.get_node_subgraph(node_id=node_id, hops=hops)
    except Exception as exc:
        log.exception("get_node_subgraph failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/search")
def search_nodes(
    q: str,
    type: str | None = None,
    limit: int = 20,
    _: dict = Depends(get_current_user),
):
    """Full-text node search by name, externalId, or hostname."""
    if not neo4j.is_available():
        return []
    try:
        return neo4j.search_nodes(query=q, node_type=type, limit=limit)
    except Exception as exc:
        log.exception("search_nodes failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Maintainer mutation endpoints ──────────────────────────────────────────────

@router.post("/nodes")
def create_node(
    body: CreateNodeRequest,
    user: dict = Depends(require_permission("ontology_maintain")),
):
    """Create a new node with a generated externalId.  Writes Neo4j audit + DynamoDB changelog."""
    from src.graph import provenance
    with provenance.trace_run(
        provenance.PIPELINE_MANUAL,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"], actorId=user.get("userId", ""),
        source="manual",
        sourceDetail=f"created {body.label} \u201c{body.name}\u201d in Onto Verse",
        writtenBy="ontology_universe.create_node",
    ):
        if not neo4j.is_available():
            raise HTTPException(status_code=503, detail="Neo4j not available")
        try:
            node = neo4j.create_node(body.label, body.name, body.props or {}, user["username"])
        except Exception as exc:
            log.exception("create_node failed")
            raise HTTPException(status_code=500, detail=str(exc))
        entity_id = node.get("elementId") or node.get("id", "unknown")
        try:
            dynamo.write_changelog(_build_changelog_entry(
                entity_id=str(entity_id),
                external_id=node.get("externalId"),
                entity_type="Node",
                entity_label=body.label,
                entity_name=body.name,
                change_type="CREATE",
                actor=user["username"],
                before=None,
                after={**(body.props or {}), "name": body.name, "label": body.label},
                source="api",
                notes=f"Node created via API by {user['username']}",
            ))
        except Exception:
            pass
        return {"ok": True, "node": node}


@router.put("/nodes/{node_id}")
def update_node(
    node_id: str,
    body: UpdateNodeRequest,
    user: dict = Depends(require_permission("ontology_maintain")),
):
    """Update a single property on any node.  Writes Neo4j AuditLog + DynamoDB changelog."""
    from src.graph import provenance
    with provenance.trace_run(
        provenance.PIPELINE_MANUAL,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"], actorId=user.get("userId", ""),
        source="manual",
        sourceDetail=f"edited {body.prop} in Onto Verse",
        writtenBy="ontology_universe.update_node",
    ):
        if not neo4j.is_available():
            raise HTTPException(status_code=503, detail="Neo4j not available")
        existing = neo4j.get_node_by_id(node_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Node {node_id!r} not found")
        before = existing.get(body.prop)
        ok = neo4j.update_node_property(node_id, body.prop, body.value)
        if not ok:
            raise HTTPException(status_code=500, detail="Update failed")
        entity_name = existing.get("name") or existing.get("externalId") or node_id
        entity_label = existing["labels"][0] if existing.get("labels") else "Unknown"
        try:
            neo4j.write_audit_log(
                actor=user["username"],
                action=f"UPDATE_PROPERTY:{body.prop}",
                target_id=node_id,
                before=before,
                after=body.value,
            )
        except Exception:
            pass
        try:
            dynamo.write_changelog(_build_changelog_entry(
                entity_id=node_id,
                external_id=existing.get("externalId"),
                entity_type="Node",
                entity_label=entity_label,
                entity_name=entity_name,
                change_type="UPDATE",
                actor=user["username"],
                before={body.prop: before},
                after={body.prop: body.value},
                source="api",
                notes=f"{body.prop}: {before!r} → {body.value!r}",
            ))
        except Exception:
            pass
        return {"ok": True, "node_id": node_id, "prop": body.prop, "value": body.value}


@router.post("/nodes/{node_id}/relationships")
def add_relationship(
    node_id: str,
    body: AddRelationshipRequest,
    user: dict = Depends(require_permission("ontology_maintain")),
):
    """Add a relationship from a node to another.  Writes Neo4j AuditLog + DynamoDB changelog."""
    from src.graph import provenance
    with provenance.trace_run(
        provenance.PIPELINE_MANUAL,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"], actorId=user.get("userId", ""),
        source="manual",
        sourceDetail=f"added {body.rel_type} relationship in Onto Verse",
        writtenBy="ontology_universe.add_relationship",
    ):
        if not neo4j.is_available():
            raise HTTPException(status_code=503, detail="Neo4j not available")
        node = neo4j.get_node_by_id(node_id)
        if not node:
            raise HTTPException(status_code=404, detail=f"Source node {node_id!r} not found")
        from_label = node["labels"][0] if node.get("labels") else "Unknown"
        from_eid = node.get("externalId", node_id)
        ok = neo4j.upsert_relationship(from_label, from_eid, body.to_label, body.to_external_id, body.rel_type, body.props)
        if not ok:
            raise HTTPException(status_code=500, detail="Relationship creation failed")
        entity_name = node.get("name") or node.get("externalId") or node_id
        try:
            neo4j.write_audit_log(
                actor=user["username"],
                action=f"ADD_RELATIONSHIP:{body.rel_type}",
                target_id=node_id,
                before=None,
                after={"to": body.to_external_id, "type": body.rel_type},
            )
        except Exception:
            pass
        try:
            dynamo.write_changelog(_build_changelog_entry(
                entity_id=node_id,
                # A relationship has no externalId of its own; key its history to the
                # source node so it stays with something portable.
                external_id=from_eid,
                entity_type="Relationship",
                entity_label=body.rel_type,
                entity_name=f"{entity_name} → {body.to_external_id}",
                change_type="RELATIONSHIP_ADD",
                actor=user["username"],
                before=None,
                after={"from": from_eid, "to": body.to_external_id, "type": body.rel_type},
                source="api",
                notes=f"Added {body.rel_type} from {entity_name} to {body.to_external_id}",
            ))
        except Exception:
            pass
        return {"ok": True}


@router.post("/relationships/{rel_id}/archive")
def archive_relationship(
    rel_id: str,
    user: dict = Depends(require_permission("ontology_maintain")),
):
    """Soft-delete a relationship (sets active=false).  Writes Neo4j AuditLog + DynamoDB changelog."""
    from src.graph import provenance
    with provenance.trace_run(
        provenance.PIPELINE_MANUAL,
        trigger=provenance.TRIGGER_MANUAL,
        actor=user["username"], actorId=user.get("userId", ""),
        source="manual",
        sourceDetail="archived a relationship in Onto Verse",
        writtenBy="ontology_universe.archive_relationship",
    ):
        if not neo4j.is_available():
            raise HTTPException(status_code=503, detail="Neo4j not available")
        ok = neo4j.archive_relationship(rel_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Relationship {rel_id!r} not found")
        try:
            neo4j.write_audit_log(
                actor=user["username"],
                action="ARCHIVE_RELATIONSHIP",
                target_id=rel_id,
                before={"active": True},
                after={"active": False},
            )
        except Exception:
            pass
        try:
            dynamo.write_changelog(_build_changelog_entry(
                entity_id=rel_id,
                entity_type="Relationship",
                entity_label="Relationship",
                entity_name=rel_id,
                change_type="RELATIONSHIP_ARCHIVE",
                actor=user["username"],
                before={"active": True},
                after={"active": False},
                source="api",
                notes=f"Relationship archived by {user['username']}",
            ))
        except Exception:
            pass
        return {"ok": True}


@router.get("/audit-log")
def get_audit_log(
    page: int = 0,
    page_size: int = 50,
    _: dict = Depends(require_permission("ontology_maintain")),
):
    if not neo4j.is_available():
        return []
    return neo4j.get_audit_log(page=page, page_size=page_size)


@router.get("/changelog")
def get_changelog(
    limit: int = 50,
    _: dict = Depends(require_permission("ontology_maintain")),
):
    """Paginated DynamoDB changelog — all versioning events, newest first."""
    return dynamo.get_recent_changelog(limit=limit)


@router.get("/nodes/{node_id}/changelog")
def get_node_changelog(
    node_id: str,
    limit: int = 20,
    _: dict = Depends(require_permission("ontology_maintain")),
):
    """Per-node version history from DynamoDB.

    `node_id` is the engine's own node id, which is what the UI holds. History is
    keyed by externalId so it survives a rebuild and resolves on whichever engine a
    deployment runs, so the id is resolved to an externalId first.

    Rows written before that change are keyed by the engine id, so both are queried
    and merged — otherwise the re-key would appear to erase existing history.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    external_id = ""
    try:
        node = neo4j.get_node_by_id(node_id)
        external_id = (node or {}).get("externalId") or ""
    except Exception:  # noqa: BLE001 — fall back to the legacy lookup below
        external_id = ""

    for key in filter(None, (external_id, node_id)):
        for row in dynamo.get_entity_changelog(entity_id=key, limit=limit):
            change_id = row.get("changeId")
            if change_id and change_id not in seen:
                seen.add(change_id)
                rows.append(row)

    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return rows[:limit]


@router.get("/relationships/{rel_id}/changelog")
def get_relationship_changelog(
    rel_id: str,
    limit: int = 20,
    _: dict = Depends(require_permission("ontology_maintain")),
):
    """Per-relationship version history from DynamoDB."""
    return dynamo.get_entity_changelog(entity_id=rel_id, limit=limit)


@router.get("/stats")
def get_stats_endpoint(_: dict = Depends(get_current_user)):
    return neo4j.get_stats()


# ── WebSocket: Ontology Maintainer Chat ───────────────────────────────────────

@router.websocket("/ws/chat")
async def ontology_chat_ws(ws: WebSocket):
    """Maintainer chat with human-in-the-loop.  JWT must be sent as ?token= query param."""
    token = ws.query_params.get("token")
    from src.services.auth_service import verify_token
    user = verify_token(token or "")

    # Must accept before closing — closing before accept raises a Starlette error
    await ws.accept()

    if not user or "ontology_maintain" not in user.get("permissions", []):
        await ws.close(code=4003)
        return
    await ws.send_json({"type": "connected", "username": user["username"]})

    # Per-connection store: changeId -> {changes, summary, session_id}
    pending_changes: dict[str, dict] = {}

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "chat":
                text = msg.get("text", "")
                session_id = msg.get("sessionId", "default")
                await _run_maintainer_chat(ws, text, user, session_id, pending_changes)

            elif msg_type == "confirm_change":
                await _apply_or_reject_changes(ws, msg, user, pending_changes)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("Ontology chat WS error")
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


async def _run_maintainer_chat(
    ws: WebSocket, text: str, user: dict, session_id: str,
    pending_changes: dict[str, dict],
) -> None:
    """Stream agent events to the WebSocket.  Captures pending_change frames."""
    try:
        from src.agents.ontology_maintainer_agent import run_maintainer_agent
        async for event in run_maintainer_agent(text, user, session_id):
            await ws.send_json(event)
            if event.get("type") == "pending_change":
                # Store for later confirmation — agent signals stop here
                pending_changes[event["changeId"]] = {
                    "changes": event["changes"],
                    "summary": event["summary"],
                    "session_id": session_id,
                }
    except ImportError:
        await ws.send_json({"type": "token", "text": "Ontology maintainer agent not available — check dependencies."})
        await ws.send_json({"type": "done"})
    except Exception as exc:
        log.exception("Maintainer agent error")
        await ws.send_json({"type": "error", "message": str(exc)})


async def _apply_or_reject_changes(
    ws: WebSocket, msg: dict, user: dict, pending_changes: dict[str, dict],
) -> None:
    """Execute confirmed changes or discard rejected ones.  Writes both Neo4j and DynamoDB."""
    change_id = msg.get("changeId")
    approved = msg.get("approved", False)
    pending = pending_changes.pop(change_id, None)

    if not pending:
        await ws.send_json({"type": "error", "message": f"No pending change found for id {change_id}"})
        return

    if not approved:
        await ws.send_json({
            "type": "change_result",
            "changeId": change_id,
            "success": False,
            "message": "Changes cancelled by user",
        })
        return

    results = []
    session_id = pending.get("session_id", "chat")
    actor = user["username"]

    from src.graph import provenance
    # One run for the whole approved batch. The maintainer chat proposes a set of
    # changes and a human approves them together, so they are one decision — and
    # the trace should read as one.
    with provenance.trace_run(
        provenance.PIPELINE_MANUAL,
        trigger=provenance.TRIGGER_MANUAL,
        actor=actor,
        source="chat",
        sourceDetail="approved in the Onto Verse maintainer chat",
        sessionId=session_id,
        writtenBy="ontology_universe.maintainer_chat",
        notes=f"{len(pending['changes'])} change(s) approved by {actor}",
    ):
        for change in pending["changes"]:
            result = await _execute_single_change(change, actor, session_id)
            results.append(result)

    await ws.send_json({"type": "change_result", "changeId": change_id, "success": True, "results": results})
    await ws.send_json({"type": "graph_refresh_needed"})


async def _execute_single_change(change: dict, actor: str, session_id: str) -> dict:
    """Execute one proposed change and write both Neo4j audit + DynamoDB changelog."""
    change_type = change.get("changeType", "")
    entity_id = change.get("entityId", "")
    entity_name = change.get("entityName", entity_id)
    entity_label = change.get("entityLabel", "Unknown")

    try:
        if change_type == "UPDATE":
            prop = change.get("prop", "")
            after_val = change.get("after")
            ok = neo4j.update_node_property(entity_id, prop, after_val)
            if ok:
                neo4j.write_audit_log(actor, f"UPDATE_PROPERTY:{prop}", entity_id, change.get("before"), after_val)
                dynamo.write_changelog(_build_changelog_entry(
                    entity_id=entity_id, entity_type="Node", entity_label=entity_label,
                    entity_name=entity_name, change_type="UPDATE", actor=actor,
                    before={prop: change.get("before")}, after={prop: after_val},
                    session_id=session_id, source="chat",
                    notes=f"Chat: {prop}: {change.get('before')!r} → {after_val!r}",
                ))
            return {"ok": ok, "changeType": change_type, "entity": entity_name}

        elif change_type == "RETIRE":
            node = neo4j.get_node_by_id(entity_id)
            if not node:
                return {"ok": False, "changeType": change_type, "error": "Node not found"}
            label = node["labels"][0] if node.get("labels") else entity_label
            eid = node.get("externalId", entity_id)
            ok = neo4j.retire_node(label, eid)
            if ok:
                neo4j.write_audit_log(actor, "RETIRE_NODE", entity_id, {"status": "active"}, {"status": "retired"})
                dynamo.write_changelog(_build_changelog_entry(
                    entity_id=entity_id, entity_type="Node", entity_label=label,
                    entity_name=entity_name, change_type="RETIRE", actor=actor,
                    before={"status": "active"}, after={"status": "retired"},
                    session_id=session_id, source="chat",
                    notes=f"Chat: node retired by {actor}",
                ))
            return {"ok": ok, "changeType": change_type, "entity": entity_name}

        elif change_type == "CREATE":
            node_label = change.get("toLabel") or entity_label
            props = change.get("after") or {}
            node = neo4j.create_node(node_label, entity_name, props, actor)
            new_id = node.get("elementId") or node.get("id", "unknown")
            dynamo.write_changelog(_build_changelog_entry(
                entity_id=str(new_id), entity_type="Node", entity_label=node_label,
                entity_name=entity_name, change_type="CREATE", actor=actor,
                before=None, after=props,
                session_id=session_id, source="chat",
                notes=f"Chat: new {node_label} node created",
            ))
            return {"ok": True, "changeType": change_type, "entity": entity_name, "newId": str(new_id)}

        elif change_type == "RELATIONSHIP_ADD":
            node = neo4j.get_node_by_id(entity_id)
            if not node:
                return {"ok": False, "changeType": change_type, "error": "Source node not found"}
            from_label = node["labels"][0] if node.get("labels") else "Unknown"
            from_eid = node.get("externalId", entity_id)
            to_label = change.get("toLabel", "Unknown")
            to_eid = change.get("toExternalId", "")
            rel_type = change.get("relType", "RELATED_TO")
            ok = neo4j.upsert_relationship(from_label, from_eid, to_label, to_eid, rel_type)
            if ok:
                neo4j.write_audit_log(actor, f"ADD_RELATIONSHIP:{rel_type}", entity_id, None, {"to": to_eid})
                dynamo.write_changelog(_build_changelog_entry(
                    entity_id=entity_id, entity_type="Relationship", entity_label=rel_type,
                    entity_name=f"{entity_name} → {to_eid}", change_type="RELATIONSHIP_ADD", actor=actor,
                    before=None, after={"from": from_eid, "to": to_eid, "type": rel_type},
                    session_id=session_id, source="chat",
                    notes=f"Chat: added {rel_type} from {entity_name} to {to_eid}",
                ))
            return {"ok": ok, "changeType": change_type, "entity": entity_name}

        elif change_type == "RELATIONSHIP_ARCHIVE":
            ok = neo4j.archive_relationship(entity_id)
            if ok:
                neo4j.write_audit_log(actor, "ARCHIVE_RELATIONSHIP", entity_id, {"active": True}, {"active": False})
                dynamo.write_changelog(_build_changelog_entry(
                    entity_id=entity_id, entity_type="Relationship", entity_label=entity_label,
                    entity_name=entity_name, change_type="RELATIONSHIP_ARCHIVE", actor=actor,
                    before={"active": True}, after={"active": False},
                    session_id=session_id, source="chat",
                    notes=f"Chat: relationship archived by {actor}",
                ))
            return {"ok": ok, "changeType": change_type, "entity": entity_name}

        else:
            return {"ok": False, "changeType": change_type, "error": f"Unknown change type: {change_type}"}

    except Exception as exc:
        log.exception("_execute_single_change failed for %s", change_type)
        return {"ok": False, "changeType": change_type, "entity": entity_name, "error": str(exc)}

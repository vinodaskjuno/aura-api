"""Provenance API — where every node and edge came from.

Routes:
  GET /api/provenance/nodes/{id}       Full trace for one node
  GET /api/provenance/edges/{id}       Full trace for one relationship
  GET /api/provenance/runs             Filterable feed of ingestion runs
  GET /api/provenance/runs/{run_id}    One run, its stats, and what it wrote
  GET /api/provenance/summary          Per-pipeline health + attribution coverage

Readable by any signed-in user. That is a deliberate widening: the existing
changelog endpoints require `ontology_maintain`, so the Provenance tab in Onto Verse
showed most people nothing at all, and "where did this come from" is a question
every user of a knowledge graph has to be able to answer.

Before/after **values** stay behind `ontology_maintain` — those are the contents of
records a viewer may not be cleared to see, which is a different question from who
wrote them and when. `_redact` draws that line in one place.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src.database import dynamo_client as dynamo
from src.graph import neo4j_client as neo4j
from src.graph import provenance as prov
from src.routers.auth import get_current_user
from src.services import ontology_version_service as versions

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/provenance", tags=["provenance"])

# Node/edge properties that describe the write rather than the thing written.
# Pulled out into their own block so the UI does not have to know which of the 40
# properties on a node are provenance.
_TRACE_KEYS = (
    "source", "sourceDetail", "sourceRecordId", "pipeline", "trigger", "actor",
    "actorId", "writtenBy", "attribution", "firstSeenAt", "firstSeenRunId",
    "createdBy", "createdVia", "lastSeenAt", "lastSeenRunId", "versionId",
    "createdAt", "updatedAt", "confidence", "discoveredBy", "factType", "evidence",
    "firstSeen", "lastSeen",
)


def _can_see_values(user: dict) -> bool:
    return "ontology_maintain" in (user.get("permissions") or [])


def _redact(rows: list[dict], allowed: bool) -> list[dict]:
    """Strip before/after payloads for users who may not read record contents.

    The event itself — who, when, which run — is never redacted. Hiding that would
    defeat the feature for exactly the people it is meant to serve.
    """
    if allowed:
        return rows
    out = []
    for row in rows:
        clean = {k: v for k, v in row.items() if k not in ("before", "after")}
        clean["valuesRedacted"] = bool(row.get("before") or row.get("after"))
        out.append(clean)
    return out


def _extract_trace(props: dict) -> dict:
    trace = {k: props[k] for k in _TRACE_KEYS if props.get(k) not in (None, "")}
    # Evidence is stored as a JSON string because Neo4j has no nested types.
    raw = trace.get("evidence")
    if isinstance(raw, str):
        try:
            trace["evidence"] = json.loads(raw)
        except (ValueError, TypeError):
            trace["evidence"] = [raw]
    trace.setdefault("attribution", prov.ATTRIBUTION_PRE_TRACE)
    trace.setdefault("pipeline", prov.PIPELINE_UNKNOWN)
    return trace


def _run_brief(run_id: str) -> dict | None:
    if not run_id:
        return None
    record = versions.get_version(run_id)
    if not record:
        return None
    return {
        "runId": record.get("versionId"),
        "versionNumber": record.get("versionNumber"),
        "pipeline": record.get("pipeline") or record.get("loadMethod"),
        "trigger": record.get("trigger"),
        "actor": record.get("actor"),
        "status": record.get("status"),
        "startedAt": record.get("startedAt"),
        "finishedAt": record.get("finishedAt"),
        "durationMs": record.get("durationMs"),
        "sourceDetail": record.get("sourceDetail"),
        "sources": record.get("sources") or [],
        "notes": record.get("notes"),
    }


def _timeline(entity_id: str, external_id: str, limit: int) -> list[dict]:
    """Change history for one entity, newest first.

    Queries both the portable externalId and the engine's own id: rows written
    before history was re-keyed use the latter, and querying only one of them would
    make existing history look erased. This mirrors what
    `ontology_universe.get_node_changelog` already does.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for key in filter(None, (external_id, entity_id)):
        try:
            for row in dynamo.get_entity_changelog(entity_id=key, limit=limit):
                change_id = row.get("changeId")
                if change_id and change_id not in seen:
                    seen.add(change_id)
                    rows.append(row)
        except Exception as exc:  # noqa: BLE001 — a missing history is not a 500
            log.warning("changelog lookup failed for %s: %s", key, exc)
    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return rows[:limit]


def _contributing(timeline: list[dict], trace: dict) -> list[dict]:
    """Distinct pipelines that have written this entity, with a count each.

    Built from the timeline, then topped up from the live properties: with
    diff-only history a node written once and re-confirmed a hundred times has a
    single row, and the current writer would otherwise be missing from its own
    contributor list.
    """
    counts: dict[str, int] = {}
    for row in timeline:
        name = row.get("pipeline") or row.get("source") or prov.PIPELINE_UNKNOWN
        counts[name] = counts.get(name, 0) + 1
    current = trace.get("pipeline")
    if current and current not in counts:
        counts[current] = 1
    return [{"pipeline": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda kv: -kv[1])]


@router.get("/nodes/{node_id}")
def node_trace(
    node_id: str,
    limit: int = Query(30, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """Everything known about where one node came from."""
    node = neo4j.get_node_by_id(node_id)
    if not node:
        raise HTTPException(404, f"Node {node_id!r} not found")

    trace = _extract_trace(node)
    external_id = node.get("externalId") or ""
    timeline = _timeline(node_id, external_id, limit)
    return {
        "entityKind": "node",
        "id": node_id,
        "externalId": external_id,
        "label": (node.get("labels") or ["Unknown"])[0],
        "name": node.get("name") or external_id or node_id,
        "trace": trace,
        "origin": _run_brief(trace.get("firstSeenRunId", "")),
        "latest": _run_brief(trace.get("lastSeenRunId", "") or trace.get("versionId", "")),
        "contributingSources": _contributing(timeline, trace),
        "timeline": _redact(timeline, _can_see_values(user)),
        "canSeeValues": _can_see_values(user),
    }


@router.get("/edges/{edge_id}")
def edge_trace(
    edge_id: str,
    limit: int = Query(30, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """The same, for a relationship — which had no provenance surface at all."""
    edge = neo4j.get_relationship_by_id(edge_id)
    if not edge:
        raise HTTPException(404, f"Relationship {edge_id!r} not found")

    trace = _extract_trace(edge)
    timeline = _timeline(edge_id, "", limit)
    return {
        "entityKind": "edge",
        "id": edge_id,
        "type": edge.get("type"),
        "source": edge.get("source"),
        "target": edge.get("target"),
        "trace": trace,
        "origin": _run_brief(trace.get("firstSeenRunId", "")),
        "latest": _run_brief(trace.get("lastSeenRunId", "")),
        "contributingSources": _contributing(timeline, trace),
        "timeline": _redact(timeline, _can_see_values(user)),
        "canSeeValues": _can_see_values(user),
    }


@router.get("/runs")
def list_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pipeline: str | None = None,
    trigger: str | None = None,
    actor: str | None = None,
    status: str | None = None,
    _: dict = Depends(get_current_user),
):
    """The run feed behind the Lineage Explorer."""
    return versions.list_versions(
        limit=limit, offset=offset,
        pipeline=pipeline, trigger=trigger, actor=actor, status=status,
    )


@router.get("/runs/{run_id}")
def run_detail(
    run_id: str,
    entity_limit: int = Query(200, ge=0, le=1000),
    user: dict = Depends(get_current_user),
):
    """One run, its stats, its errors, and the entities it wrote."""
    record = versions.get_version(run_id)
    if not record:
        raise HTTPException(404, f"Run {run_id!r} not found")

    entities = neo4j.entities_written_by_run(run_id, limit=entity_limit) if entity_limit else []
    changes: list[dict] = []
    try:
        changes = dynamo.query_items(
            "ontology-changelog", pk_name="runId", pk_value=run_id,
            index_name="runId-timestamp-index", limit=entity_limit or 100,
        )
    except Exception as exc:  # noqa: BLE001 — index may not exist yet
        log.warning("run changelog lookup failed for %s: %s", run_id, exc)

    return {
        **record,
        "entities": entities,
        # Deliberately reported: `entities` is capped, and a truncated list that
        # says nothing about being truncated reads as "this is all of it".
        "entitiesTruncated": len(entities) >= entity_limit > 0,
        "changes": _redact(changes, _can_see_values(user)),
    }


@router.get("/summary")
def summary(_: dict = Depends(get_current_user)):
    """Per-pipeline counts and freshness, plus how much of the graph is attributed."""
    data = neo4j.provenance_summary()
    recent = versions.list_versions(limit=200)
    last_by_pipeline: dict[str, dict[str, Any]] = {}
    for run in recent:
        name = run.get("pipeline") or run.get("loadMethod") or prov.PIPELINE_UNKNOWN
        if name not in last_by_pipeline:
            last_by_pipeline[name] = {
                "runId": run.get("versionId"),
                "versionNumber": run.get("versionNumber"),
                "startedAt": run.get("startedAt"),
                "status": run.get("status"),
                "actor": run.get("actor"),
                "trigger": run.get("trigger"),
            }
    for entry in data.get("pipelines", []):
        entry["lastRun"] = last_by_pipeline.get(entry["pipeline"])
    return data

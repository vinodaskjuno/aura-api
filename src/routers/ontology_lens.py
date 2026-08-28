"""Lens-scoped ontology graph API.

Routes:
  GET  /api/ontology/lenses                  Lens catalog + tier maps (static)
  GET  /api/ontology/lens/{lens_id}          Lens-projected {nodes, links, meta}
  GET  /api/ontology/lens/{lens_id}/summary  Lens KPI aggregates

Projection lives on the server because a lens is defined by *typed* edges, not a
flat reltype list: ``DEPENDS_ON`` means ``Repository → Dependency`` in the Git
lens and ``Service → Service`` in the application view. Doing it here also means
the ``limit`` truncates inside the lens — anchors-first — instead of truncating
the whole graph before the lens is applied.

Read routes degrade to HTTP 200 with an empty payload plus ``warning``, matching
the existing ``/org-graph`` contract. ``/lenses`` is static and works with Neo4j
down, which is what lets the lens chrome and legend render on an empty graph.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from src.graph import neo4j_client as neo4j
from src.ontology.lenses import LENSES
from src.ontology.schema import (
    ALL_LABELS,
    NODE_TIER,
    RELATIONSHIP_TYPES,
)
from src.routers.auth import get_current_user
from src.services.lens_summary_service import get_lens_summary

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ontology", tags=["ontology-lens"])


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


def _require_lens(lens_id: str):
    lens = LENSES.get(lens_id)
    if lens is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown lens {lens_id!r}; known: {sorted(LENSES)}",
        )
    return lens


@router.get("/lenses")
def list_lenses(_: dict = Depends(get_current_user)):
    """Return the lens catalog. Static constants — no Neo4j required.

    This is the contract that removes the frontend's duplicated NODE_TIER map:
    the backend owns tiers, both global and per-lens.
    """
    return {
        "nodeTiers": NODE_TIER,
        "relationshipTypes": RELATIONSHIP_TYPES,
        "labels": ALL_LABELS,
        "lenses": [
            {
                "id": lens.id,
                "name": lens.name,
                "description": lens.description,
                "accent": lens.accent,
                "labels": list(lens.labels),
                "edges": [
                    {
                        "from": list(e.from_labels),
                        "type": e.rel_type,
                        "to": list(e.to_labels),
                        "reverse": e.reverse,
                        "weight": e.weight,
                    }
                    for e in lens.edges
                ],
                "relationshipTypes": list(lens.edge_types()),
                "tiers": dict(lens.tiers),
                "anchorLabels": list(lens.anchor_labels),
                "kpis": [
                    {
                        "id": k.id,
                        "label": k.label,
                        "format": k.format,
                        "hint": k.hint,
                        "secondary": k.secondary,
                    }
                    for k in lens.kpis
                ],
            }
            for lens in LENSES.values()
        ],
    }


@router.get("/lens/{lens_id}")
def get_lens(
    lens_id: str,
    limit: int = Query(5000, ge=1, le=50000),
    sources: str | None = None,
    env: str | None = None,
    drop_orphans: bool = False,
    _: dict = Depends(get_current_user),
):
    """Return the subgraph projected by the named lens."""
    lens = _require_lens(lens_id)

    if not neo4j.is_available():
        return {
            "nodes": [],
            "links": [],
            "warning": "Neo4j not available — no data",
            "meta": {
                "lensId": lens.id,
                "lensName": lens.name,
                "nodeCount": 0,
                "linkCount": 0,
                "orphanCount": 0,
                "truncated": False,
                "limit": limit,
                "labels": list(lens.labels),
                "tiers": dict(lens.tiers),
                "available": False,
            },
        }

    try:
        return neo4j.get_lens_graph(
            lens,
            limit=limit,
            sources=_csv(sources),
            envs=_csv(env),
            drop_orphans=drop_orphans,
        )
    except Exception as exc:
        log.exception("get_lens_graph failed for lens %s", lens_id)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/lens/{lens_id}/summary")
def get_lens_kpis(lens_id: str, _: dict = Depends(get_current_user)):
    """Return KPI aggregates for the lens header."""
    lens = _require_lens(lens_id)
    try:
        return get_lens_summary(lens)
    except Exception as exc:
        log.exception("lens summary failed for lens %s", lens_id)
        raise HTTPException(status_code=500, detail=str(exc))

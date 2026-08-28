"""Runbook ingestion, retrieval and scoring.

Retrieval is Neo4j full-text, not a vector store: no embedding library exists in
requirements.txt, and a deterministic scorer is explainable AND directly scoreable by
the eval harness (retrieval@k). Swapping the FTS leg for embeddings later happens
behind the unchanged `search()` signature.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from src.models.sop import TEMPLATE_STEPS

log = logging.getLogger(__name__)

# Score = 0.6 * service edge match + 0.3 * normalized FTS + 0.1 * recency.
_W_SERVICE, _W_FTS, _W_RECENCY = 0.6, 0.3, 0.1

# A synthesized runbook must never silently outrank a human-authored one.
_ORIGIN_BONUS = {"human": 0.15, "confluence": 0.10, "git": 0.10,
                 "synthesized": 0.0, "template": -0.05}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _recency_score(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - then).days
        return max(0.0, 1.0 - days / 365.0)
    except Exception:  # noqa: BLE001
        return 0.0


def score_runbooks(rows: list[dict], service: str, signatures: list[str] | None = None,
                   ) -> list[dict]:
    """Deterministic ranking. Pure function — testable without a database."""
    if not rows:
        return []
    max_fts = max((r.get("fts_score") or 0.0) for r in rows) or 1.0
    sigs = [s.lower() for s in (signatures or [])]

    scored = []
    for r in rows:
        matched: list[str] = []

        services = r.get("services") or r.get("documented_services") or []
        if isinstance(services, str):
            services = [s.strip() for s in services.split(",") if s.strip()]
        svc_hit = 1.0 if service and any(
            service.lower() == s.lower() for s in services) else 0.0
        if not svc_hit and service and any(service.lower() in s.lower() for s in services):
            svc_hit = 0.6
        if svc_hit:
            matched.append("service")

        alert_sigs = r.get("alertSignatures") or []
        if isinstance(alert_sigs, str):
            alert_sigs = [alert_sigs]
        sig_hit = any(a and any(a.lower() in s or s in a.lower() for s in sigs)
                      for a in alert_sigs)
        if sig_hit:
            matched.append("alert_signature")

        fts = (r.get("fts_score") or 0.0) / max_fts
        if fts > 0.2:
            matched.append("text")

        score = (_W_SERVICE * svc_hit + _W_FTS * fts
                 + _W_RECENCY * _recency_score(r.get("lastConfirmedAt")
                                               or r.get("createdAt", "")))
        score += _ORIGIN_BONUS.get(r.get("origin", "human"), 0.0)
        score += min(0.15, 0.03 * int(r.get("confirmedCount", 0) or 0))
        if sig_hit:
            score += 0.15
        if r.get("status") == "candidate":
            score -= 0.20        # candidates rank below proven runbooks

        scored.append({**r, "match_score": round(max(0.0, min(1.0, score)), 3),
                       "matched_on": matched})
    return sorted(scored, key=lambda r: r["match_score"], reverse=True)


def search(service: str = "", signatures: list[str] | None = None,
           query: str = "", limit: int = 10) -> list[dict]:
    """Retrieve and rank runbooks. Falls back to the SOP aiops template."""
    rows: list[dict] = []
    try:
        from src.graph.neo4j_client import search_runbooks
        rows = search_runbooks(service=service,
                               alert_signature=" ".join((signatures or [])[:3]),
                               labels=[query] if query else None,
                               limit=max(limit * 2, 20))
    except Exception as exc:  # noqa: BLE001
        log.debug("runbook FTS unavailable: %s", exc)

    ranked = score_runbooks(rows, service, signatures)[:limit]
    if ranked:
        return ranked
    return [template_runbook(service)]


def template_runbook(service: str = "") -> dict:
    """The models/sop.py aiops checklist, as a last-resort runbook. Reuse, don't reinvent."""
    steps = TEMPLATE_STEPS.get("aiops", [])
    return {
        "externalId": "runbook:template:aiops",
        "runbookId": "runbook:template:aiops",
        "title": "Standard Incident Response (template)",
        "origin": "template",
        "status": "active",
        "sourceType": "template",
        "services": [service] if service else [],
        "steps": steps,
        "match_score": 0.1,
        "matched_on": ["fallback"],
        "bodySnippet": "Built-in 7-step incident runbook used when no runbook matched.",
        "confirmedCount": 0,
    }


def save_runbook(doc: dict, body: str = "", project_id: str = "") -> dict:
    """Persist a Runbook node (+ S3 body) and link it to the services it documents."""
    rid = doc.get("runbookId") or f"runbook:{uuid.uuid4().hex[:12]}"
    s3_uri = ""
    if body:
        try:
            from src.storage.s3_client import put_object
            key = f"observability/{project_id or 'global'}/runbooks/{rid}.md"
            s3_uri = put_object("exports", key, body, content_type="text/markdown")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not store runbook body: %s", exc)

    props = {
        "name": doc.get("title", ""),
        "title": doc.get("title", ""),
        "origin": doc.get("origin", "human"),
        "status": doc.get("status", "active"),
        "sourceType": doc.get("sourceType", "upload"),
        "sourceUrl": doc.get("sourceUrl", ""),
        "s3Uri": s3_uri,
        "bodySnippet": (body or doc.get("bodySnippet", ""))[:8000],
        "tags": doc.get("tags") or [],
        "services": doc.get("services") or [],
        "alertSignatures": doc.get("alertSignatures") or [],
        "severity": doc.get("severity", ""),
        "steps": json.dumps(doc.get("steps") or []),
        "confirmedCount": int(doc.get("confirmedCount", 0)),
        "lastConfirmedAt": doc.get("lastConfirmedAt", ""),
        "sourceInvestigations": doc.get("sourceInvestigations") or [],
        "createdAt": doc.get("createdAt") or _now(),
        "source": "observability",
    }
    try:
        from src.graph import neo4j_client as graph
        graph.upsert_node("Runbook", rid, props)
        for svc in props["services"]:
            if not svc:
                continue
            graph.upsert_node("Service", f"service:{svc}", {"name": svc})
            graph.upsert_relationship(
                "Runbook", rid, "Service", f"service:{svc}", "DOCUMENTS",
                provenance={"source": "observability", "discoveredBy": "runbook_ingest",
                            "confidence": 1.0, "factType": "known"})
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist runbook %s: %s", rid, exc)
    return {**props, "runbookId": rid, "steps": doc.get("steps") or []}


def get_runbook(runbook_id: str) -> dict | None:
    if runbook_id == "runbook:template:aiops":
        return template_runbook()
    try:
        from src.graph.neo4j_client import run_query
        rows = run_query("MATCH (n:Runbook {externalId:$eid}) RETURN n LIMIT 1",
                         {"eid": runbook_id})
        if not rows:
            return None
        node = dict(rows[0]["n"])
        node["runbookId"] = node.get("externalId", runbook_id)
        node["steps"] = _decode_steps(node.get("steps"))
        return node
    except Exception as exc:  # noqa: BLE001
        log.warning("get_runbook failed: %s", exc)
        return None


def _decode_steps(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
    return []


def list_runbooks(origin: str = "", status: str = "", limit: int = 100) -> list[dict]:
    try:
        from src.graph.neo4j_client import run_query
        where = []
        params: dict = {"limit": limit}
        if origin:
            where.append("n.origin = $origin")
            params["origin"] = origin
        if status:
            where.append("n.status = $status")
            params["status"] = status
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = run_query(
            f"MATCH (n:Runbook) {clause} RETURN n ORDER BY n.createdAt DESC LIMIT $limit",
            params)
        out = []
        for r in rows:
            node = dict(r["n"])
            node["runbookId"] = node.get("externalId", "")
            node["steps"] = _decode_steps(node.get("steps"))
            out.append(node)
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("list_runbooks failed: %s", exc)
        return []


def delete_runbook(runbook_id: str) -> bool:
    try:
        from src.graph.neo4j_client import run_query
        run_query("MATCH (n:Runbook {externalId:$eid}) DETACH DELETE n",
                  {"eid": runbook_id})
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_runbook failed: %s", exc)
        return False

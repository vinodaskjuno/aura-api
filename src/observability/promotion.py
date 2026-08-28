"""Runbook synthesis and promotion.

Triggered when an outcome reaches observability_promotion_min_confidence (0.7 —
human or git-correlation grade):

  runbook matched and worked  -> confirmedCount++, lastConfirmedAt, higher weight
  no runbook matched          -> synthesize a CANDIDATE from the resolution
  same (service, category) resolved the same way N times -> candidate -> active

A `wrong` verdict cascades: candidates synthesized from that investigation are
demoted, and the case flips into the negative pool rather than being deleted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config_settings import get_settings

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def on_outcome(outcome, investigation: dict) -> dict | None:
    s = get_settings()
    if outcome.confidence < s.observability_promotion_min_confidence:
        return None

    if outcome.verdict == "wrong":
        return _demote_from(investigation)
    if outcome.verdict not in ("confirmed", "partial"):
        return None

    runbook_id = investigation.get("runbookId", "")
    if runbook_id and not runbook_id.startswith("runbook:template:"):
        return _confirm(runbook_id, investigation)
    return _synthesize(outcome, investigation)


def _confirm(runbook_id: str, investigation: dict) -> dict | None:
    from src.observability import runbooks
    rb = runbooks.get_runbook(runbook_id)
    if not rb:
        return None
    count = int(rb.get("confirmedCount", 0) or 0) + 1
    sources = list(rb.get("sourceInvestigations") or [])
    inv_id = investigation.get("investigationId", "")
    if inv_id and inv_id not in sources:
        sources.append(inv_id)

    status = rb.get("status", "active")
    if status == "candidate" and count >= get_settings().observability_promotion_repeat_count:
        status = "active"
        log.info("Promoted runbook %s candidate -> active after %d confirmations",
                 runbook_id, count)

    try:
        from src.graph import neo4j_client as graph
        graph.upsert_node("Runbook", runbook_id, {
            "confirmedCount": count, "lastConfirmedAt": _now(),
            "status": status, "sourceInvestigations": sources[-20:],
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not confirm runbook %s: %s", runbook_id, exc)
        return None
    return {"runbookId": runbook_id, "action": "confirmed",
            "confirmedCount": count, "status": status}


def _synthesize(outcome, investigation: dict) -> dict | None:
    """Build a candidate runbook from what the investigation actually established.

    Steps come from the recommended actions plus the discriminating checks that
    actually discriminated — i.e. the things that turned out to matter.
    """
    from src.observability import runbooks

    root = investigation.get("rootCause") or {}
    service = investigation.get("serviceName", "")
    category = outcome.actual_category or root.get("category", "") or "unknown"
    statement = outcome.actual_cause or root.get("statement", "")
    if not statement:
        return None

    steps = []
    for i, chk in enumerate(investigation.get("discriminatingChecks") or [], start=1):
        steps.append({
            "id": f"step-{i}", "order": i,
            "title": f"Check: {chk.get('description','')[:80]}",
            "description": (f"Signal: {chk.get('signal','')} · "
                            f"Query: {chk.get('query','')}\nExpect: {chk.get('expect','')}"),
            "checklist": [],
        })
    offset = len(steps)
    for i, act in enumerate(investigation.get("recommendedActions") or [], start=1):
        steps.append({
            "id": f"step-{offset + i}", "order": offset + i,
            "title": act.get("action", "")[:120],
            "description": (f"Risk: {act.get('risk','unknown')} · "
                            f"Owner: {act.get('owner_hint','')}"),
            "checklist": [],
        })
    if not steps:
        return None

    doc = {
        "title": f"[{category}] {statement[:90]}",
        "origin": "synthesized",
        "status": "candidate",
        "sourceType": "synthesized",
        "services": [service] if service else [],
        "tags": [category, "synthesized"],
        "alertSignatures": (investigation.get("errorSignatures") or [])[:5],
        "severity": investigation.get("severity", "high"),
        "steps": steps,
        "confirmedCount": 1,
        "lastConfirmedAt": _now(),
        "sourceInvestigations": [investigation.get("investigationId", "")],
        "createdAt": _now(),
    }
    body = _to_markdown(doc, statement, investigation)
    saved = runbooks.save_runbook(doc, body=body,
                                  project_id=investigation.get("projectId", ""))
    log.info("Synthesized candidate runbook %s from %s",
             saved.get("runbookId"), investigation.get("investigationId"))

    existing = _count_similar(service, category)
    if existing >= get_settings().observability_promotion_repeat_count:
        try:
            from src.graph import neo4j_client as graph
            graph.upsert_node("Runbook", saved["runbookId"], {"status": "active"})
            saved["status"] = "active"
        except Exception:  # noqa: BLE001
            pass
    return {"runbookId": saved.get("runbookId"), "action": "synthesized",
            "status": saved.get("status", "candidate")}


def _count_similar(service: str, category: str) -> int:
    """How many DISTINCT resolutions of this shape exist — not raw incident count."""
    from src.observability import store
    seen = set()
    for c in store.list_cases(service=service, limit=200):
        if c.get("rootCauseCategory") == category and c.get("outcomeVerdict") == "confirmed":
            seen.add(c.get("resolutionHash", c.get("caseId", "")))
    return len(seen)


def _demote_from(investigation: dict) -> dict | None:
    """A wrong verdict retracts anything this investigation taught."""
    from src.observability import runbooks
    inv_id = investigation.get("investigationId", "")
    demoted = []
    for rb in runbooks.list_runbooks(origin="synthesized", limit=200):
        if inv_id and inv_id in (rb.get("sourceInvestigations") or []):
            try:
                from src.graph import neo4j_client as graph
                graph.upsert_node("Runbook", rb["runbookId"],
                                  {"status": "archived", "confirmedCount": 0})
                demoted.append(rb["runbookId"])
            except Exception:  # noqa: BLE001
                pass
    if demoted:
        log.info("Demoted %d synthesized runbook(s) after a WRONG verdict on %s",
                 len(demoted), inv_id)
    return {"action": "demoted", "runbookIds": demoted} if demoted else None


def _to_markdown(doc: dict, statement: str, investigation: dict) -> str:
    lines = [f"# {doc['title']}", "",
             "> Synthesized by Aura from a confirmed investigation. Review before relying on it.",
             "",
             f"**Service:** {', '.join(doc['services']) or 'n/a'}  ",
             f"**Category:** {', '.join(doc['tags'])}  ",
             f"**Source investigation:** {investigation.get('investigationId','')}", "",
             "## Root cause", "", statement, "", "## Steps", ""]
    for step in doc["steps"]:
        lines += [f"### {step['order']}. {step['title']}", "", step["description"], ""]
    sigs = doc.get("alertSignatures") or []
    if sigs:
        lines += ["## Matching alert signatures", ""]
        lines += [f"- `{s}`" for s in sigs]
    return "\n".join(lines)


def list_learned() -> list[dict]:
    """Everything the system has learned, with provenance. Powers the governance UI."""
    from src.observability import runbooks, store
    out: list[dict] = []
    for rb in runbooks.list_runbooks(origin="synthesized", limit=200):
        out.append({
            "artifactId": rb.get("runbookId", ""),
            "kind": "runbook",
            "title": rb.get("title", ""),
            "status": rb.get("status", ""),
            "confirmedCount": int(rb.get("confirmedCount", 0) or 0),
            "learnedFrom": rb.get("sourceInvestigations") or [],
            "createdAt": rb.get("createdAt", ""),
            "lastConfirmedAt": rb.get("lastConfirmedAt", ""),
        })
    for c in store.list_cases(limit=200):
        out.append({
            "artifactId": c.get("caseId", ""),
            "kind": "case",
            "title": c.get("rootCauseStatement", "")[:120],
            "status": c.get("outcomeVerdict", ""),
            "confidence": float(c.get("outcomeConfidence", 0) or 0),
            "service": c.get("serviceName", ""),
            "learnedFrom": [c.get("investigationId", "")],
            "createdAt": c.get("createdAt", ""),
        })
    return sorted(out, key=lambda a: a.get("createdAt", ""), reverse=True)


def forget(artifact_id: str) -> bool:
    """Delete one learned artifact. Reversible precisely because it is data."""
    from src.observability import cases, runbooks
    if artifact_id.startswith("case_"):
        return cases.forget(artifact_id)
    if artifact_id.startswith("runbook:"):
        return runbooks.delete_runbook(artifact_id)
    return False

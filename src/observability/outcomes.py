"""Outcome recording — the ground-truth signal the learning loop runs on.

Four sources compose into ONE graded confidence. They all observe the same event, so
we take the MAX, never a sum, and record every contributing source for audit.

  human verdict         1.0   UI Confirmed / Wrong / Partial
  git / deploy corr.    0.7   rollback or fix commit + signal recovery
  pagerduty notes       0.5   resolution text matched against the stated cause
  automated verifier    0.3   error rate returned to baseline

Only outcomes at >= observability_learning_min_confidence (default 0.5) become
learning material. Verifier-only outcomes accumulate as data but never teach. That
threshold is the entire difference between a system that learns and one that
confidently entrenches its own mistakes.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from src.config_settings import get_settings

log = logging.getLogger(__name__)

SOURCE_WEIGHTS = {
    "human": 1.0,
    "git_correlation": 0.7,
    "pagerduty_notes": 0.5,
    "verifier": 0.3,
}


@dataclass
class IncidentOutcome:
    investigation_id: str
    incident_id: str = ""
    verdict: str = "unknown"          # confirmed | wrong | partial | unknown
    confidence: float = 0.0
    sources: list[dict] = field(default_factory=list)
    actual_cause: str = ""
    actual_category: str = ""
    confirmed_by: str = ""
    confirmed_at: str = ""
    service_name: str = ""
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_row(self) -> dict:
        return {
            "investigationId": self.investigation_id,
            "recordedAt": self.recorded_at,
            "incidentId": self.incident_id,
            "verdict": self.verdict,
            "confidence": str(self.confidence),
            "sources": self.sources,
            "actualCause": self.actual_cause,
            "actualCategory": self.actual_category,
            "confirmedBy": self.confirmed_by,
            "confirmedAt": self.confirmed_at,
            "serviceName": self.service_name,
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def teaches(self) -> bool:
        """Is this outcome trustworthy enough to learn from?"""
        return self.confidence >= get_settings().observability_learning_min_confidence


def compose(sources: list[dict]) -> tuple[str, float]:
    """Fold contributing signals into (verdict, confidence).

    A human `wrong` overrides every automated signal regardless of their weights —
    the verifier saying "the incident ended" is not evidence the diagnosis was right.
    Incidents end for reasons unrelated to the stated cause all the time.
    """
    if not sources:
        return "unknown", 0.0

    human = [s for s in sources if s.get("source") == "human"]
    if human:
        latest = sorted(human, key=lambda s: s.get("observed_at", ""))[-1]
        return latest.get("verdict", "unknown"), SOURCE_WEIGHTS["human"]

    best = max(sources, key=lambda s: SOURCE_WEIGHTS.get(s.get("source", ""), 0.0))
    return (best.get("verdict", "unknown"),
            SOURCE_WEIGHTS.get(best.get("source", ""), 0.0))


def record(investigation_id: str, source: str, verdict: str, *,
           detail: str = "", actual_cause: str = "", actual_category: str = "",
           confirmed_by: str = "", incident_id: str = "",
           service_name: str = "") -> IncidentOutcome:
    """Append one signal and recompute the composite outcome."""
    from src.observability import store

    existing = store.get_outcome(investigation_id)
    sources: list[dict] = list((existing or {}).get("sources") or [])
    sources.append({
        "source": source,
        "verdict": verdict,
        "weight": SOURCE_WEIGHTS.get(source, 0.0),
        "detail": detail[:500],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })

    final_verdict, confidence = compose(sources)
    inv = store.get_investigation(investigation_id) or {}
    outcome = IncidentOutcome(
        investigation_id=investigation_id,
        incident_id=incident_id or inv.get("incidentId", ""),
        verdict=final_verdict,
        confidence=confidence,
        sources=sources,
        actual_cause=actual_cause or (existing or {}).get("actualCause", ""),
        actual_category=actual_category or (existing or {}).get("actualCategory", ""),
        confirmed_by=confirmed_by or (existing or {}).get("confirmedBy", ""),
        confirmed_at=datetime.now(timezone.utc).isoformat() if source == "human" else "",
        service_name=service_name or inv.get("serviceName", ""),
    )
    store.save_outcome(outcome.to_row())
    _write_graph(outcome)

    if outcome.teaches:
        _learn(outcome, inv)
    else:
        log.info("Outcome for %s recorded at confidence %.2f — below the learning "
                 "threshold, so it will not teach", investigation_id, outcome.confidence)
    return outcome


def _write_graph(outcome: IncidentOutcome) -> None:
    try:
        from src.graph import neo4j_client as graph
        oid = f"outcome:{outcome.investigation_id}"
        graph.upsert_node("IncidentOutcome", oid, {
            "name": f"Outcome {outcome.verdict}",
            "verdict": outcome.verdict,
            "confidence": outcome.confidence,
            "actualCause": outcome.actual_cause,
            "actualCategory": outcome.actual_category,
            "confirmedBy": outcome.confirmed_by,
            "recordedAt": outcome.recorded_at,
            "sourceKinds": [s.get("source", "") for s in outcome.sources],
            "source": "observability",
        })
        graph.upsert_relationship(
            "Incident", f"incident:{outcome.investigation_id}",
            "IncidentOutcome", oid, "HAS_OUTCOME",
            provenance={"source": "observability", "discoveredBy": "outcomes",
                        "confidence": outcome.confidence, "factType": "known"})
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not write outcome to graph: %s", exc)


def _learn(outcome: IncidentOutcome, investigation: dict) -> None:
    """Turn a trustworthy outcome into a retrieval case and a runbook candidate."""
    try:
        from src.observability import cases, promotion
        cases.record_case(outcome, investigation)
        promotion.on_outcome(outcome, investigation)
    except Exception as exc:  # noqa: BLE001
        log.warning("Learning step failed for %s: %s", outcome.investigation_id, exc)


# ── Automated signal collectors ──────────────────────────────────────────────

async def collect_verifier_signal(investigation_id: str, recovered: bool) -> IncidentOutcome:
    """obs_verifier_agent result. Weakest signal — records but does not teach."""
    return record(investigation_id, "verifier",
                  "confirmed" if recovered else "unknown",
                  detail="error rate returned to baseline" if recovered
                         else "signal has not recovered")


async def collect_pagerduty_signal(investigation_id: str, provider, incident_id: str,
                                   stated_cause: str) -> IncidentOutcome | None:
    notes = await provider.resolution_notes(incident_id)
    if not notes:
        return None
    text = (notes.get("note") or "").lower()
    stated = (stated_cause or "").lower()
    overlap = _token_overlap(text, stated)
    verdict = "confirmed" if overlap >= 0.25 else "partial" if overlap > 0 else "unknown"
    return record(investigation_id, "pagerduty_notes", verdict,
                  detail=f"resolution note overlap {overlap:.0%}: {notes.get('note','')[:200]}",
                  confirmed_by=notes.get("resolved_by", ""), incident_id=incident_id)


def collect_git_signal(investigation_id: str, implicated_version: str,
                       fix_commits: list[dict], recovered: bool) -> IncidentOutcome | None:
    """A rollback/fix landing on the implicated service + signal recovery = 0.7 confidence."""
    if not fix_commits or not recovered:
        return None
    subjects = " ".join((c.get("message") or "") for c in fix_commits).lower()
    hit = bool(implicated_version and implicated_version.lower() in subjects) or \
        any(k in subjects for k in ("revert", "rollback", "hotfix"))
    if not hit:
        return None
    return record(investigation_id, "git_correlation", "confirmed",
                  detail=f"{len(fix_commits)} fix/rollback commit(s) followed by recovery")


def _token_overlap(a: str, b: str) -> float:
    import re
    stop = {"the", "a", "an", "was", "is", "of", "to", "in", "and", "on", "for", "it"}
    ta = {t for t in re.findall(r"[a-z0-9]+", a) if len(t) > 2 and t not in stop}
    tb = {t for t in re.findall(r"[a-z0-9]+", b) if len(t) > 2 and t not in stop}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(tb)

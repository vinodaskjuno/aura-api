"""Case-based retrieval — past incidents become priors for new ones.

A case is a PRIOR, not evidence. It is a fact about a *past* incident, not about this
one. It goes into a separate, clearly labelled prompt section, and
`_validate_citations` rejects any case_* id appearing in evidence_ids. Without that
rule, "we've seen this before" launders a prior into a citation and the whole
evidence-backed guarantee rots from the inside.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from src.config_settings import get_settings

log = logging.getLogger(__name__)

# Score = signature Jaccard + service proximity + symptom overlap + recency.
_W_SIG, _W_SVC, _W_SYMPTOM, _W_RECENCY = 0.50, 0.25, 0.15, 0.10

SYMPTOM_KINDS = ("error_spike", "latency_knee", "restart_loop",
                 "saturation", "dependency_timeout", "deploy_nearby")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9<>.]+", (text or "").lower()) if len(t) > 2}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def symptom_shape(signatures: list[dict], anomalies: list[dict],
                  suspects: list[dict], pod_health: list[dict] | None = None) -> list[str]:
    """A small categorical vector — what KIND of incident this looks like."""
    shape: list[str] = []
    if any(s.get("level") in ("ERROR", "FATAL") and s.get("count", 0) >= 5
           for s in signatures):
        shape.append("error_spike")
    for a in anomalies:
        metric = (a.get("metric") or "").lower()
        if a.get("direction") == "up" and ("duration" in metric or "latency" in metric):
            shape.append("latency_knee")
        elif a.get("direction") == "up" and ("memory" in metric or "cpu" in metric):
            shape.append("saturation")
    blob = " ".join(s.get("signature", "") for s in signatures).lower()
    if any(k in blob for k in ("crashloopbackoff", "oomkilled", "restart")):
        shape.append("restart_loop")
    if any(k in blob for k in ("timeout", "connection refused", "circuit", "upstream")):
        shape.append("dependency_timeout")
    if pod_health:
        shape.append("restart_loop")
    if suspects and suspects[0].get("score", 0) >= 0.5:
        shape.append("deploy_nearby")
    return sorted(set(shape))


def fingerprint(service: str, signatures: list[str], shape: list[str]) -> str:
    raw = "|".join([service or "", "~".join(sorted(signatures)[:5]), "~".join(sorted(shape))])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def resolution_hash(category: str, resolution: str) -> str:
    return hashlib.sha256(f"{category}|{(resolution or '')[:200]}".encode()).hexdigest()[:12]


# ── Writing cases ────────────────────────────────────────────────────────────

def record_case(outcome, investigation: dict) -> dict | None:
    """Persist a resolved investigation as a retrievable case. Called only for
    outcomes above the learning-confidence threshold."""
    from src.observability import store

    root = investigation.get("rootCause") or {}
    service = investigation.get("serviceName", "")
    signatures = investigation.get("errorSignatures") or []
    shape = investigation.get("symptomShape") or []

    # A `wrong` verdict is recorded too — as a NEGATIVE case. "This was investigated
    # as a network fault and that was wrong" narrows the hypothesis space as much as
    # a positive one does, and `ruled_out` already produces that data for free.
    category = (outcome.actual_category or root.get("category", "")
                if outcome.verdict == "wrong" else root.get("category", ""))
    statement = (outcome.actual_cause or root.get("statement", "")
                 if outcome.verdict == "wrong" else root.get("statement", ""))

    case = {
        "caseId": f"case_{investigation.get('investigationId','')[:24]}",
        "createdAt": _now(),
        "investigationId": investigation.get("investigationId", ""),
        "serviceName": service,
        "title": investigation.get("title", ""),
        "fingerprint": fingerprint(service, signatures, shape),
        "errorSignatures": signatures[:10],
        "symptomShape": shape,
        "rootCauseStatement": statement,
        "rootCauseCategory": category,
        "wrongCategory": root.get("category", "") if outcome.verdict == "wrong" else "",
        "resolution": investigation.get("resolution", ""),
        "resolutionHash": resolution_hash(category, investigation.get("resolution", "")),
        "runbookId": investigation.get("runbookId", ""),
        "outcomeVerdict": outcome.verdict,
        "outcomeConfidence": str(outcome.confidence),
        "occurredAt": investigation.get("startedAt", ""),
        "sourceUrl": f"/observability?tab=investigate&investigation="
                     f"{investigation.get('investigationId','')}",
    }
    store.save_case(case)
    log.info("Recorded case %s (verdict=%s, confidence=%.2f)",
             case["caseId"], outcome.verdict, outcome.confidence)
    return case


# ── Retrieval ────────────────────────────────────────────────────────────────

def score_case(case: dict, service: str, signatures: list[str],
               shape: list[str]) -> tuple[float, list[str]]:
    """Pure scoring function — no I/O, so the harness can assert on it directly."""
    matched: list[str] = []

    case_sig_tokens = _tokens(" ".join(case.get("errorSignatures") or []))
    sig_tokens = _tokens(" ".join(signatures))
    sig_score = jaccard(sig_tokens, case_sig_tokens)
    if sig_score > 0.2:
        matched.append("signatures")

    case_service = case.get("serviceName", "")
    if service and case_service:
        if service.lower() == case_service.lower():
            svc_score = 1.0
            matched.append("service")
        else:
            svc_score = _graph_proximity(service, case_service)
            if svc_score:
                matched.append("service_neighbour")
    else:
        svc_score = 0.0

    case_shape = set(case.get("symptomShape") or [])
    shape_score = jaccard(set(shape), case_shape)
    if shape_score > 0:
        matched.append("symptom_shape")

    rec = _recency(case.get("occurredAt") or case.get("createdAt", ""))

    score = (_W_SIG * sig_score + _W_SVC * svc_score
             + _W_SYMPTOM * shape_score + _W_RECENCY * rec)
    return round(min(1.0, score), 3), matched


_NEIGHBOUR_CACHE: dict[str, dict] = {}


def _graph_proximity(a: str, b: str) -> float:
    try:
        if a not in _NEIGHBOUR_CACHE:
            from src.graph.neo4j_client import service_neighbours
            _NEIGHBOUR_CACHE[a] = service_neighbours(a, hops=2)
        n = _NEIGHBOUR_CACHE[a]
        if b in (n.get("downstream") or []) or b in (n.get("upstream") or []):
            return 0.5
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _recency(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - then).days
        return max(0.0, 1.0 - days / 180.0)
    except Exception:  # noqa: BLE001
        return 0.0


def retrieve(service: str, signatures: list[str], shape: list[str],
             exclude_investigation_id: str = "", limit: int = 5) -> dict:
    """Top-k similar past cases, plus empirical category priors.

    Stays inert below the corpus floor: a single anecdote dressed up as a prior is
    worse than no prior at all.
    """
    from src.observability import store

    s = get_settings()
    all_cases = store.list_cases(limit=500)
    all_cases = [c for c in all_cases
                 if c.get("investigationId") != exclude_investigation_id]
    corpus = len(all_cases)

    if corpus < s.observability_case_corpus_floor:
        return {"cases": [], "negative_cases": [], "category_priors": {},
                "corpus_size": corpus, "below_floor": True}

    min_conf = s.observability_learning_min_confidence
    usable = [c for c in all_cases
              if float(c.get("outcomeConfidence", 0) or 0) >= min_conf]

    scored = []
    for c in usable:
        score, matched = score_case(c, service, signatures, shape)
        if score <= 0.05:
            continue
        scored.append({
            "case_id": c.get("caseId", ""),
            "incident_id": c.get("investigationId", ""),
            "similarity": score,
            "matched_on": matched,
            "occurred_at": c.get("occurredAt", ""),
            "root_cause_statement": c.get("rootCauseStatement", ""),
            "root_cause_category": c.get("rootCauseCategory", ""),
            "outcome_verdict": c.get("outcomeVerdict", ""),
            "outcome_confidence": float(c.get("outcomeConfidence", 0) or 0),
            "resolution": c.get("resolution", ""),
            "runbook_id": c.get("runbookId", ""),
            "service": c.get("serviceName", ""),
            "source_url": c.get("sourceUrl", ""),
            "resolution_hash": c.get("resolutionHash", ""),
            "wrong_category": c.get("wrongCategory", ""),
        })
    scored.sort(key=lambda c: c["similarity"], reverse=True)

    all_positive = [c for c in scored if c["outcome_verdict"] in ("confirmed", "partial")]
    negative = [c for c in scored if c["outcome_verdict"] == "wrong"][:limit]

    # Priors are a base rate over DISTINCT resolutions across the whole matched set;
    # they must not be computed from the top-k display slice. Otherwise a service
    # flapping 20 times still crowds the rarer category out of the denominator, which
    # is the exact bias the dedupe exists to prevent.
    return {
        "cases": all_positive[:limit],
        "negative_cases": negative,
        "category_priors": category_priors(all_positive),
        "corpus_size": corpus,
        "below_floor": False,
    }


def category_priors(cases: list[dict]) -> dict[str, float]:
    """Empirical category distribution, deduped by DISTINCT RESOLUTION.

    One flapping service producing 40 identical incidents in a week would otherwise
    make `capacity` look like a 0.9 prior. Counting distinct resolutions instead of
    distinct incidents is what stops that.
    """
    seen: set[tuple] = set()
    counts: dict[str, int] = {}
    for c in cases:
        key = (c.get("service", ""), c.get("root_cause_category", ""),
               c.get("resolution_hash", ""))
        if key in seen:
            continue
        seen.add(key)
        cat = c.get("root_cause_category") or "unknown"
        counts[cat] = counts.get(cat, 0) + 1
    total = sum(counts.values())
    if not total:
        return {}
    return {k: round(v / total, 3) for k, v in
            sorted(counts.items(), key=lambda kv: kv[1], reverse=True)}


def forget(case_id: str) -> bool:
    """One-click revert. Learning artifacts are DATA, so this is a plain delete."""
    from src.observability import store
    ok = store.delete_case(case_id)
    if ok:
        log.info("Forgot case %s by explicit request", case_id)
    return ok

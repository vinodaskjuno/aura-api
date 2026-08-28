"""Citation validation — the evidence-link guarantee, enforced in code not prompt.

Prompting a model to cite its sources without verifying the citations is how you ship
confident fabrications. Every evidence_ids entry must resolve to a real id in the
evidence index; claims that fail are moved to `unsupported_claims`, stripped from the
root cause, and the agent result is downgraded to `partial`.

Case ids (case_*) are REJECTED here on purpose. A retrieved past incident is a prior,
not evidence about the present one. If cases were citable, "we've seen this before"
would launder a prior into a citation and the chips in the UI would stop meaning
anything.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

INLINE_RE = re.compile(r"\[\[ev:([A-Za-z0-9_-]+)\]\]")


def valid_ids(evidence_index: list[dict]) -> set[str]:
    return {e.get("evidenceId") or e.get("evidence_id", "") for e in evidence_index
            if (e.get("evidenceId") or e.get("evidence_id"))}


def _clean(ids, allowed: set[str]) -> tuple[list[str], list[str]]:
    kept, rejected = [], []
    for raw in ids or []:
        rid = str(raw).strip()
        if rid in allowed:
            if rid not in kept:
                kept.append(rid)
        else:
            rejected.append(rid)
    return kept, rejected


def validate_claim(claim: dict, allowed: set[str]) -> tuple[dict, list[str]]:
    """Strip unresolvable citations from one claim. Returns (claim, rejected_ids)."""
    kept, rejected = _clean(claim.get("evidence_ids"), allowed)

    # Inline [[ev:...]] tokens must resolve too, or the footnote renders as a dead chip.
    text = claim.get("statement") or claim.get("claim") or ""
    for match in INLINE_RE.findall(text):
        if match not in allowed:
            rejected.append(match)
            text = text.replace(f"[[ev:{match}]]", "")
        elif match not in kept:
            kept.append(match)

    out = {**claim, "evidence_ids": kept}
    if "statement" in claim:
        out["statement"] = text.strip()
    elif "claim" in claim:
        out["claim"] = text.strip()
    return out, rejected


def validate(output: dict, evidence_index: list[dict]) -> dict:
    """Enforce the guarantee across a whole obs_root_cause output.

    Mutates nothing; returns a new dict plus `citation_coverage`,
    `unsupported_claims` and `rejected_citations`.
    """
    allowed = valid_ids(evidence_index)
    unsupported: list[dict] = []
    rejected_all: list[str] = []
    total = 0
    cited = 0

    def handle(claim: dict, *, required: bool) -> dict | None:
        nonlocal total, cited
        cleaned, rejected = validate_claim(claim, allowed)
        rejected_all.extend(rejected)
        total += 1
        if cleaned["evidence_ids"]:
            cited += 1
            return cleaned
        unsupported.append({**cleaned, "reason": "no resolvable evidence"})
        return None if required else cleaned

    result = dict(output)

    root = result.get("root_cause")
    if isinstance(root, dict):
        kept_root = handle(root, required=True)
        # A root cause with no evidence is not a root cause. Do not soften this.
        result["root_cause"] = kept_root

    for key in ("contributing_factors", "ruled_out", "recommended_actions"):
        items = result.get(key)
        if isinstance(items, list):
            kept = [c for c in (handle(i, required=(key == "contributing_factors"))
                                for i in items if isinstance(i, dict)) if c]
            result[key] = kept

    impact = result.get("impact")
    if isinstance(impact, dict):
        cleaned, rejected = validate_claim(impact, allowed)
        rejected_all.extend(rejected)
        result["impact"] = cleaned

    result["citation_coverage"] = round(cited / total, 3) if total else 0.0
    result["unsupported_claims"] = unsupported
    result["rejected_citations"] = sorted(set(rejected_all))

    if rejected_all:
        log.warning("Rejected %d unresolvable citation(s): %s",
                    len(rejected_all), sorted(set(rejected_all))[:10])
    return result


def to_findings(output: dict, agent: str = "obs_root_cause") -> list[dict]:
    """Flatten a validated output into the Finding shape the SPA renders."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    findings: list[dict] = []
    rank = 0

    def add(claim: dict, status: str, confidence: float) -> None:
        nonlocal rank
        rank += 1
        text = claim.get("statement") or claim.get("claim") or claim.get("action", "")
        ev = claim.get("evidence_ids") or []
        findings.append({
            "findingId": f"f{rank}",
            "rank": rank,
            "claim": text,
            "confidence": round(float(claim.get("confidence", confidence) or confidence), 3),
            "status": status if ev else "unsupported",
            "evidenceIds": ev,
            "agent": agent,
            "category": claim.get("category", ""),
            "runbookStepId": claim.get("runbook_step_id", ""),
            "caseIds": claim.get("case_ids", []),
            "createdAt": now,
        })

    root = output.get("root_cause")
    if isinstance(root, dict):
        add(root, "root_cause", 0.8)
    for c in output.get("contributing_factors") or []:
        add(c, "supported", 0.6)
    for c in output.get("ruled_out") or []:
        add(c, "refuted", 0.5)
    for c in output.get("unsupported_claims") or []:
        add(c, "unsupported", 0.2)
    return findings

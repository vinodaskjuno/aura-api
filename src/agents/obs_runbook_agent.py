"""obs_runbook — retrieve the matching runbook and mark steps the evidence satisfies."""
from __future__ import annotations

import logging

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent
from src.observability import runbooks
from src.observability.types import InvestigationSpec

log = logging.getLogger(__name__)

# Cheap keyword -> signal mapping so a step can be auto-satisfied from evidence.
_STEP_HINTS = {
    "alert": ("events",), "acknowledge": ("events",),
    "diagnos": ("logs", "metrics"), "log": ("logs",),
    "metric": ("metrics",), "trace": ("traces",),
    "root cause": ("logs", "metrics", "events"),
    "deploy": ("events",), "rollback": ("events",),
    "verif": ("metrics",), "monitor": ("metrics",),
}


class ObsRunbookAgent(BaseAgent):
    name = "obs_runbook"
    description = ("Retrieve the runbook matching this incident and instantiate its "
                   "steps against the evidence already collected")

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        spec_raw = context.extra.get("investigation")
        spec = (spec_raw if isinstance(spec_raw, InvestigationSpec)
                else InvestigationSpec.from_dict(spec_raw or {}))

        collector = context.prior_results.get("obs_signal_collector")
        signatures = [s["signature"] for s in
                      ((collector.output.get("top_error_signatures") if collector else None) or [])]
        service = spec.service or (collector.output.get("service") if collector else "")
        evidence = (collector.output.get("evidence_index") if collector else []) or []

        if spec.runbook_id:
            rb = runbooks.get_runbook(spec.runbook_id) or runbooks.template_runbook(service)
            matches = [rb]
        else:
            matches = runbooks.search(service=service, signatures=signatures,
                                      query=spec.symptom, limit=5)
        best = matches[0]
        steps = self._instantiate(best.get("steps") or [], evidence)
        satisfied = sum(1 for s in steps if s["status"] == "satisfied")

        result.log(f"Matched runbook '{best.get('title','')}' "
                   f"(score {best.get('match_score', 0)}); {satisfied}/{len(steps)} "
                   f"step(s) already satisfied by collected evidence")

        result.output = {
            "runbook_id": best.get("runbookId") or best.get("externalId", ""),
            "title": best.get("title", ""),
            "origin": best.get("origin", "human"),
            "status": best.get("status", "active"),
            "source": "template" if best.get("origin") == "template" else "neo4j",
            "source_url": best.get("sourceUrl", ""),
            "matched_on": best.get("matched_on") or [],
            "match_score": best.get("match_score", 0.0),
            "steps": steps,
            "steps_satisfied": satisfied,
            "alternatives": [{"runbook_id": m.get("runbookId") or m.get("externalId", ""),
                              "title": m.get("title", ""),
                              "score": m.get("match_score", 0.0)}
                             for m in matches[1:4]],
        }
        return result.finish("success")

    def _instantiate(self, steps: list[dict], evidence: list[dict]) -> list[dict]:
        by_kind: dict[str, list[str]] = {}
        for e in evidence:
            by_kind.setdefault(e.get("signal", ""), []).append(e.get("evidenceId", ""))

        out = []
        for i, step in enumerate(steps, start=1):
            title = (step.get("title") or "").lower()
            desc = (step.get("description") or "").lower()
            wanted: set[str] = set()
            for hint, signals in _STEP_HINTS.items():
                if hint in title or hint in desc:
                    wanted.update(signals)
            ev_ids: list[str] = []
            for sig in wanted:
                ev_ids.extend(by_kind.get(sig, [])[:3])
            out.append({
                "order": step.get("order", i),
                "id": step.get("id", f"step-{i}"),
                "title": step.get("title", ""),
                "description": step.get("description", ""),
                "checklist": step.get("checklist", []),
                "status": "satisfied" if ev_ids else "pending",
                "evidence_ids": ev_ids[:5],
            })
        return out

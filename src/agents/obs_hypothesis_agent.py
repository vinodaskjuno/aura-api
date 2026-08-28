"""obs_hypothesis — generate COMPETING hypotheses, each with a discriminating check.

First LLM call in the pipeline. It sees the correlator's deterministic timeline and
the evidence summaries — never raw logs, never payloads.
"""
from __future__ import annotations

import json
import logging

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent
from src.observability.llm import invoke_masked, parse_json_block
from src.observability.types import InvestigationSpec

log = logging.getLogger(__name__)

SYSTEM = """You are an SRE incident analyst. You are given a deterministic correlation \
of signals from a production incident: error signatures, metric change points, recent \
deploys, and blast radius.

Produce 3-5 COMPETING hypotheses. They must be genuinely different explanations, not \
restatements of one another. For each, give a discriminating check: a specific query \
whose result would tell the hypotheses apart.

Rules:
- Cite evidence by the exact evidence_id strings given to you. Never invent an id.
- PAST CASES are priors about earlier incidents, NOT evidence about this one. Never \
put a case id in supporting_evidence_ids or contradicting_evidence_ids.
- Identifiers of the form AURA_POD_01 are masked. Reason about them as opaque stable \
names; do not attempt to guess the real value.
- Respond with JSON only, no prose, no code fences.

Schema:
{"hypotheses":[{"hypothesis_id":"h1","statement":"...",
  "category":"deploy|config|capacity|dependency|data|infra|external",
  "prior":0.0-1.0,"supporting_evidence_ids":["ev_..."],
  "contradicting_evidence_ids":["ev_..."],
  "discriminating_check":{"description":"...","signal":"logs|metrics|traces|events",
    "query":"...","expect":"..."}}]}"""


class ObsHypothesisAgent(BaseAgent):
    name = "obs_hypothesis"
    description = ("Generate competing root-cause hypotheses with discriminating "
                   "checks, informed by correlated signals and past cases")

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        spec_raw = context.extra.get("investigation")
        spec = (spec_raw if isinstance(spec_raw, InvestigationSpec)
                else InvestigationSpec.from_dict(spec_raw or {}))

        collector = context.prior_results.get("obs_signal_collector")
        corr = context.prior_results.get("obs_correlator")
        runbook = context.prior_results.get("obs_runbook")
        cases_res = context.prior_results.get("obs_case_retrieval")
        if not collector or not corr:
            result.log("Missing collector/correlator results")
            return result.finish("failed")

        prompt = self._build_prompt(spec, collector.output, corr.output,
                                    runbook.output if runbook else {},
                                    cases_res.output if cases_res else {})
        text, call = await invoke_masked(
            system=SYSTEM, user=prompt,
            investigation_id=spec.investigation_id, agent=self.name, stage=3,
            user_id=context.user_id, max_tokens=8000,
        )
        if call.error:
            result.log(f"LLM call failed: {call.error}")
            result.output = {"hypotheses": [], "llm_calls": [call.to_dict()],
                             "cost_usd": 0.0, "llm_error": call.error}
            return result.finish("partial")

        parsed = parse_json_block(text) or {}
        hypotheses = parsed.get("hypotheses") or []

        # Cases are priors. Strip any case id the model tried to cite as evidence.
        allowed = {e.get("evidenceId") for e in collector.output.get("evidence_index") or []}
        for h in hypotheses:
            for key in ("supporting_evidence_ids", "contradicting_evidence_ids"):
                h[key] = [i for i in (h.get(key) or []) if i in allowed]

        result.log(f"{len(hypotheses)} competing hypothesis/es "
                   f"(${call.cost_usd:.4f}, {call.latency_ms}ms)")
        result.output = {
            "hypotheses": hypotheses,
            "model_id": call.model_id,
            "tokens": {"input": call.input_tokens, "output": call.output_tokens},
            "cost_usd": call.cost_usd,
            "llm_calls": [call.to_dict()],
        }
        return result.finish("success" if hypotheses else "partial")

    def _build_prompt(self, spec, collected: dict, corr: dict,
                      runbook: dict, cases: dict) -> str:
        parts = [
            f"## Incident\nService: {collected.get('service','')}",
            f"Symptom: {spec.symptom or '(not stated)'}",
            f"Window: {spec.window.start} .. {spec.window.end}",
            f"Severity: {spec.severity}", "",
            "## Error signatures",
            json.dumps(collected.get("top_error_signatures", [])[:8], indent=1), "",
            "## Metric change points",
            json.dumps(corr.get("anomalies", [])[:8], indent=1), "",
            "## Suspect changes (deploys / config), highest score first",
            json.dumps(corr.get("suspect_changes", [])[:5], indent=1), "",
            "## Blast radius", json.dumps(corr.get("blast_radius", {}), indent=1), "",
            "## Trace summary", json.dumps(corr.get("trace_summary", {}), indent=1), "",
            "## Evidence index (cite ONLY these ids)",
            json.dumps([{"evidence_id": e.get("evidenceId"), "signal": e.get("signal"),
                         "t": e.get("timestamp"), "summary": e.get("summary")}
                        for e in (collected.get("evidence_index") or [])[:80]], indent=1),
        ]
        if runbook.get("title"):
            parts += ["", "## Matched runbook",
                      json.dumps({"title": runbook.get("title"),
                                  "steps": [s.get("title") for s in runbook.get("steps", [])]},
                                 indent=1)]
        if cases.get("cases") or cases.get("negative_cases"):
            parts += ["", "## PRIOR KNOWLEDGE — past incidents (NOT evidence, NOT citable)",
                      json.dumps({
                          "similar_resolved": [
                              {"when": c.get("occurred_at"),
                               "similarity": c.get("similarity"),
                               "root_cause": c.get("root_cause_statement"),
                               "category": c.get("root_cause_category")}
                              for c in cases.get("cases", [])[:5]],
                          "previously_wrong_conclusions": [
                              {"wrongly_concluded": c.get("wrong_category"),
                               "actual": c.get("root_cause_category"),
                               "statement": c.get("root_cause_statement")}
                              for c in cases.get("negative_cases", [])[:5]],
                          "empirical_category_priors": cases.get("category_priors", {}),
                      }, indent=1),
                      "",
                      "Treat the above as background base rates only. The evidence for "
                      "THIS incident is what decides the answer."]
        return "\n".join(parts)

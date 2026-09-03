"""obs_root_cause — pick the winning hypothesis and state an evidence-cited cause.

Writes the first OPERATIONS_LABELS nodes the repo has ever produced: Incident, Alert,
and their provenance-stamped relationships.

Every claim is validated against the evidence index by
`src/observability/citations.py`. Anything that cannot be resolved is moved to
`unsupported_claims`, stripped from the root cause, and NOT written to Neo4j.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent, Triple
from src.observability import citations
from src.observability.llm import invoke_masked, parse_json_block
from src.observability.types import InvestigationSpec

log = logging.getLogger(__name__)

SYSTEM = """You are an SRE incident analyst writing the final root-cause analysis.

You are given competing hypotheses, a deterministic correlation of signals, and an \
evidence index. Decide which hypothesis the evidence supports, and say so.

Rules — these are enforced programmatically, so violating them silently loses you the claim:
- EVERY claim must carry evidence_ids drawn from the evidence index. A claim with no \
resolvable evidence id is discarded.
- Never invent an evidence id. Never cite a case id (case_*) — past incidents are \
priors, not evidence about this incident.
- You may embed inline citations in statements as [[ev:ev_abc123]].
- If the evidence does not support a confident conclusion, say so and give a lower \
confidence. A hedged, well-cited answer is far more useful than a confident guess.
- Identifiers like AURA_POD_01 are masked. Use them verbatim; do not guess real values.
- Respond with JSON only. No prose, no code fences.

Schema:
{"root_cause":{"statement":"...","category":"deploy|config|capacity|dependency|data|infra|external",
   "confidence":0.0-1.0,"evidence_ids":["ev_..."],"hypothesis_id":"h1"},
 "contributing_factors":[{"statement":"...","evidence_ids":["ev_..."]}],
 "ruled_out":[{"statement":"...","reason":"...","evidence_ids":["ev_..."]}],
 "impact":{"statement":"...","services_affected":["..."],"user_facing":true,"evidence_ids":["ev_..."]},
 "recommended_actions":[{"action":"...","owner_hint":"...","risk":"low|medium|high",
   "runbook_step_id":"","evidence_ids":["ev_..."]}],
 "timeline_summary":["..."]}"""


class ObsRootCauseAgent(BaseAgent):
    name = "obs_root_cause"
    description = ("Produce an evidence-cited root cause, with every conclusion linked "
                   "to the data that supports it")

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        spec_raw = context.extra.get("investigation")
        spec = (spec_raw if isinstance(spec_raw, InvestigationSpec)
                else InvestigationSpec.from_dict(spec_raw or {}))

        collector = context.prior_results.get("obs_signal_collector")
        corr = context.prior_results.get("obs_correlator")
        hypo = context.prior_results.get("obs_hypothesis")
        runbook = context.prior_results.get("obs_runbook")
        if not collector:
            result.log("No collected signals — cannot produce a root cause")
            return result.finish("failed")

        evidence_index = collector.output.get("evidence_index") or []
        prompt = self._build_prompt(spec, collector.output,
                                    corr.output if corr else {},
                                    hypo.output if hypo else {},
                                    runbook.output if runbook else {})

        text, call = await invoke_masked(
            system=SYSTEM, user=prompt,
            investigation_id=spec.investigation_id, agent=self.name, stage=4,
            user_id=context.user_id, max_tokens=16000,
        )
        if call.error:
            # Without this the UI shows an empty findings list and the operator
            # concludes the feature is broken, when the real cause is a billing,
            # auth or model-configuration problem they can actually fix.
            result.log(f"LLM call failed: {call.error}")
            result.output = {"root_cause": None, "llm_calls": [call.to_dict()],
                             "cost_usd": 0.0, "citation_coverage": 0.0,
                             "llm_error": call.error}
            return result.finish("partial")

        parsed = parse_json_block(text) or {}

        # ── The guarantee. Not a prompt instruction — a gate. ────────────────
        validated = citations.validate(parsed, evidence_index)
        findings = citations.to_findings(validated, agent=self.name)

        coverage = validated.get("citation_coverage", 0.0)
        rejected = validated.get("rejected_citations", [])
        result.log(f"Citation coverage {coverage:.0%}"
                   + (f"; rejected {len(rejected)} unresolvable id(s)" if rejected else ""))

        emit = context.extra.get("_emit")
        if emit:
            for f in findings:
                await emit({"type": "finding", "finding": f})

        report_ref = self._save_report(spec, context, validated)
        if validated.get("root_cause"):
            self._write_graph(spec, context, collector.output, validated, result)

        status = "success" if validated.get("root_cause") else "partial"
        if validated.get("unsupported_claims") and not validated.get("root_cause"):
            result.log("No claim survived citation validation — downgraded to partial")

        result.output = {
            **validated,
            "investigation_id": spec.investigation_id,
            "incident_id": spec.incident_id,
            "service": collector.output.get("service", ""),
            "findings": findings,
            "report_ref": report_ref,
            "model_id": call.model_id,
            "cost_usd": call.cost_usd + (hypo.output.get("cost_usd", 0.0) if hypo else 0.0),
            "llm_calls": [call.to_dict()],
            "unmask_incomplete": call.unmask_incomplete,
        }
        return result.finish(status)

    def _build_prompt(self, spec, collected, corr, hypo, runbook) -> str:
        parts = [
            f"## Incident\nService: {collected.get('service','')}",
            f"Symptom: {spec.symptom or '(not stated)'}",
            f"Window: {spec.window.start} .. {spec.window.end}", "",
            "## Competing hypotheses",
            json.dumps(hypo.get("hypotheses", []), indent=1), "",
            "## Correlation (deterministic — trust these)",
            json.dumps({"anomalies": corr.get("anomalies", [])[:8],
                        "suspect_changes": corr.get("suspect_changes", [])[:5],
                        "error_onset_t": corr.get("error_onset_t"),
                        "blast_radius": corr.get("blast_radius", {}),
                        "symptom_shape": corr.get("symptom_shape", [])}, indent=1), "",
            "## Error signatures",
            json.dumps(collected.get("top_error_signatures", [])[:8], indent=1), "",
            "## Evidence index — cite ONLY these ids",
            json.dumps([{"evidence_id": e.get("evidenceId"), "signal": e.get("signal"),
                         "t": e.get("timestamp"), "service": e.get("service"),
                         "summary": e.get("summary")}
                        for e in (collected.get("evidence_index") or [])[:100]], indent=1),
        ]
        if runbook.get("steps"):
            parts += ["", "## Runbook steps (reference runbook_step_id where an action maps)",
                      json.dumps([{"id": s.get("id"), "title": s.get("title"),
                                   "status": s.get("status")}
                                  for s in runbook.get("steps", [])], indent=1)]
        return "\n".join(parts)

    def _save_report(self, spec, context, validated: dict) -> dict:
        try:
            from src.storage.s3_client import put_json
            key = (f"observability/{spec.project_id or context.project_id or 'global'}"
                   f"/{spec.investigation_id}/rca.json")
            uri = put_json("exports", key, validated)
            return {"bucket": "exports", "key": key, "uri": uri}
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not save RCA report: %s", exc)
            return {}

    def _write_graph(self, spec, context, collected: dict, validated: dict,
                     result: AgentResult) -> None:
        """First code in this repo to actually create OPERATIONS_LABELS nodes."""
        try:
            from src.graph import neo4j_client as graph
        except Exception as exc:  # noqa: BLE001
            log.debug("graph unavailable: %s", exc)
            return

        root = validated.get("root_cause") or {}
        service = collected.get("service", "") or "UnknownService"
        inc_eid = f"incident:{spec.investigation_id}"
        now = datetime.now(timezone.utc).isoformat()

        try:
            graph.upsert_node("Incident", inc_eid, {
                "name": spec.title or root.get("statement", "")[:120],
                "title": spec.title or root.get("statement", "")[:120],
                "serviceName": service,
                "severity": spec.severity,
                "startedAt": spec.window.start,
                "detectedAt": collected.get("error_onset_t") or spec.window.start,
                "analyzedAt": now,
                "rootCauseStatement": root.get("statement", ""),
                "rootCauseCategory": root.get("category", ""),
                "confidence": float(root.get("confidence", 0) or 0),
                "citationCoverage": validated.get("citation_coverage", 0.0),
                "errorSignatures": [s["signature"] for s in
                                    collected.get("top_error_signatures", [])][:10],
                "source": "observability",
                "investigationId": spec.investigation_id,
            })
            graph.upsert_node("Service", f"service:{service}", {"name": service})

            evidence_urls = [e.get("sourceUrl", "") for e in
                             (collected.get("evidence_index") or [])
                             if e.get("evidenceId") in (root.get("evidence_ids") or [])]
            prov = {
                "source": "observability",
                "sourceRecordId": spec.investigation_id,
                "discoveredBy": self.name,
                "confidence": float(root.get("confidence", 0) or 0),
                "evidence": (root.get("evidence_ids") or []) + [u for u in evidence_urls if u],
                # Only a confirmed discriminating check earns "inferred".
                "factType": "hypothesis",
            }
            graph.upsert_relationship("Service", f"service:{service}",
                                      "Incident", inc_eid, "HAS_INCIDENT", provenance_props=prov)
            result.kg_updates.append(Triple(inc_eid, "AFFECTS", f"service:{service}"))

            for i, ev in enumerate(collected.get("evidence_index") or []):
                if ev.get("signal") != "events":
                    continue
                alert_eid = f"alert:{ev.get('evidenceId','')}"
                graph.upsert_node("Alert", alert_eid, {
                    "name": ev.get("title", "")[:120],
                    "timestamp": ev.get("timestamp", ""),
                    "serviceName": ev.get("service", ""),
                    "sourceUrl": ev.get("sourceUrl", ""),
                    "source": ev.get("provider", "observability"),
                })
                graph.upsert_relationship("Incident", inc_eid, "Alert", alert_eid,
                                          "TRIGGERED_BY", provenance_props=prov)
                result.kg_updates.append(Triple(inc_eid, "TRIGGERED_BY", alert_eid))
                if i > 20:
                    break

            for sus in (validated.get("suspect_changes") or
                        collected.get("deploy_candidates") or [])[:3]:
                version = sus.get("version") or ""
                if not version:
                    continue
                cr_eid = f"change:{service}:{version}"
                graph.upsert_node("ChangeRequest", cr_eid, {
                    "name": f"{service} {version}", "version": version,
                    "actor": sus.get("actor", ""), "timestamp": sus.get("t", ""),
                    "source": "observability",
                })
                graph.upsert_relationship("Incident", inc_eid, "ChangeRequest", cr_eid,
                                          "CAUSED_BY", provenance_props=prov)
                result.kg_updates.append(Triple(inc_eid, "CAUSED_BY", cr_eid))

            result.log(f"Wrote Incident/Alert/ChangeRequest nodes for {service}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not write incident graph: %s", exc)

"""obs_correlator — deterministic timeline, change points, deploy alignment.

NO LLM. This is what the eval harness scores exactly, and it is what turns the
model's job from "invent a causal story" into "explain this timeline".
"""
from __future__ import annotations

import logging

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent
from src.observability import cases, correlation
from src.observability.types import InvestigationSpec

log = logging.getLogger(__name__)


class ObsCorrelatorAgent(BaseAgent):
    name = "obs_correlator"
    description = ("Correlate collected signals into a timeline, detect metric change "
                   "points, and align them against deploys and config changes")

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        prior = context.prior_results.get("obs_signal_collector")
        if not prior:
            result.log("obs_signal_collector produced no result — nothing to correlate")
            return result.finish("failed")

        out = prior.output
        scratch = context.extra.get("_scratch") or {}
        series = scratch.get("series") or []
        events = scratch.get("events") or []
        evidence = scratch.get("evidence") or []

        # Map natural keys -> evidence ids so every derived claim can cite its source.
        ev_by_key: dict[str, str] = {}
        for e in evidence:
            key = (e.payload.get("series_id") or e.payload.get("kind")
                   or e.evidence_id)
            ev_by_key[e.evidence_id] = e.evidence_id
        for s_ in series:
            for e in evidence:
                if e.signal == "metrics" and s_.metric in e.title:
                    ev_by_key[s_.series_id] = e.evidence_id
                    break
        for ev in events:
            for e in evidence:
                if e.signal == "events" and ev.title in e.title:
                    ev_by_key[ev.event_id] = e.evidence_id
                    break

        anomalies = correlation.find_anomalies(series, ev_by_key)
        onset = out.get("error_onset_t")
        suspects = correlation.align_changes(events, onset, anomalies, ev_by_key)
        timeline = correlation.build_timeline(evidence)
        service = out.get("service", "")
        radius = correlation.blast_radius(service) if service else {
            "upstream": [], "downstream": [], "hops": 0, "source": "skipped"}

        signatures = out.get("top_error_signatures") or []
        shape = cases.symptom_shape(signatures, anomalies, suspects)

        result.log(f"{len(anomalies)} anomaly/ies, {len(suspects)} suspect change(s), "
                   f"symptom shape: {', '.join(shape) or 'none'}")

        result.output = {
            "timeline": timeline,
            "anomalies": anomalies,
            "suspect_changes": suspects,
            "blast_radius": radius,
            "error_onset_t": onset,
            "symptom_shape": shape,
            "error_signatures": [s_["signature"] for s_ in signatures],
            "trace_summary": out.get("trace_summary", {}),
        }
        scratch["symptom_shape"] = shape
        return result.finish("success")

"""obs_case_retrieval — past incidents become priors for this one.

A retrieved case is a PRIOR, never evidence. Case ids are handed to the hypothesis
agent in a separate prompt section, and _validate_citations rejects any case_* id
that turns up in evidence_ids.
"""
from __future__ import annotations

import logging

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent
from src.observability import cases
from src.observability.types import InvestigationSpec

log = logging.getLogger(__name__)


class ObsCaseRetrievalAgent(BaseAgent):
    name = "obs_case_retrieval"
    description = ("Retrieve similar past incidents and their confirmed outcomes as "
                   "priors for the current investigation")

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        spec_raw = context.extra.get("investigation")
        spec = (spec_raw if isinstance(spec_raw, InvestigationSpec)
                else InvestigationSpec.from_dict(spec_raw or {}))

        corr = context.prior_results.get("obs_correlator")
        collector = context.prior_results.get("obs_signal_collector")
        signatures = (corr.output.get("error_signatures") if corr else None) or \
                     [s["signature"] for s in
                      ((collector.output.get("top_error_signatures") if collector else None) or [])]
        shape = (corr.output.get("symptom_shape") if corr else None) or []
        service = spec.service or (collector.output.get("service") if collector else "")

        found = cases.retrieve(service, signatures, shape,
                               exclude_investigation_id=spec.investigation_id)

        if found.get("below_floor"):
            result.log(f"Case corpus has {found['corpus_size']} entries — below the "
                       f"floor, so no priors are injected")
        else:
            result.log(f"{len(found['cases'])} similar case(s), "
                       f"{len(found['negative_cases'])} negative case(s) "
                       f"from a corpus of {found['corpus_size']}")

        result.output = found
        return result.finish("success")

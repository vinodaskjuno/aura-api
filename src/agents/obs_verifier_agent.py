"""obs_verifier — did the signal actually recover after the fix window?

Weakest outcome source (weight 0.3): it confirms the incident ended, not that the
diagnosis was right. Recorded, but below the learning threshold on its own.
"""
from __future__ import annotations

import logging

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent
from src.observability import outcomes
from src.observability.registry import resolve_providers
from src.observability.types import InvestigationSpec, LogQuery, MetricQuery, TimeWindow

log = logging.getLogger(__name__)


class ObsVerifierAgent(BaseAgent):
    name = "obs_verifier"
    description = ("Re-query the incident signals after a fix window and record whether "
                   "they returned to baseline")

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        spec_raw = context.extra.get("investigation")
        spec = (spec_raw if isinstance(spec_raw, InvestigationSpec)
                else InvestigationSpec.from_dict(spec_raw or {}))
        service = spec.service
        if not service:
            result.log("No service to verify")
            return result.finish("partial")

        after = TimeWindow.last(int(context.extra.get("verify_minutes", 30)))
        providers = resolve_providers(context.user_id, spec.project_id or context.project_id)

        errors_after = 0
        checked = 0
        for p in providers:
            if not p.supports("logs"):
                continue
            page = await p.query_logs(LogQuery(service=service, window=after, limit=200))
            if page.error or page.unsupported:
                continue
            checked += 1
            errors_after += sum(1 for r in page.records if r.level in ("ERROR", "FATAL"))

        collector = context.prior_results.get("obs_signal_collector")
        before = 0
        if collector:
            before = sum(s.get("count", 0) for s in
                         collector.output.get("top_error_signatures", [])
                         if s.get("level") in ("ERROR", "FATAL"))

        recovered = checked > 0 and (errors_after == 0 or
                                     (before > 0 and errors_after < before * 0.25))
        result.log(f"Post-fix window: {errors_after} error(s) vs {before} during the "
                   f"incident — {'recovered' if recovered else 'not recovered'}")

        if checked:
            outcome = outcomes.record(
                spec.investigation_id, "verifier",
                "confirmed" if recovered else "unknown",
                detail=f"errors before={before} after={errors_after}",
                service_name=service)
            result.log(f"Outcome now: {outcome.verdict} at {outcome.confidence:.2f} "
                       f"confidence ({'teaches' if outcome.teaches else 'does not teach'})")

        result.output = {
            "recovered": recovered, "errors_after": errors_after,
            "errors_before": before, "providers_checked": checked,
            "window": after.to_dict(),
        }
        return result.finish("success" if checked else "partial")

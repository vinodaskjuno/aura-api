"""Investigation runner — a SEPARATE pipeline from the ontology DAG.

Deliberately not extra stages in dag.py, for three concrete reasons:
  1. run_dag unconditionally calls create_version_record() — an ontology-versioning
     side effect that is meaningless here and pollutes version history.
  2. It aggregates nodes_added/rels_added into dag_done. Investigations don't
     produce those numbers.
  3. agents_override filters WITHIN existing stages. An SRE-only override would find
     zero overlap with all six stages and return a zero-result summary. The mechanism
     itself proves these are two pipelines.

Event names reuse dag.py's vocabulary verbatim (dag_start / stage_start / agent_start
/ agent_done / stage_done / dag_done / error) plus evidence / finding / cost, so the
SPA drives both feeds through ONE reducer. The socket path separates them.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from src.agents.base_agent import AgentContext, AgentResult
from src.orchestrator.dag_runtime import (
    Emitter, SeqEmitter, Stage, make_context, null_emitter, run_stages,
)
from src.observability import store
from src.observability.types import InvestigationSpec, TimeWindow

log = logging.getLogger(__name__)

_INVESTIGATION_STAGES: list[Stage] = [
    (1, "Collect Signals", ["obs_signal_collector"],                        True),
    (2, "Correlate",       ["obs_correlator"],                              True),
    (3, "Recall & Match",  ["obs_case_retrieval", "obs_runbook"],           False),
    (4, "Hypothesize",     ["obs_hypothesis"],                              True),
    (5, "Root Cause",      ["obs_root_cause"],                              True),
    (6, "Recommend",       ["remediation_agent"],                           True),
]

# NOT added to dag.py::COMMAND_AGENTS — routers/commands.py builds _VALID_COMMANDS
# from its keys and routes everything to run_dag, which is the wrong runner.
INVESTIGATION_COMMANDS: dict[str, list[str]] = {
    "investigate": [],                       # empty = every stage
    "investigate-logs": ["obs_signal_collector", "obs_correlator"],
    "investigate-deploy": ["obs_signal_collector", "obs_correlator", "obs_hypothesis"],
    "runbook": ["obs_signal_collector", "obs_runbook"],
    "postmortem": ["obs_signal_collector", "obs_correlator", "obs_root_cause"],
    "verify": ["obs_verifier"],
}

_ALL_STAGES_WITH_VERIFY = _INVESTIGATION_STAGES + [
    (7, "Verify", ["obs_verifier"], True),
]


def new_investigation_id() -> str:
    return f"INV-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


def build_spec(body: dict, project_id: str = "") -> InvestigationSpec:
    if body.get("start") and body.get("end"):
        window = TimeWindow(start=body["start"], end=body["end"])
    else:
        window = TimeWindow.last(int(body.get("window_minutes") or 60))
    services = body.get("services") or ([body["service"]] if body.get("service") else [])
    return InvestigationSpec(
        investigation_id=body.get("investigation_id") or new_investigation_id(),
        title=body.get("title") or (f"{services[0]} investigation" if services else "Investigation"),
        services=services,
        window=window,
        symptom=body.get("symptom", ""),
        incident_id=body.get("incident_id", ""),
        severity=body.get("severity", "high"),
        provider_ids=body.get("provider_ids") or [],
        runbook_id=body.get("runbook_id", ""),
        project_id=body.get("project_id") or project_id,
        filter=body.get("filter", ""),
        masking_enabled=bool(body.get("masking_enabled", True)),
    )


async def run_investigation(
    spec: InvestigationSpec,
    *,
    user_id: str,
    username: str,
    role: str,
    session_id: str,
    project_id: str | None = None,
    emit: Emitter | None = None,
    command: str | None = None,
    agents_override: list[str] | None = None,
    include_verify: bool = False,
) -> dict[str, Any]:
    """Execute an investigation. Never raises — failures land in the summary."""
    run_id = f"inv-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{session_id[:8]}"
    seq = SeqEmitter(emit or null_emitter(), investigationId=spec.investigation_id)

    stages = _ALL_STAGES_WITH_VERIFY if include_verify else _INVESTIGATION_STAGES
    if agents_override is None and command:
        override = INVESTIGATION_COMMANDS.get(command)
        agents_override = override or None

    total_agents = sum(len(s[2]) for s in stages
                       if agents_override is None
                       or any(a in agents_override for a in s[2]))

    store.update_investigation(spec.investigation_id,
                               {"status": "running", "runId": run_id,
                                "startedAt": store.now()})

    await seq({"type": "dag_start", "runId": run_id,
               "investigationId": spec.investigation_id,
               "total_stages": len(stages), "total_agents": total_agents,
               "title": spec.title, "service": spec.service})

    # remediation_agent (stage 6) reads extra["incident_id"] and would no-op without it.
    # make_context() does `{**extra, ...}` per stage, so the top-level dict is COPIED
    # and mutations to it do not reach later agents. `_scratch` is a nested dict, so
    # the copy shares its reference — that is how normalized objects (which are not
    # JSON-serialisable into AgentResult.output) travel between stages.
    scratch: dict[str, Any] = {}
    extra: dict[str, Any] = {
        "investigation": spec,
        "_emit": seq,
        "_scratch": scratch,
        "incident_id": spec.incident_id or f"incident:{spec.investigation_id}",
    }

    def _ctx(prior: dict[str, AgentResult]) -> AgentContext:
        return make_context(
            intent=spec.symptom or spec.title,
            user_id=user_id, username=username, role=role,
            project_id=spec.project_id or project_id, session_id=session_id,
            version_id=None, extra=extra, prior_results=prior,
        )

    try:
        outcome = await run_stages(stages, _ctx, seq, agents_override)
    except Exception as exc:  # noqa: BLE001
        log.exception("Investigation %s failed", spec.investigation_id)
        await seq({"type": "error", "message": str(exc)})
        store.update_investigation(spec.investigation_id,
                                   {"status": "failed", "error": str(exc)[:500],
                                    "completedAt": store.now()})
        return {"run_id": run_id, "investigation_id": spec.investigation_id,
                "status": "failed", "error": str(exc)}

    summary = _summarize(spec, run_id, outcome, extra)
    await seq({"type": "cost", "cost": summary["cost"]})
    await seq({"type": "dag_done", "runId": run_id,
               "investigationId": spec.investigation_id,
               "status": summary["status"],
               "llmError": summary.get("llm_error"),
               "rootCause": summary.get("root_cause"),
               "masking": summary.get("masking", {}),
               "cost": summary["cost"],
               "evidenceCount": summary["evidence_count"],
               "citationCoverage": summary["citation_coverage"]})

    store.update_investigation(spec.investigation_id, {
        "status": "complete" if summary["status"] != "failed" else "failed",
        "completedAt": store.now(),
        "findings": summary["findings"],
        "rootCause": summary.get("root_cause"),
        "evidenceCount": summary["evidence_count"],
        "citationCoverage": str(summary["citation_coverage"]),
        "cost": {k: str(v) for k, v in summary["cost"].items()},
        "masking": summary.get("masking", {}),
        "evidenceRef": summary.get("evidence_ref", {}),
        "runbookId": summary.get("runbook_id", ""),
        "llmError": summary.get("llm_error") or "",
        "errorSignatures": summary.get("error_signatures", []),
        "symptomShape": summary.get("symptom_shape", []),
        "recommendedActions": summary.get("recommended_actions", []),
        "discriminatingChecks": summary.get("discriminating_checks", []),
        "caseCount": summary.get("case_count", 0),
        "serviceName": spec.service,
    })
    _notify(spec, summary)
    return summary


def _summarize(spec, run_id, outcome, extra) -> dict:
    prior = outcome.prior_results
    collector = prior.get("obs_signal_collector")
    corr = prior.get("obs_correlator")
    root = prior.get("obs_root_cause")
    runbook = prior.get("obs_runbook")
    retrieved = prior.get("obs_case_retrieval")
    hypo = prior.get("obs_hypothesis")

    llm_calls: list[dict] = []
    cost = 0.0
    in_tok = out_tok = 0
    for res in prior.values():
        for call in res.output.get("llm_calls") or []:
            llm_calls.append(call)
            cost += float(call.get("cost_usd", 0) or 0)
            in_tok += int(call.get("input_tokens", 0) or 0)
            out_tok += int(call.get("output_tokens", 0) or 0)

    masking = {}
    try:
        from src.observability.masking import get_session, masking_enabled
        if masking_enabled() and spec.masking_enabled:
            masking = get_session(spec.investigation_id).state()
        else:
            masking = {"enabled": False, "reversible": False, "totalTokens": 0, "byType": {}}
    except Exception:  # noqa: BLE001
        masking = {"enabled": False, "reversible": False, "totalTokens": 0, "byType": {}}

    root_out = root.output if root else {}
    findings = root_out.get("findings") or []
    failed = [r.agent for r in outcome.runs if r.status == "failed"]

    # An LLM failure is not an agent crash — it degrades the run to "no root cause".
    # Surface the provider's reason so the UI can explain itself.
    llm_errors = [res.output["llm_error"] for res in prior.values()
                  if res.output.get("llm_error")]

    return {
        "run_id": run_id,
        "investigation_id": spec.investigation_id,
        "status": "failed" if len(failed) == len(outcome.runs) and outcome.runs else "success",
        "title": spec.title,
        "service": spec.service,
        "window": spec.window.to_dict(),
        "stages_run": outcome.stages_run,
        "agents_failed": failed,
        "findings": findings,
        "root_cause": root_out.get("root_cause"),
        "contributing_factors": root_out.get("contributing_factors", []),
        "ruled_out": root_out.get("ruled_out", []),
        "recommended_actions": root_out.get("recommended_actions", []),
        "unsupported_claims": root_out.get("unsupported_claims", []),
        "citation_coverage": root_out.get("citation_coverage", 0.0),
        "llm_error": llm_errors[0] if llm_errors else None,
        "rejected_citations": root_out.get("rejected_citations", []),
        "timeline": (corr.output.get("timeline") if corr else []) or [],
        "anomalies": (corr.output.get("anomalies") if corr else []) or [],
        "suspect_changes": (corr.output.get("suspect_changes") if corr else []) or [],
        "blast_radius": (corr.output.get("blast_radius") if corr else {}) or {},
        "symptom_shape": (corr.output.get("symptom_shape") if corr else []) or [],
        "error_signatures": (corr.output.get("error_signatures") if corr else []) or [],
        "evidence_index": (collector.output.get("evidence_index") if collector else []) or [],
        "evidence_count": len(collector.output.get("evidence_index", [])) if collector else 0,
        "evidence_ref": (collector.output.get("evidence_ref") if collector else {}) or {},
        "providers_queried": (collector.output.get("providers_queried") if collector else []) or [],
        "runbook": runbook.output if runbook else {},
        "runbook_id": (runbook.output.get("runbook_id") if runbook else "") or "",
        "cases": (retrieved.output.get("cases") if retrieved else []) or [],
        "negative_cases": (retrieved.output.get("negative_cases") if retrieved else []) or [],
        "case_count": len((retrieved.output.get("cases") if retrieved else []) or []),
        "corpus_size": (retrieved.output.get("corpus_size") if retrieved else 0) or 0,
        "discriminating_checks": [h.get("discriminating_check", {})
                                  for h in ((hypo.output.get("hypotheses") if hypo else []) or [])],
        "hypotheses": (hypo.output.get("hypotheses") if hypo else []) or [],
        "masking": masking,
        "cost": {"inputTokens": in_tok, "outputTokens": out_tok,
                 "totalCost": round(cost, 6), "calls": len(llm_calls),
                 "model": llm_calls[-1].get("model_id", "") if llm_calls else ""},
        "llm_calls": llm_calls,
        "cost_usd": round(cost, 6),
        "agent_runs": {r.agent: r.to_dict() for r in outcome.runs},
    }


def _notify(spec, summary: dict) -> None:
    """Fire-and-forget: a Slack outage must never fail an investigation."""
    try:
        from src.config_settings import get_settings
        from src.services.notifications import dispatcher
        from src.services.notifications.base import Notification
        s = get_settings()
        root = summary.get("root_cause") or {}
        link = (f"{s.app_public_url}/observability?tab=investigate"
                f"&investigation={spec.investigation_id}")
        dispatcher.send_sync(Notification(
            kind="investigation_done",
            severity=spec.severity,
            title=f"RCA complete: {spec.service or spec.title}",
            body=root.get("statement", "No root cause could be established from the "
                                       "available evidence."),
            links=[{"label": "Open investigation", "url": link}],
            fields=[{"label": "Confidence", "value": f"{float(root.get('confidence', 0)):.0%}"},
                    {"label": "Evidence", "value": str(summary.get("evidence_count", 0))},
                    {"label": "Citation coverage",
                     "value": f"{summary.get('citation_coverage', 0):.0%}"},
                    {"label": "Cost", "value": f"${summary['cost']['totalCost']:.4f}"}],
            dedupe_key=f"investigation:{spec.investigation_id}",
        ))
    except Exception as exc:  # noqa: BLE001
        log.debug("investigation notification skipped: %s", exc)


async def run_investigation_headless(req, emit: Emitter | None = None) -> dict:
    """Adapter for orchestrator/headless.py::run_headless."""
    spec_raw = req.extra.get("investigation") or {}
    spec = (spec_raw if isinstance(spec_raw, InvestigationSpec)
            else build_spec(spec_raw, req.project_id or ""))
    return await run_investigation(
        spec, user_id=req.user_id, username=req.username, role=req.role,
        session_id=req.session_id, project_id=req.project_id, emit=emit,
        command=req.command, agents_override=req.agents_override,
        include_verify=bool(req.extra.get("include_verify")),
    )

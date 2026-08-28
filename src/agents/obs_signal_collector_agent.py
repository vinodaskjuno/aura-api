"""obs_signal_collector — fan out across every configured provider, normalize, mask.

Produces the evidence bundle everything downstream cites. No LLM.
"""
from __future__ import annotations

import asyncio
import logging

from src.agents.base_agent import AgentContext, AgentResult, BaseAgent
from src.config_settings import get_settings
from src.observability import correlation, store
from src.observability.masking import get_session, masking_enabled, persist_session
from src.observability.registry import resolve_providers
from src.observability.types import (
    EventQuery, EvidenceRecord, InvestigationSpec, LogQuery, MetricQuery, TraceQuery,
)

log = logging.getLogger(__name__)


class ObsSignalCollectorAgent(BaseAgent):
    name = "obs_signal_collector"
    description = ("Collect logs, metrics, traces and change events from every "
                   "configured observability provider and normalize them into evidence")

    async def run(self, context: AgentContext) -> AgentResult:
        result = self._result(context)
        spec_raw = context.extra.get("investigation")
        if not spec_raw:
            result.log("No investigation spec in context.extra — nothing to collect")
            return result.finish("failed")
        spec = (spec_raw if isinstance(spec_raw, InvestigationSpec)
                else InvestigationSpec.from_dict(spec_raw))

        s = get_settings()
        emit = context.extra.get("_emit")
        providers = resolve_providers(context.user_id, spec.project_id or context.project_id,
                                      spec.provider_ids or None)
        if not providers:
            result.log("No observability providers configured")
            result.output = {"investigation_id": spec.investigation_id,
                             "providers_queried": [], "counts": {},
                             "evidence_index": [], "top_error_signatures": [],
                             "deploy_candidates": []}
            return result.finish("partial")

        service = spec.service
        window = spec.window
        result.log(f"Querying {len(providers)} provider(s) for '{service}' "
                   f"over {window.duration_s()}s")

        tasks, plan = [], []
        for p in providers:
            if p.supports("logs"):
                plan.append((p, "logs"))
                tasks.append(p.query_logs(LogQuery(
                    service=service, window=window, filter=spec.filter,
                    limit=s.observability_max_log_records)))
            if p.supports("metrics"):
                plan.append((p, "metrics"))
                tasks.append(p.query_metrics(MetricQuery(service=service, window=window)))
            if p.supports("traces"):
                plan.append((p, "traces"))
                tasks.append(p.query_traces(TraceQuery(service=service, window=window)))
            if p.supports("events"):
                plan.append((p, "events"))
                tasks.append(p.recent_deploys(EventQuery(
                    service=service, window=window.widen(before_s=3600))))

        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        logs, series, traces, events = [], [], [], []
        queried: list[dict] = []
        for (provider, signal), outcome in zip(plan, gathered):
            entry = {"provider_id": provider.provider_id,
                     "provider_type": provider.provider_type,
                     "signal": signal, "status": "ok", "records": 0,
                     "query_ms": 0, "error": None}
            if isinstance(outcome, Exception):
                entry.update(status="failed", error=str(outcome)[:200])
            elif signal == "logs":
                entry["query_ms"] = outcome.query_ms
                if outcome.error:
                    entry.update(status="failed", error=outcome.error[:200])
                elif outcome.unsupported:
                    entry["status"] = "unsupported"
                else:
                    logs.extend(outcome.records)
                    entry["records"] = len(outcome.records)
            else:
                bucket = {"metrics": series, "traces": traces, "events": events}[signal]
                bucket.extend(outcome or [])
                entry["records"] = len(outcome or [])
            queried.append(entry)

        result.log(f"Collected {len(logs)} logs, {len(series)} series, "
                   f"{len(traces)} traces, {len(events)} events")

        signatures = correlation.error_signatures(logs, top=10)
        evidence = self._build_evidence(spec, logs, series, traces, events, signatures)

        # Cap AFTER weighting so the most relevant records survive truncation.
        evidence.sort(key=lambda e: e.weight, reverse=True)
        dropped = max(0, len(evidence) - s.observability_max_evidence_records)
        evidence = evidence[: s.observability_max_evidence_records]
        if dropped:
            result.log(f"Capped evidence at {len(evidence)} records ({dropped} dropped)")

        if masking_enabled() and spec.masking_enabled:
            ms = get_session(spec.investigation_id)
            for e in evidence:
                original = e.summary
                e.summary = ms.mask(e.summary)
                e.title = ms.mask(e.title)
                if e.summary != original:
                    e.masked_fields = ["summary"]
            persist_session(ms)
            result.log(f"Masked {ms.token_count} identifier(s) before any LLM egress")

        if emit:
            for e in evidence[:100]:
                await emit({"type": "evidence", "evidence": e.index_row()})

        ref = store.save_evidence_bundle(
            spec.investigation_id, spec.project_id or context.project_id or "", evidence)

        result.output = {
            "investigation_id": spec.investigation_id,
            "service": service,
            "window": window.to_dict(),
            "providers_queried": queried,
            "counts": {"logs": len(logs), "metrics": len(series),
                       "traces": len(traces), "events": len(events)},
            "evidence_ref": ref,
            "evidence_index": [e.index_row() for e in evidence],
            "evidence_added": len(evidence),
            "top_error_signatures": signatures,
            "deploy_candidates": [self._event_dict(e) for e in events],
            "error_onset_t": correlation.error_onset(logs),
            "trace_summary": correlation.trace_error_summary(traces),
            "masked": bool(masking_enabled() and spec.masking_enabled),
            "mask_session_id": spec.investigation_id,
            "_series": [self._series_dict(x) for x in series],
            "evidence_dropped": dropped,
        }
        # Shared by-reference scratch — see investigation_dag.run_investigation.
        scratch = context.extra.setdefault("_scratch", {})
        scratch["series"] = series
        scratch["events"] = events
        scratch["evidence"] = evidence
        return result.finish("success" if evidence else "partial")

    # ── Evidence construction ────────────────────────────────────────────────

    def _build_evidence(self, spec, logs, series, traces, events, signatures):
        ev: list[EvidenceRecord] = []
        sig_index = {}
        for s_ in signatures:
            for rid in s_.get("record_ids", []):
                sig_index[rid] = s_["count"]

        for r in logs:
            # Weight by error level and how common the signature is — this is what
            # decides which records survive the cap.
            weight = 1.0
            if r.level in ("ERROR", "FATAL"):
                weight += 1.5
            weight += min(1.0, sig_index.get(r.record_id, 0) / 20.0)
            ev.append(EvidenceRecord.make(
                spec.investigation_id, "logs", r.provider_id, r.provider_type,
                r.service, r.timestamp, f"{r.level} {r.service}",
                r.body[:280], natural_key=r.record_id,
                payload={"body": r.body, "labels": r.labels, "level": r.level,
                         "trace_id": r.trace_id, "truncated": r.truncated},
                labels=r.labels, source_url=r.source_url, weight=weight))

        for m in series:
            delta = m.stats.get("delta_pct", 0)
            ev.append(EvidenceRecord.make(
                spec.investigation_id, "metrics", m.provider_id, m.provider_type,
                m.service, m.points[-1].timestamp if m.points else spec.window.end,
                f"{m.metric} ({m.unit or 'n/a'})",
                f"{m.metric}: p95={m.stats.get('p95')} mean={m.stats.get('mean')} "
                f"delta={delta}%", natural_key=m.series_id,
                payload={"stats": m.stats, "labels": m.labels, "unit": m.unit,
                         "points": [{"timestamp": p.timestamp, "value": p.value}
                                    for p in m.points]},
                labels=m.labels, source_url=m.source_url,
                weight=1.0 + min(2.0, abs(delta) / 100.0)))

        for t in traces:
            ev.append(EvidenceRecord.make(
                spec.investigation_id, "traces", t.provider_id, t.provider_type,
                t.root_service, t.start_time,
                f"trace {t.root_operation or t.trace_id[:8]}",
                f"{t.duration_ms:.0f}ms, {t.span_count} spans, "
                f"{t.error_count} errors across {len(t.services_touched)} services",
                natural_key=t.trace_id,
                payload={"spans": [s.__dict__ for s in t.slowest_spans],
                         "services_touched": t.services_touched,
                         "duration_ms": t.duration_ms, "status": t.status},
                source_url=t.source_url,
                weight=1.0 + (1.5 if t.status == "error" else 0.0)))

        for e in events:
            ev.append(EvidenceRecord.make(
                spec.investigation_id, "events", e.provider_id, e.provider_type,
                e.service, e.timestamp, f"{e.kind}: {e.title}",
                (f"{e.title} {('version ' + e.version) if e.version else ''} "
                 f"{('by ' + e.actor) if e.actor else ''}").strip(),
                natural_key=e.event_id,
                payload={"kind": e.kind, "version": e.version, "actor": e.actor,
                         "description": e.description, "labels": e.labels},
                labels=e.labels, source_url=e.source_url,
                # Change events are the highest-yield signal in a deploy regression.
                weight=3.0 if e.kind in ("deploy", "config_change", "release") else 1.5))
        return ev

    @staticmethod
    def _event_dict(e) -> dict:
        return {"event_id": e.event_id, "kind": e.kind, "timestamp": e.timestamp,
                "service": e.service, "title": e.title, "version": e.version,
                "actor": e.actor, "source_url": e.source_url}

    @staticmethod
    def _series_dict(m) -> dict:
        return {"series_id": m.series_id, "metric": m.metric, "unit": m.unit,
                "stats": m.stats, "source_url": m.source_url}

"""End-to-end investigation: fixture providers in, evidence-cited root cause out.

Covers the claims the feature is sold on:
  * signals collected and correlated deterministically
  * the deploy 4 minutes before the knee is identified, the later one excluded
  * NO real identifier reaches the LLM prompt
  * a citation that cannot be resolved is rejected, including laundered case ids
  * unsupported claims stay visible instead of being quietly dropped
"""
from __future__ import annotations

import json

import pytest

from src.observability import store
from src.orchestrator.agent_registry import bootstrap
from src.orchestrator.investigation_dag import build_spec, run_investigation

FORBIDDEN = ["checkout-7d9f4b-abc01", "10.42.3.17", "ip-10-42-3-17.ec2.internal",
             "prod-use1-cluster", "prod-checkout", "grafana.internal.corp"]


def _responder(system: str, user: str):
    ids = [line.split('"evidence_id": "')[1].split('"')[0]
           for line in user.splitlines() if '"evidence_id": "' in line][:4]
    usage = {"input_tokens": 1200, "output_tokens": 300, "stop_reason": "end_turn"}
    # Discriminate on a phrase unique to the hypothesis prompt — the root-cause
    # system prompt also contains the word "competing".
    if "Produce 3-5" in system:
        return json.dumps({"hypotheses": [
            {"hypothesis_id": "h1",
             "statement": "Deploy v2.14.3 raised the JVM heap ceiling above the pod memory limit",
             "category": "deploy", "prior": 0.7,
             "supporting_evidence_ids": ids[:2], "contradicting_evidence_ids": [],
             "discriminating_check": {"description": "Compare -Xmx to the pod memory limit",
                                      "signal": "events", "query": "deploy v2.14.3",
                                      "expect": "Xmx exceeds limit"}},
            {"hypothesis_id": "h2",
             "statement": "Upstream payment-service latency exhausted the connection pool",
             "category": "dependency", "prior": 0.3,
             "supporting_evidence_ids": ids[2:3], "contradicting_evidence_ids": [],
             "discriminating_check": {"description": "Check payment-service p99",
                                      "signal": "metrics", "query": "payment p99",
                                      "expect": "flat"}}]}), usage
    return json.dumps({
        "root_cause": {
            "statement": f"checkout-service was OOM-killed after deploy v2.14.3 "
                         f"[[ev:{ids[0]}]] raised the JVM heap ceiling above the pod "
                         f"memory limit [[ev:{ids[1]}]].",
            "category": "deploy", "confidence": 0.91,
            "evidence_ids": ids[:2], "hypothesis_id": "h1"},
        "contributing_factors": [
            {"statement": "Connection pool exhaustion followed the restarts",
             "evidence_ids": ids[2:3]},
            {"statement": "Probably a DNS issue as well", "evidence_ids": []},
            {"statement": "We have seen this before", "evidence_ids": ["case_abc123"]}],
        "ruled_out": [{"statement": "Network partition", "reason": "no upstream errors",
                       "evidence_ids": ids[:1]}],
        "impact": {"statement": "Checkout unavailable", "user_facing": True,
                   "services_affected": ["checkout-service"], "evidence_ids": ids[:1]},
        "recommended_actions": [{"action": "Roll back to v2.14.2", "owner_hint": "platform",
                                 "risk": "low", "evidence_ids": ids[:1]}],
        "timeline_summary": ["10:16 deploy", "10:20 latency knee", "10:22 OOMKilled"],
    }), usage


@pytest.fixture
def investigation(fake_dynamo, fake_s3, fake_graph, fixture_provider, stub_llm,
                  captured_events, no_notifications):
    bootstrap()
    fixture_provider()
    prompts = stub_llm(_responder)
    spec = build_spec({"services": ["checkout-service"],
                       "symptom": "checkout 5xx and latency spike",
                       "start": "2026-08-01T10:00:00Z", "end": "2026-08-01T11:00:00Z",
                       "severity": "critical", "provider_ids": ["fx-loki"]})
    store.create_investigation({
        "investigationId": spec.investigation_id, "createdAt": store.now(),
        "projectId": "", "serviceName": "checkout-service", "title": spec.title,
        "status": "queued", "severity": "critical", "services": spec.services,
        "window": spec.window.to_dict(), "userId": "u1"})
    return spec, prompts, captured_events


async def _run(spec, emit):
    return await run_investigation(spec, user_id="u1", username="tester", role="admin",
                                   session_id="sess1234", emit=emit)


@pytest.mark.scenario
async def test_investigation_produces_cited_root_cause(investigation):
    spec, prompts, emit = investigation
    s = await _run(spec, emit)

    assert s["status"] == "success"
    assert s["evidence_count"] > 0
    root = s["root_cause"]
    assert root is not None
    assert root["category"] == "deploy"
    assert root["evidence_ids"], "a root cause must cite evidence"
    assert "AURA_" not in root["statement"], "output must be unmasked before display"


@pytest.mark.scenario
async def test_signals_collapse_and_correlate(investigation):
    spec, _, emit = investigation
    s = await _run(spec, emit)

    # 46 raw log lines collapse to a handful of signatures.
    assert 0 < len(s["error_signatures"]) <= 5
    assert any(a["delta_pct"] > 100 for a in s["anomalies"]), s["anomalies"]
    # The deploy 4 minutes before the knee wins; the earlier one ranks below it.
    assert s["suspect_changes"], "the deploy must be identified"
    assert s["suspect_changes"][0]["version"] == "v2.14.3"
    assert "deploy_nearby" in s["symptom_shape"]


@pytest.mark.scenario
async def test_no_real_identifier_reaches_the_llm(investigation):
    """The masking guarantee. If this fails, the feature is not shippable."""
    spec, prompts, emit = investigation
    await _run(spec, emit)

    assert prompts, "the LLM should have been called"
    for secret in FORBIDDEN:
        for prompt in prompts:
            assert secret not in prompt, f"LEAKED {secret!r} into an LLM prompt"
    # Service names are deliberately NOT masked — they are the join key to the graph.
    assert any("checkout-service" in p for p in prompts)


@pytest.mark.scenario
async def test_unresolvable_citations_are_rejected(investigation):
    spec, _, emit = investigation
    s = await _run(spec, emit)

    assert "case_abc123" in s["rejected_citations"], \
        "a case id must never be usable as a citation"
    statuses = {f["status"] for f in s["findings"]}
    assert "unsupported" in statuses, "unsupported claims must stay visible"
    unsupported = [f for f in s["findings"] if f["status"] == "unsupported"]
    assert all(not f["evidenceIds"] for f in unsupported)
    assert 0.0 < s["citation_coverage"] <= 1.0


@pytest.mark.scenario
async def test_event_stream_is_replayable(investigation):
    spec, _, emit = investigation
    await _run(spec, emit)
    events = emit.events

    kinds = {e["type"] for e in events}
    assert {"dag_start", "stage_start", "agent_start", "agent_done",
            "stage_done", "dag_done", "evidence", "finding", "cost"} <= kinds, kinds
    assert all("seq" in e for e in events), "every event needs seq for reconnect replay"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(e.get("investigationId") == spec.investigation_id for e in events)


@pytest.mark.scenario
async def test_runbook_matched_and_steps_instantiated(investigation):
    spec, _, emit = investigation
    s = await _run(spec, emit)
    rb = s["runbook"]
    assert rb.get("steps"), "a runbook must always be produced (template fallback)"
    assert rb["steps_satisfied"] >= 1, "collected evidence should satisfy some steps"


# ── LLM failure is explained, not silent ─────────────────────────────────────

@pytest.mark.scenario
async def test_llm_failure_degrades_with_a_reason(fake_dynamo, fake_s3, fake_graph,
                                                  fixture_provider, stub_llm,
                                                  captured_events, no_notifications):
    """A billing/auth/model failure must reach the UI as text an operator can act on.

    Before this, `raise_for_status()` discarded the provider's body, so the log said
    only "Client error '400 Bad Request'" and the UI showed an empty findings list —
    indistinguishable from a broken feature.
    """
    from src.observability.llm import set_llm_override
    bootstrap()
    fixture_provider()

    def boom(system, user, model, max_tokens):
        raise RuntimeError("invalid_request_error: Your credit balance is too low "
                           "to access the Anthropic API.")
    set_llm_override(boom)

    spec = build_spec({"services": ["checkout-service"],
                       "start": "2026-08-01T10:00:00Z", "end": "2026-08-01T11:00:00Z",
                       "provider_ids": ["fx-loki"]})
    store.create_investigation({
        "investigationId": spec.investigation_id, "createdAt": store.now(),
        "projectId": "", "serviceName": "checkout-service", "title": spec.title,
        "status": "queued", "severity": "high", "services": spec.services,
        "window": spec.window.to_dict(), "userId": "u1"})

    s = await _run(spec, captured_events)

    assert s["root_cause"] is None
    assert s["llm_error"] and "credit balance" in s["llm_error"], s["llm_error"]

    done = [e for e in captured_events.events if e["type"] == "dag_done"]
    assert done and "credit balance" in (done[0].get("llmError") or ""), \
        "dag_done must carry the reason so the UI can explain the empty state"

    # The deterministic half of the pipeline still did its job.
    assert s["evidence_count"] > 0
    assert s["suspect_changes"], "correlation does not depend on the LLM"

"""Aura's own agents, traced into Opik.

Two properties are load-bearing and everything else here is detail:

  1. A span must NEVER carry unmasked text. Masking (observability/masking.py) is
     fail-closed and is the reason Aura can send prompts to a model at all; a span
     emitted from the wrong side of it would route around the guarantee entirely.
  2. Tracing must NEVER break the traced call. If Opik is down, slow, or
     misconfigured, the investigation still completes.
"""
from __future__ import annotations

import asyncio

import pytest

from src.aiobs import opik_client
from src.observability.llm import invoke_masked, invoke_masked_sync


@pytest.fixture
def captured_spans(monkeypatch):
    """Capture emit_llm_span calls instead of performing them, and force Opik on."""
    spans: list[dict] = []
    monkeypatch.setattr(opik_client, "enabled", lambda: True)

    def emit(**kwargs):
        spans.append(kwargs)
        return {"traceId": "01a05d64-0000-7000-8000-000000000001",
                "spanId": "01a05d64-0000-7000-8000-000000000002"}

    monkeypatch.setattr(opik_client, "emit_llm_span", emit)
    return spans


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── The masking guarantee ─────────────────────────────────────────────────────

def test_span_payload_is_the_masked_text_not_the_unmasked_reply(
        captured_spans, stub_llm, monkeypatch):
    """The span is emitted from inside invoke_masked, AFTER masking and using a
    snapshot taken BEFORE unmasking.

    This is the single reason span emission lives in the seam rather than in the
    caller: by the time invoke_masked returns, `text` holds real identifiers again.
    """
    monkeypatch.setattr("src.observability.masking.masking_enabled", lambda: True)
    stub_llm(lambda system, user: ("the pod AURA_POD_01 is unhealthy",
                                   {"input_tokens": 10, "output_tokens": 4}))

    text, record = _run(invoke_masked(
        system="sys", user="investigate pod checkout-7c9f in prod",
        investigation_id="inv-1", agent="obs_root_cause"))

    assert captured_spans, "no span was emitted"
    payload = captured_spans[0]["masked_output"]
    # The span keeps the placeholder; the CALLER gets the unmasked text back.
    assert "AURA_POD_01" in payload
    assert record.masked is True


def test_span_carries_metadata_needed_to_debug_a_judge(captured_spans, stub_llm):
    stub_llm(lambda s, u: ("ok", {"input_tokens": 7, "output_tokens": 3,
                                  "stop_reason": "end_turn"}))
    _run(invoke_masked(system="sys", user="hello", investigation_id="",
                       agent="obs_hypothesis", stage=3))

    meta = captured_spans[0]["metadata"]
    assert meta["stage"] == 3
    # A prompt hash lets two identical prompts be correlated without either being
    # readable.
    assert meta["promptHash"]
    assert meta["stopReason"] == "end_turn"
    assert captured_spans[0]["input_tokens"] == 7


def test_cost_on_the_span_is_auras_own_figure(captured_spans, stub_llm):
    """Opik computes cost from its own bundled price table. Aura's
    calculate_cost_v2 is the number the invoice uses, so it is sent explicitly —
    otherwise the UI and the bill can disagree."""
    stub_llm(lambda s, u: ("ok", {"input_tokens": 1000, "output_tokens": 1000}))
    _, record = _run(invoke_masked(system="s", user="u", investigation_id="",
                                   agent="obs_x"))
    assert captured_spans[0]["cost_usd"] == record.cost_usd


# ── Never break the traced call ───────────────────────────────────────────────

def test_a_failing_span_emission_does_not_fail_the_llm_call(stub_llm, monkeypatch):
    """The whole point of the seam. An observability tool that can break the thing
    it observes is worse than none."""
    monkeypatch.setattr(opik_client, "enabled", lambda: True)

    def boom(**kwargs):
        raise RuntimeError("opik is on fire")

    monkeypatch.setattr(opik_client, "emit_llm_span", boom)
    stub_llm(lambda s, u: ("still fine", {"input_tokens": 1, "output_tokens": 1}))

    text, record = _run(invoke_masked(system="s", user="u", investigation_id="",
                                      agent="obs_x"))
    assert text == "still fine"
    assert record.error is None
    assert record.opik_trace_id == ""       # tracing simply did not happen


def test_a_failed_llm_call_is_still_traced(captured_spans, monkeypatch):
    """A provider outage is exactly what someone opens the traces view looking for,
    so the error path emits a span too."""
    from src.observability.llm import set_llm_override

    def explode(system, user, model, max_tokens):
        raise RuntimeError("throttled")

    set_llm_override(explode)
    try:
        text, record = _run(invoke_masked(system="s", user="u",
                                          investigation_id="", agent="obs_x"))
    finally:
        set_llm_override(None)

    assert text == "" and "throttled" in (record.error or "")
    assert captured_spans and "throttled" in captured_spans[0]["error"]


def test_opik_disabled_emits_nothing(stub_llm, monkeypatch):
    monkeypatch.setattr(opik_client, "enabled", lambda: False)
    calls: list = []
    monkeypatch.setattr(opik_client, "emit_llm_span",
                        lambda **kw: calls.append(kw))
    stub_llm(lambda s, u: ("ok", {"input_tokens": 1, "output_tokens": 1}))
    _run(invoke_masked(system="s", user="u", investigation_id="", agent="obs_x"))
    assert calls == []


# ── Project routing and the sync bridge ───────────────────────────────────────

@pytest.mark.parametrize("agent,expected", [
    ("obs_root_cause", "aura-observability"),
    ("judge_relevance", "aura-observability"),
    ("qa_planner", "aura-qualitymind"),
    ("intent_classifier", "aura-agents"),
])
def test_agents_are_routed_to_a_workspace_project(agent, expected, captured_spans,
                                                  stub_llm):
    """One project per Aura workspace, not per agent: a project is the unit the
    traces view filters by, and someone debugging a run wants every agent in it
    together."""
    stub_llm(lambda s, u: ("ok", {"input_tokens": 1, "output_tokens": 1}))
    _run(invoke_masked(system="s", user="u", investigation_id="", agent=agent))
    assert captured_spans[0]["project"] == expected


def test_explicit_project_overrides_the_default(captured_spans, stub_llm):
    stub_llm(lambda s, u: ("ok", {"input_tokens": 1, "output_tokens": 1}))
    _run(invoke_masked(system="s", user="u", investigation_id="", agent="obs_x",
                       opik_project="aura-evaluations"))
    assert captured_spans[0]["project"] == "aura-evaluations"


def test_trace_context_is_threaded_so_a_dag_is_one_trace(captured_spans, stub_llm):
    """Without this a 5-agent orchestrator run renders as 5 unrelated traces and
    tracing a DAG achieves nothing."""
    stub_llm(lambda s, u: ("ok", {"input_tokens": 1, "output_tokens": 1}))
    _run(invoke_masked(system="s", user="u", investigation_id="", agent="obs_x",
                       opik_trace_id="01a05d64-0000-7000-8000-00000000aaaa",
                       opik_parent_span_id="01a05d64-0000-7000-8000-00000000bbbb",
                       opik_thread_id="sess-9"))
    span = captured_spans[0]
    assert span["trace_id"] == "01a05d64-0000-7000-8000-00000000aaaa"
    assert span["parent_span_id"] == "01a05d64-0000-7000-8000-00000000bbbb"
    assert span["thread_id"] == "sess-9"


def test_record_reports_the_ids_back_to_the_caller(captured_spans, stub_llm):
    """The orchestrator reads these off the AgentResult to thread the next agent
    into the same trace."""
    stub_llm(lambda s, u: ("ok", {"input_tokens": 1, "output_tokens": 1}))
    _, record = _run(invoke_masked(system="s", user="u", investigation_id="",
                                   agent="obs_x"))
    assert record.opik_trace_id == "01a05d64-0000-7000-8000-000000000001"
    assert record.opik_span_id == "01a05d64-0000-7000-8000-000000000002"


def test_sync_bridge_works_with_no_running_loop(stub_llm):
    """intent_classifier and the judges are sync callers; making them async would
    ripple through their callers for no benefit."""
    stub_llm(lambda s, u: ("sync ok", {"input_tokens": 1, "output_tokens": 1}))
    text, record = invoke_masked_sync(system="s", user="u", investigation_id="",
                                      agent="intent_classifier")
    assert text == "sync ok" and record.error is None


# ── The redaction belt in opik_client ─────────────────────────────────────────

@pytest.mark.parametrize("raw,must_not_contain", [
    ("key AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
    ("token gw-abcdefghijklmnopqrstuvwxyz", "gw-abcdefghijklmnopqrstuvwxyz"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
])
def test_redact_is_a_second_belt_not_the_masking_implementation(raw, must_not_contain):
    """observability/masking.py is the real guarantee. This only catches the case
    where a caller forgot, and it truncates rather than raising because never
    breaking the traced call outranks completeness."""
    assert must_not_contain not in opik_client._redact(raw)


def test_redact_caps_runaway_payloads():
    assert len(opik_client._redact("x" * 50_000)) <= 8_000

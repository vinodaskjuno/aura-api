"""A QA run as a trace, and the advisor's own turns as traces.

The AI Traces page was blind to both of the surfaces people actually use. Nothing new
is measured here — the run's event stream and the advisor's token counts already
existed — so these tests guard the two properties that make the rendering honest:
the stream must not be able to fail because of tracing, and what is emitted must
actually parse back through Aura's own ingest.
"""
from __future__ import annotations

from src.aiobs import ingest, service
from src.qatest import tracing


def _tracer():
    return tracing.RunTracer("proj-1", "run-9",
                             endpoint="http://aura/otlp/v1/traces", api_key="gw-x")


def _events():
    return [
        {"type": "plan", "message": "reading the knowledge graph"},
        {"type": "planned", "cases": 2},
        {"type": "step", "name": "root loads", "status": "passed"},
        {"type": "step", "name": "orders api", "status": "failed", "message": "500"},
        {"type": "evidence", "message": "stored"},
        {"type": "done", "status": "failed", "passed": 1, "failed": 1,
         "reason": "a case failed"},
    ]


def test_a_run_parses_back_through_auras_own_ingest():
    """Emitting OTLP that Aura cannot read would be the quietest possible failure:
    the receiver answers 200 to everything."""
    tracer = _tracer()
    for event in _events():
        tracer.observe(event)

    payload = tracer.payload()
    spans = [span for span, _ in ingest.parse_spans(payload)]

    assert len(spans) == 5
    assert service.project_of(payload) == "proj-1"
    # The run id is the thread, so a run is one conversation on the Threads tab.
    assert ingest.thread_id_from(spans, payload) == "run-9"


def test_the_root_is_the_run_and_a_failure_propagates():
    tracer = _tracer()
    for event in _events():
        tracer.observe(event)

    spans = [span for span, _ in ingest.parse_spans(tracer.payload())]
    traces = ingest.assemble(spans, "proj-1", "u1", "run-9")

    assert len(traces) == 1
    trace, children = traces[0]
    assert trace.name == "qa run: proj-1"
    assert trace.status == "error"
    assert trace.span_count == 5
    # Infra spans carry no model and no tokens, so they cost nothing and cannot be
    # mistaken for LLM traffic on a page whose whole subject is LLM traffic.
    assert trace.cost_usd == 0.0
    assert trace.total_tokens == 0
    assert {c.name for c in children} >= {"case: orders api", "plan"}


def test_per_line_events_do_not_become_spans():
    """`evidence` fires per line and would bury the timeline in one-line spans."""
    tracer = _tracer()
    for event in _events():
        tracer.observe(event)
    names = [s["name"] for s in tracer.payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]]
    assert "evidence" not in names


def test_an_unconfigured_tracer_is_free():
    tracer = tracing.RunTracer("proj-1", "run-9", endpoint="", api_key="")
    assert not tracer.enabled
    for event in _events():
        tracer.observe(event)
    assert tracer.flush() is False
    assert tracer._spans == []


def test_tracing_cannot_fail_a_run(monkeypatch):
    """The live WebSocket stream is the product. A telemetry failure must not reach it."""
    tracer = _tracer()

    def boom(self, event):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(tracing.RunTracer, "_observe", boom)
    tracer.observe({"type": "plan"})        # must not raise


def test_the_span_budget_is_bounded(monkeypatch):
    """A run with hundreds of cases must not grow the payload without limit, and must
    say how much it dropped rather than quietly truncating."""
    monkeypatch.setattr(tracing, "MAX_SPANS", 3)
    tracer = _tracer()
    for i in range(10):
        tracer.observe({"type": "step", "name": f"case {i}", "status": "passed"})
    tracer.observe({"type": "done", "status": "passed", "passed": 10, "failed": 0})

    spans = tracer.payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 4                  # 3 capped children plus the root
    root = spans[-1]
    dropped = [a for a in root["attributes"] if a["key"] == "qa.spans_dropped"]
    assert dropped and int(dropped[0]["value"]["intValue"]) == 7


# ── The advisor ─────────────────────────────────────────────────────────────

def test_advisor_spans_are_silent_when_opik_is_off(monkeypatch):
    from src.services.advisor import react_orchestrator as ro
    from src.aiobs import opik_client

    monkeypatch.setattr(opik_client, "enabled", lambda: False)
    assert ro._emit_turn_span(
        trace_id="", agent="dev-mate", model="m", provider="anthropic",
        prompt="hi", completion="there", input_tokens=1, output_tokens=1,
        cost_usd=0.0, latency_ms=5, thread_id="s1") == {}


def test_a_tool_span_needs_a_trace_to_hang_from(monkeypatch):
    """A bare tool call with no LLM turn above it is not something DevMate produces,
    and inventing a parent would scatter orphan single-span traces across the list."""
    from src.aiobs import opik_client

    monkeypatch.setattr(opik_client, "enabled", lambda: True)
    calls: list = []
    monkeypatch.setattr(opik_client, "request",
                        lambda *a, **k: calls.append(k) or {})

    assert opik_client.emit_span(project="p", trace_id="", name="read_file",
                                 kind="tool") == ""
    assert calls == []


def test_a_tool_span_is_not_an_llm_span(monkeypatch):
    """Opik's `type` drives how the waterfall groups a span, and a tool call has no
    model, no usage and no cost to report."""
    from src.aiobs import opik_client

    monkeypatch.setattr(opik_client, "enabled", lambda: True)
    sent: list = []
    monkeypatch.setattr(opik_client, "request",
                        lambda method, path, json_body=None, **k:
                        sent.append(json_body) or {})

    opik_client.emit_span(project="p", trace_id="t1", name="read_file", kind="tool",
                          masked_input="{}", masked_output="ok", parent_span_id="s1")

    assert sent[0]["type"] == "tool"
    assert sent[0]["parent_span_id"] == "s1"
    assert "usage" not in sent[0] and "model" not in sent[0]

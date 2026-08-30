"""OTLP trace ingestion for LLM-application observability.

The wire format is whatever a client's OpenTelemetry SDK emits, so these tests
assert against real OTLP shapes rather than a convenient internal one. Attribute
spellings differ between instrumentors (OpenAI's, OpenInference, Traceloop), and
guessing across them wrongly means a span's cost silently reads zero.
"""
from __future__ import annotations

import pytest

from src.aiobs import ingest, service
from src.aiobs.types import KIND_LLM, KIND_RETRIEVER, KIND_TOOL, Span


def kv(**pairs):
    """OTLP KeyValue list."""
    out = []
    for k, v in pairs.items():
        key = k.replace("__", ".")
        if isinstance(v, bool):
            out.append({"key": key, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": key, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            out.append({"key": key, "value": {"doubleValue": v}})
        else:
            out.append({"key": key, "value": {"stringValue": str(v)}})
    return out


def payload(spans, resource=None):
    return {"resourceSpans": [{
        "resource": {"attributes": kv(**(resource or {"service__name": "demo-app"}))},
        "scopeSpans": [{"spans": spans}],
    }]}


# Realistic UnixNano values: 2023-11-14, and a span lasting exactly 500ms.
_T0 = 1_700_000_000_000_000_000
_HALF_SECOND_NS = 500_000_000


def span(name="chat", span_id="s1", parent="", trace_id="t1", attrs=None,
         start=_T0, end=_T0 + _HALF_SECOND_NS, status=None):
    raw = {
        "traceId": trace_id, "spanId": span_id, "name": name,
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
        "attributes": kv(**(attrs or {})),
    }
    if parent:
        raw["parentSpanId"] = parent
    if status:
        raw["status"] = status
    return raw


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parses_a_minimal_span():
    parsed = ingest.parse_spans(payload([span()]))
    assert len(parsed) == 1
    s, resource = parsed[0]
    assert s.trace_id == "t1" and s.span_id == "s1"
    assert s.latency_ms == 500          # 500ms, from the UnixNano delta
    assert resource["service.name"] == "demo-app"


def test_a_span_without_ids_is_dropped_not_stored_wrong():
    assert ingest.parse_spans(payload([{"name": "x"}])) == []


@pytest.mark.parametrize("attrs,expected_in,expected_out", [
    ({"gen_ai__usage__input_tokens": 100, "gen_ai__usage__output_tokens": 20}, 100, 20),
    ({"gen_ai__usage__prompt_tokens": 50, "gen_ai__usage__completion_tokens": 5}, 50, 5),
    ({"llm__token_count__prompt": 7, "llm__token_count__completion": 3}, 7, 3),
])
def test_token_counts_across_instrumentor_spellings(attrs, expected_in, expected_out):
    """OpenAI's, OpenInference's and Traceloop's instrumentors disagree on names.
    Reading only one spelling makes every other client's cost read zero."""
    s, _ = ingest.parse_spans(payload([span(attrs=attrs)]))[0]
    assert s.input_tokens == expected_in
    assert s.output_tokens == expected_out
    assert s.total_tokens == expected_in + expected_out


def test_cost_is_computed_from_the_shared_pricing_table():
    """Span cost and the user's bill must not be able to disagree, so both come
    from MODEL_PRICING via calculate_cost_v2."""
    s, _ = ingest.parse_spans(payload([span(attrs={
        "gen_ai__request__model": "claude-sonnet-4-5",
        "gen_ai__usage__input_tokens": 1_000_000,
        "gen_ai__usage__output_tokens": 1_000_000,
    })])) [0]
    from src.config_settings import calculate_cost_v2
    assert s.cost_usd == pytest.approx(
        calculate_cost_v2("claude-sonnet-4-5", 1_000_000, 1_000_000), rel=1e-6)
    assert s.cost_usd > 0


def test_an_unknown_model_still_yields_a_span():
    s, _ = ingest.parse_spans(payload([span(attrs={
        "gen_ai__request__model": "some-model-we-have-never-heard-of",
        "gen_ai__usage__input_tokens": 10, "gen_ai__usage__output_tokens": 1})]))[0]
    assert s.model == "some-model-we-have-never-heard-of"
    assert s.input_tokens == 10          # the span survives even if pricing does not


def test_error_status_is_captured():
    s, _ = ingest.parse_spans(payload([span(
        status={"code": 2, "message": "upstream refused"})]))[0]
    assert s.status == "error"
    assert "upstream refused" in s.error


@pytest.mark.parametrize("name,attrs,kind", [
    ("chat gpt-4", {}, KIND_LLM),
    ("openai.completion", {}, KIND_LLM),
    ("retrieve_documents_rag", {}, KIND_RETRIEVER),
    ("contact_insight_tool", {}, KIND_TOOL),
    ("anything", {"gen_ai__request__model": "claude-opus-5"}, KIND_LLM),
])
def test_span_classification(name, attrs, kind):
    """LLM spans carry the cost, so misclassifying one loses the money."""
    s, _ = ingest.parse_spans(payload([span(name=name, attrs=attrs)]))[0]
    assert s.kind == kind


# ── Trace assembly ───────────────────────────────────────────────────────────

def test_nested_spans_assemble_into_one_trace_with_aggregates():
    spans = [s for s, _ in ingest.parse_spans(payload([
        span(name="handle_query", span_id="root"),
        span(name="classify", span_id="c1", parent="root",
             attrs={"gen_ai__request__model": "claude-sonnet-4-5",
                    "gen_ai__usage__input_tokens": 100,
                    "gen_ai__usage__output_tokens": 10}),
        span(name="tool", span_id="c2", parent="root",
             attrs={"gen_ai__request__model": "claude-sonnet-4-5",
                    "gen_ai__usage__input_tokens": 200,
                    "gen_ai__usage__output_tokens": 20}),
    ]))]
    (trace, group), = ingest.assemble(spans, "proj", "tenant-1")
    assert trace.name == "handle_query"          # the root names the trace
    assert trace.span_count == 3
    assert trace.total_tokens == 330             # summed across children
    assert trace.cost_usd > 0
    assert trace.status == "ok"
    assert len(group) == 3


def test_a_single_failing_span_marks_the_whole_trace_failed():
    spans = [s for s, _ in ingest.parse_spans(payload([
        span(span_id="root"),
        span(span_id="c1", parent="root", status={"code": 2, "message": "boom"}),
    ]))]
    (trace, _), = ingest.assemble(spans, "p", "t")
    assert trace.status == "error"


def test_an_orphaned_span_still_produces_a_trace():
    """Exports arrive split, so a child can land before its parent. Requiring a
    real root would drop that batch entirely."""
    spans = [s for s, _ in ingest.parse_spans(payload([
        span(span_id="child", parent="a-parent-in-another-batch")]))]
    (trace, group), = ingest.assemble(spans, "p", "t")
    assert trace.span_count == 1 and len(group) == 1


def test_separate_trace_ids_stay_separate():
    spans = [s for s, _ in ingest.parse_spans(payload([
        span(trace_id="t1", span_id="a"), span(trace_id="t2", span_id="b")]))]
    assert len(ingest.assemble(spans, "p", "t")) == 2


@pytest.mark.parametrize("key", [
    "session__id", "gen_ai__conversation__id", "thread__id"])
def test_thread_id_is_read_from_any_known_spelling(key):
    body = payload([span(attrs={key: "conv-42"})])
    spans = [s for s, _ in ingest.parse_spans(body)]
    assert ingest.thread_id_from(spans, body) == "conv-42"


def test_no_thread_id_is_not_an_error():
    body = payload([span()])
    assert ingest.thread_id_from([s for s, _ in ingest.parse_spans(body)], body) == ""


# ── Project attribution ──────────────────────────────────────────────────────

def test_project_comes_from_standard_otel_service_name():
    """Every OTel SDK sets service.name, so clients get grouping for free."""
    assert service.project_of(payload([span()], {"service__name": "crm-agent"})) == "crm-agent"


def test_an_explicit_aura_project_attribute_wins():
    body = payload([span()], {"service__name": "svc", "aura__project": "billing"})
    assert service.project_of(body) == "billing"


def test_project_falls_back_rather_than_failing():
    assert service.project_of({"resourceSpans": []}) == service.DEFAULT_PROJECT


# ── Storage flow ─────────────────────────────────────────────────────────────

class RecordingStore:
    def __init__(self):
        self.traces, self.spans = [], []

    def write_trace(self, trace, spans):
        self.traces.append(trace); self.spans.extend(spans)

    def get_trace(self, project_id, trace_id): return None
    def get_spans(self, trace_id, limit=1000): return []
    def list_traces(self, project_id, limit=50, status="", thread_id=""): return []
    def list_threads(self, project_id, limit=50): return []
    def capabilities(self): return {}


@pytest.fixture
def store(monkeypatch):
    s = RecordingStore()
    service.set_store(s)
    # Offload is exercised separately; keep payloads inline here.
    monkeypatch.setattr("src.aiobs.service.store_payload", lambda *a: (a[-1][:512], ""))
    yield s
    service.set_store(None)


def test_store_batch_persists_and_reports_a_count(store):
    body = payload([span(span_id="root"), span(span_id="c1", parent="root")])
    spans = [s for s, _ in ingest.parse_spans(body)]
    assert service.store_batch(spans, body, "tenant-9") == 2
    assert len(store.traces) == 1
    assert store.traces[0].tenant_id == "tenant-9"
    assert store.traces[0].project_id == "demo-app"


def test_one_unstorable_trace_does_not_lose_the_batch(store, monkeypatch):
    calls = {"n": 0}
    original = store.write_trace

    def flaky(trace, spans):
        calls["n"] += 1
        if trace.trace_id == "bad":
            raise RuntimeError("item too large")
        original(trace, spans)

    monkeypatch.setattr(store, "write_trace", flaky)
    body = payload([span(trace_id="bad", span_id="a"), span(trace_id="good", span_id="b")])
    spans = [s for s, _ in ingest.parse_spans(body)]
    stored = service.store_batch(spans, body, "t")
    assert calls["n"] == 2 and stored == 1
    assert [t.trace_id for t in store.traces] == ["good"]


def test_large_payloads_are_offloaded_rather_than_dropped(monkeypatch):
    """A prompt can exceed DynamoDB's 400 KB item limit; inlining one loses the span.

    Patches put_object on the real module rather than swapping sys.modules: the
    latter depends on whether another test imported it first, which made this pass
    alone and fail in the full suite.
    """
    uploaded = {}
    from src.aiobs import dynamo_store
    from src.storage import s3_client

    monkeypatch.setattr(s3_client, "put_object",
                        lambda bucket, key, body: uploaded.__setitem__(key, body))
    preview, ref = dynamo_store.store_payload("p", "t", "s", "in", "x" * 20_000)
    assert len(preview) == 512
    assert ref.endswith(".in.txt") and ref in uploaded


def test_a_failed_offload_keeps_the_span(monkeypatch):
    """Losing a payload is bad; losing the span it belonged to is worse."""
    from src.aiobs import dynamo_store
    from src.storage import s3_client

    def boom(bucket, key, body):
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(s3_client, "put_object", boom)
    preview, ref = dynamo_store.store_payload("p", "t", "s", "in", "y" * 20_000)
    assert len(preview) == 512 and ref == ""


def test_small_payloads_stay_inline():
    from src.aiobs import dynamo_store
    preview, ref = dynamo_store.store_payload("p", "t", "s", "in", "short prompt")
    assert preview == "short prompt" and ref == ""


# ── Wire format ──────────────────────────────────────────────────────────────
#
# The receiver was JSON-only. The Python OTLP HTTP exporter has NO JSON mode, so
# without protobuf decoding no Python LLM application — which is most of them —
# could send a trace at all. These tests exist because that was found by running a
# real exporter, not by reading the code.

def _pb_export(start_ns=1_700_000_000_000_000_000, end_ns=1_700_000_000_500_000_000):
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    kvp = rs.resource.attributes.add()
    kvp.key = "service.name"
    kvp.value.string_value = "pb-app"
    sp = rs.scope_spans.add().spans.add()
    sp.name = "chat_completion_create"
    sp.trace_id = bytes.fromhex("4452b8500f5fb612cb081a9725598ee4")
    sp.span_id = bytes.fromhex("0102030405060708")
    sp.start_time_unix_nano = start_ns
    sp.end_time_unix_nano = end_ns
    a = sp.attributes.add(); a.key = "gen_ai.request.model"; a.value.string_value = "claude-sonnet-4-5"
    b = sp.attributes.add(); b.key = "gen_ai.usage.input_tokens"; b.value.int_value = 1200
    c = sp.attributes.add(); c.key = "gen_ai.usage.output_tokens"; c.value.int_value = 64
    return req.SerializeToString()


def test_protobuf_decodes_to_the_same_shape_as_json():
    from src.routers.otlp import _decode_protobuf
    payload, err = _decode_protobuf(_pb_export())
    assert err == ""
    parsed = ingest.parse_spans(payload)
    assert len(parsed) == 1
    s, _ = parsed[0]
    assert s.name == "chat_completion_create"
    assert s.input_tokens == 1200 and s.output_tokens == 64
    assert s.kind == KIND_LLM and s.cost_usd > 0
    assert s.latency_ms == 500


def test_protobuf_ids_are_hex_not_base64():
    """MessageToDict base64-encodes byte fields. Every OTel UI and the JSON
    exporter show hex, so an id that renders differently per wire format is a bug."""
    from src.routers.otlp import _decode_protobuf
    payload, _ = _decode_protobuf(_pb_export())
    s, _ = ingest.parse_spans(payload)[0]
    assert s.trace_id == "4452b8500f5fb612cb081a9725598ee4"
    assert s.span_id == "0102030405060708"
    int(s.trace_id, 16)          # parses as hex


def test_protobuf_resource_attributes_survive():
    from src.routers.otlp import _decode_protobuf
    payload, _ = _decode_protobuf(_pb_export())
    assert service.project_of(payload) == "pb-app"


def test_malformed_protobuf_is_reported_not_raised():
    """The receiver must never 500: a non-2xx makes exporters retry in a loop and
    surfaces telemetry errors inside the caller's own application."""
    from src.routers.otlp import _decode_protobuf
    payload, err = _decode_protobuf(b"this is not protobuf at all")
    assert payload is None and "Malformed protobuf" in err

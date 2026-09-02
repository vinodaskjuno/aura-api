"""OpikTraceStore: the ClickHouse-backed implementation of the TraceStore protocol.

These tests assert against Opik's REAL wire shapes, captured from a running 2.2.46
instance, not against a convenient internal shape. That matters because the JSON is
snake_case while `Trace.java` is camelCase, `usage` is a map rather than a scalar and
`duration` is fractional milliseconds — every one of those is a place where a plausible
mapping is silently wrong and a cost or latency column quietly reads zero.

No network: `opik_client.request` / `post_idempotent` are stubbed, so the suite stays
offline like the rest of it.
"""
from __future__ import annotations

import uuid

import pytest

from src.aiobs import opik_client, opik_store
from src.aiobs.opik_store import OpikTraceStore
from src.aiobs.store import TraceStore
from src.aiobs.types import KIND_LLM, Span, Trace


# A trace row exactly as the live API returns one (SDK-written, nested spans).
LIVE_TRACE = {
    "id": "01a05d64-30fd-757c-83a6-00d4ec95a287",
    "name": "agent",
    "start_time": "2026-09-01T14:32:10.123456Z",
    "end_time": "2026-09-01T14:32:20.444456Z",
    "duration": 10.321,                       # fractional ms, not seconds
    "span_count": 2,
    "llm_span_count": 1,
    "usage": {"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540},
    "total_estimated_cost": 0.0087,
    "providers": ["anthropic"],
    "thread_id": "sess-42",
    "input": {"question": "what is the capital of France?"},
    "output": {"answer": "Paris"},
    "metadata": {"auraOtelTraceId": "d50e355e6bc3e0609e42ae5b3355f375",
                 "tenantId": "user-7"},
    "tags": ["aura", "agent"],
    "feedback_scores": [
        {"name": "relevance", "value": 0.9, "reason": "answers directly",
         "source": "online_scoring", "last_updated_at": "2026-09-01T14:40:00Z"},
    ],
}

LIVE_SPAN = {
    "id": "01a05d64-30fe-7821-8371-e3347f88447e",
    "trace_id": LIVE_TRACE["id"],
    "parent_span_id": None,
    "name": "chat_completion",
    "type": "llm",
    "start_time": "2026-09-01T14:32:10.123456Z",
    "end_time": "2026-09-01T14:32:20.020456Z",
    "duration": 9897.0,
    "model": "claude-sonnet-4-5",
    "provider": "anthropic",
    "usage": {"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540},
    "total_estimated_cost": 0.0087,
    "input": {"preview": "summarise this repo"},
    "output": {"preview": "It is a FastAPI service."},
    "metadata": {"auraSpanId": "abc123def456", "cacheReadTokens": 64},
}


@pytest.fixture
def store():
    return OpikTraceStore()


@pytest.fixture
def fake_opik(monkeypatch):
    """Records requests and replays canned responses.

    Keyed on (method, path) prefix rather than exact URL so a test can set one
    response and not care about paging params.
    """
    calls: list[dict] = []
    responses: dict[tuple[str, str], object] = {}
    posts: dict[str, bool] = {}

    def request(method, path, *, json_body=None, params=None):
        calls.append({"method": method, "path": path, "params": params or {},
                      "body": json_body})
        for (m, prefix), value in responses.items():
            if m == method and path.startswith(prefix):
                return value
        return None

    def write(path, body, method="POST"):
        calls.append({"method": method, "path": path, "body": body, "params": {}})
        for prefix, ok in posts.items():
            if path.startswith(prefix):
                return ok
        return True

    monkeypatch.setattr(opik_client, "request", request)
    monkeypatch.setattr(opik_client, "write", write)
    monkeypatch.setattr(opik_client, "post_idempotent",
                        lambda path, body: write(path, body, "POST"))
    monkeypatch.setattr(opik_client, "health", lambda: True)
    return type("FakeOpik", (), {"calls": calls, "responses": responses,
                                 "posts": posts})()


# ── Deterministic ids ─────────────────────────────────────────────────────────

def test_derived_id_is_a_version_7_uuid():
    """Opik REJECTS anything else on write: 400 "Trace id must be a version 7 UUID".
    This is unconditional, so a v4 fallback would fail every single write."""
    got = opik_client.deterministic_uuid7(1_760_000_000_000, "some-otel-id")
    parsed = uuid.UUID(got)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_derived_id_is_stable_so_writes_are_idempotent():
    """The TraceStore contract requires write_trace to be idempotent because OTLP
    exporters retry. Idempotency here comes entirely from the id being a pure
    function of (start_time, otel id) — a retry produces the same id and Opik
    answers 409, which post_idempotent treats as success."""
    a = opik_client.deterministic_uuid7(1_760_000_000_000, "trace-abc")
    b = opik_client.deterministic_uuid7(1_760_000_000_000, "trace-abc")
    assert a == b


@pytest.mark.parametrize("ts,seed", [(1_760_000_000_001, "trace-abc"),
                                     (1_760_000_000_000, "trace-abd")])
def test_derived_id_changes_with_either_input(ts, seed):
    base = opik_client.deterministic_uuid7(1_760_000_000_000, "trace-abc")
    assert opik_client.deterministic_uuid7(ts, seed) != base


def test_derived_id_encodes_the_real_timestamp():
    """The 48-bit prefix is the trace's own start time, which is what gives
    ClickHouse the time ordering its ORDER BY assumes."""
    ts = 1_760_000_000_000
    got = uuid.UUID(opik_client.deterministic_uuid7(ts, "x"))
    assert (got.int >> 80) == ts


def test_span_and_trace_ids_do_not_collide():
    """Same trace, same timestamp — the span id must still differ, or a span would
    overwrite its own trace."""
    tid = opik_store.opik_trace_id("otel-1", "2026-09-01T00:00:00Z")
    sid = opik_store.opik_span_id("otel-1", "span-1", "2026-09-01T00:00:00Z")
    assert tid != sid


# ── Read mapping ──────────────────────────────────────────────────────────────

def test_trace_row_maps_every_aura_field():
    row = opik_store._trace_row(LIVE_TRACE, "my-project")
    assert row["traceId"] == LIVE_TRACE["id"]
    assert row["projectId"] == "my-project"
    assert row["threadId"] == "sess-42"
    assert row["name"] == "agent"
    assert row["status"] == "ok"
    assert row["spanCount"] == 2
    # usage is a MAP; a naive int() on it would be 0
    assert row["totalTokens"] == 1540
    assert row["costUsd"] == pytest.approx(0.0087)
    # duration is fractional ms, NOT seconds
    assert row["latencyMs"] == 10
    assert row["providers"] == ["anthropic"]


def test_trace_row_preserves_the_originating_otel_id():
    """Opik does not keep OTel trace ids — it mints its own v7. The original is
    carried in metadata so a customer's logs and existing deep links still
    correlate."""
    row = opik_store._trace_row(LIVE_TRACE, "p")
    assert row["otelTraceId"] == "d50e355e6bc3e0609e42ae5b3355f375"
    assert row["tenantId"] == "user-7"


def test_error_info_becomes_aura_status():
    """Opik has no scalar status field; failure is error_info presence."""
    assert opik_store._trace_row(LIVE_TRACE, "p")["status"] == "ok"
    failed = {**LIVE_TRACE, "error_info": {"message": "boom"}}
    assert opik_store._trace_row(failed, "p")["status"] == "error"


def test_online_scores_are_projected_from_feedback_scores():
    """Stored once, in Opik's native feedback scores, and projected into the
    `onlineScores` key the existing UI reads — so a human annotation and a judge
    score cannot disagree about the same trace."""
    row = opik_store._trace_row(LIVE_TRACE, "p")
    assert row["onlineScores"] == [
        {"name": "relevance", "value": 0.9, "passed": True, "reason": "answers directly"}]


def test_online_scored_at_is_derived_so_the_sweep_terminates():
    """online_eval.run_sweep skips traces carrying `onlineScoredAt`. Opik has no such
    column, so without deriving it the sweep would re-judge — and re-bill — the same
    traces on every run, forever."""
    assert opik_store._trace_row(LIVE_TRACE, "p")["onlineScoredAt"]
    unscored = {**LIVE_TRACE, "feedback_scores": []}
    assert opik_store._trace_row(unscored, "p")["onlineScoredAt"] == ""


def test_span_row_maps_tokens_cost_and_kind():
    row = opik_store._span_row(LIVE_SPAN)
    assert row["kind"] == KIND_LLM
    assert row["model"] == "claude-sonnet-4-5"
    assert row["inputTokens"] == 1200
    assert row["outputTokens"] == 340
    assert row["totalTokens"] == 1540
    assert row["cacheReadTokens"] == 64
    assert row["latencyMs"] == 9897
    assert row["parentSpanId"] == ""            # Opik sends null, Aura wants ""
    # Opik stores payloads itself, so there is no S3 ref to follow.
    assert row["inputRef"] == "" and row["outputRef"] == ""
    assert row["inputPreview"] == "summarise this repo"


def test_opik_general_span_type_maps_to_unknown():
    """Opik's default type is "general"; Aura's vocabulary calls that "unknown"."""
    assert opik_store._span_row({**LIVE_SPAN, "type": "general"})["kind"] == "unknown"


def test_usage_total_falls_back_to_summing():
    """Some integrations populate prompt/completion but not total."""
    assert opik_store._usage_total({"prompt_tokens": 10, "completion_tokens": 5}) == 15
    assert opik_store._usage_total(None) == 0
    assert opik_store._usage_total("nonsense") == 0


# ── Store behaviour ───────────────────────────────────────────────────────────

def test_satisfies_the_trace_store_protocol(store):
    """The whole point: the router, UI and online-eval sweep call through this
    protocol, so conformance is what makes the swap a one-line change."""
    assert isinstance(store, TraceStore)


def test_list_traces_pushes_filters_down_to_the_engine(store, fake_opik):
    fake_opik.responses[("GET", "v1/private/traces")] = {"content": [LIVE_TRACE]}
    rows = store.list_traces("my-project", limit=10, thread_id="sess-42",
                             tenant_id="user-7", search="France")
    assert len(rows) == 1
    params = fake_opik.calls[-1]["params"]
    assert params["project_name"] == "my-project"
    assert params["search"] == "France"
    assert params["truncate"] == "true"          # list view never fetches full prompts
    assert "thread_id" in params["filters"]
    assert "tenantId" in params["filters"]
    assert "DESC" in params["sorting"]


def test_list_traces_survives_an_opik_outage(store, fake_opik):
    """Reads degrade to empty rather than raising — same contract as the DynamoDB
    store. capabilities() reports `degraded` so the UI can tell the difference
    between an outage and a genuinely empty project."""
    assert store.list_traces("my-project") == []


def test_get_trace_scopes_a_direct_id_lookup_to_the_project(store, fake_opik):
    """A UUID is globally unique, so the project must still be checked — trace ids
    come from client SDKs and must not be trusted alone."""
    fake_opik.responses[("GET", "v1/private/traces/")] = {
        **LIVE_TRACE, "project_name": "other-project"}
    assert store.get_trace("my-project", LIVE_TRACE["id"]) is None


def test_get_trace_finds_an_aura_id_via_metadata(store, fake_opik):
    """Non-UUID ids cannot be recomputed without the start time, so they are looked
    up by the OTel id stored in metadata."""
    fake_opik.responses[("GET", "v1/private/traces")] = {"content": [LIVE_TRACE]}
    got = store.get_trace("my-project", "d50e355e6bc3e0609e42ae5b3355f375")
    assert got is not None and got["traceId"] == LIVE_TRACE["id"]
    assert "auraOtelTraceId" in fake_opik.calls[-1]["params"]["filters"]


def test_write_trace_sends_a_v7_id_and_keeps_the_otel_id(store, fake_opik):
    trace = Trace(trace_id="d50e355e6bc3e0609e42ae5b3355f375", project_id="p",
                  tenant_id="user-7", name="chat",
                  start_time="2026-09-01T14:32:10.123456Z",
                  end_time="2026-09-01T14:32:20Z", thread_id="sess-1")
    span = Span(trace_id=trace.trace_id, span_id="span-1", name="llm",
                start_time=trace.start_time, end_time=trace.end_time,
                kind=KIND_LLM, model="claude-sonnet-4-5", provider="anthropic",
                input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001)
    store.write_trace(trace, [span])

    posted = [c for c in fake_opik.calls if c["method"] == "POST"]
    trace_body = posted[0]["body"]
    assert uuid.UUID(trace_body["id"]).version == 7
    assert trace_body["metadata"]["auraOtelTraceId"] == trace.trace_id
    assert trace_body["metadata"]["tenantId"] == "user-7"
    assert trace_body["thread_id"] == "sess-1"

    # Spans go through the BATCH endpoint: one export can carry hundreds and a POST
    # each would multiply ingest latency by the round-trip count.
    assert posted[1]["path"].endswith("/spans/batch")
    span_body = posted[1]["body"]["spans"][0]
    assert uuid.UUID(span_body["id"]).version == 7
    # Aura's calculate_cost_v2 figure is sent explicitly so the UI and the invoice
    # cannot disagree.
    assert span_body["total_estimated_cost"] == pytest.approx(0.001)
    assert span_body["metadata"]["auraSpanId"] == "span-1"


def test_write_trace_is_idempotent_for_a_retried_export(store, fake_opik):
    trace = Trace(trace_id="otel-abc", project_id="p", tenant_id="t", name="x",
                  start_time="2026-09-01T14:32:10Z", end_time="2026-09-01T14:32:11Z")
    store.write_trace(trace, [])
    store.write_trace(trace, [])
    ids = [c["body"]["id"] for c in fake_opik.calls
           if c["method"] == "POST" and c["path"] == "v1/private/traces"]
    assert len(ids) == 2 and ids[0] == ids[1]


def test_failed_trace_write_raises_but_failed_spans_do_not(store, fake_opik):
    """Losing the trace row loses the whole thing from the list view, so that
    raises. Losing spans still leaves a row carrying the aggregates, so that is
    logged — the same asymmetry DynamoTraceStore already had."""
    trace = Trace(trace_id="otel-abc", project_id="p", tenant_id="t", name="x",
                  start_time="2026-09-01T14:32:10Z", end_time="2026-09-01T14:32:11Z")
    fake_opik.posts["v1/private/traces"] = False
    with pytest.raises(RuntimeError):
        store.write_trace(trace, [])

    fake_opik.posts["v1/private/traces"] = True
    fake_opik.posts["v1/private/spans/batch"] = False
    span = Span(trace_id="otel-abc", span_id="s1", name="llm",
                start_time=trace.start_time, end_time=trace.end_time)
    store.write_trace(trace, [span])          # must not raise


def test_record_scores_writes_native_feedback_scores(store, fake_opik):
    from src.aiobs.metrics import Score
    ok = store.record_scores("p", {"traceId": LIVE_TRACE["id"]},
                             [Score("relevance", 0.9, True, "good")])
    assert ok
    body = fake_opik.calls[-1]["body"]["scores"][0]
    assert body["name"] == "relevance"
    assert body["value"] == pytest.approx(0.9)
    # Opik's own vocabulary for machine scoring, so Aura's sweep sits alongside
    # Opik's automation rules rather than looking like a hand annotation.
    assert body["source"] == "online_scoring"


def test_capabilities_reports_what_clickhouse_can_actually_do(store, fake_opik):
    caps = store.capabilities()
    assert caps["store"] == "opik"
    assert caps["fullTextSearch"] is True
    assert caps["tagFilter"] is True
    assert caps["aggregations"] is True
    assert caps["degraded"] is False


def test_capabilities_reports_degraded_when_opik_is_unreachable(store, monkeypatch):
    monkeypatch.setattr(opik_client, "health", lambda: False)
    assert store.capabilities()["degraded"] is True


# ── The swap itself ───────────────────────────────────────────────────────────

def test_get_store_is_settings_driven_and_reversible(monkeypatch):
    """The migration must be reversible WITHOUT a deploy: if Opik misbehaves,
    aiobs_store=dynamodb puts the read path back on DynamoDB."""
    from src.aiobs import service
    from src.config_settings import get_settings

    settings = get_settings()
    for choice, expected in (("opik", "opik"), ("dynamodb", "dynamodb")):
        monkeypatch.setattr(settings, "aiobs_store", choice, raising=False)
        service.set_store(None)
        assert service.get_store().name == expected
    service.set_store(None)


def test_unknown_store_name_falls_back_instead_of_crashing(monkeypatch):
    """A typo in an env var must not take the whole API down."""
    from src.aiobs import service
    from src.config_settings import get_settings

    monkeypatch.setattr(get_settings(), "aiobs_store", "clickhaus", raising=False)
    service.set_store(None)
    assert service.get_store().name == "dynamodb"
    service.set_store(None)


def test_dynamo_store_ignores_search_rather_than_scanning(fake_dynamo):
    """DynamoDB has no index for free text. Accepting the argument and ignoring it
    is correct; the ROUTER refuses the request so the caller is never handed a
    complete list that looks filtered."""
    from src.aiobs.dynamo_store import DynamoTraceStore
    store = DynamoTraceStore()
    assert store.list_traces("p", search="anything", tenant_id="t") == []
    assert store.capabilities()["fullTextSearch"] is False


def test_dynamo_list_projects_applies_the_tenant_predicate(fake_dynamo):
    """The predicate the router was missing: without it any caller holding
    dev_workspace enumerated every tenant's project names."""
    from src.aiobs.dynamo_store import DynamoTraceStore
    from src.database import dynamo_client as db

    for i, (project, tenant) in enumerate([("mine", "user-1"), ("theirs", "user-2")]):
        db.put_item("ai-traces", {"projectId": project, "sortKey": f"t{i}#x{i}",
                                  "traceId": f"x{i}", "tenantId": tenant,
                                  "startTime": f"2026-09-0{i + 1}T00:00:00Z",
                                  "costUsd": 0.5})

    store = DynamoTraceStore()
    scoped = {p["projectId"] for p in store.list_projects(tenant_id="user-1")}
    assert scoped == {"mine"}
    assert {p["projectId"] for p in store.list_projects()} == {"mine", "theirs"}


# ── Regressions for bugs a stubbed test could not have found ──────────────────
# All three were found by running against a real Opik 2.2.46 and would have shipped
# as silent wrong answers rather than errors.

def test_metadata_keys_never_contain_dots():
    """Opik's metadata filter is a DICTIONARY field whose `key` is read as a JSON
    PATH. A dotted key like "aura.otel_trace_id" is looked up as nested
    {"aura": {"otel_trace_id": ...}}, matches nothing, and returns ZERO ROWS rather
    than erroring — so `get_trace(otel_id)` silently found nothing."""
    assert "." not in opik_client._OTEL_ID_KEY


def test_get_spans_always_sends_a_project(store, fake_opik):
    """Opik answers `GET /v1/private/spans` without a project with
    400 "Either 'project_name' or 'project_id' query params must be provided".
    The DynamoDB store needed no project because trace_id was its partition key,
    so this parameter was simply missing."""
    fake_opik.responses[("GET", "v1/private/spans")] = {"content": [LIVE_SPAN]}
    rows = store.get_spans(LIVE_TRACE["id"], project_id="my-project")
    assert len(rows) == 1
    params = fake_opik.calls[-1]["params"]
    assert params.get("project_name") or params.get("project_id")


def test_get_spans_resolves_the_project_when_not_given(store, fake_opik):
    """Callers that only hold a trace id still work — at the cost of one lookup."""
    fake_opik.responses[("GET", "v1/private/traces/")] = {
        **LIVE_TRACE, "project_id": "0190babc-62a0-71d2-832a-0feffa4676eb"}
    fake_opik.responses[("GET", "v1/private/spans")] = {"content": [LIVE_SPAN]}
    assert store.get_spans(LIVE_TRACE["id"]) != []
    assert fake_opik.calls[-1]["params"].get("project_id")


def test_feedback_scores_are_sent_with_PUT_not_POST(store, fake_opik):
    """Opik declares `@PUT /feedback-scores`; a POST returns 405 Method Not Allowed.
    That surfaced as a quiet "scores not persisted", which would have meant online
    eval paying for judges forever and storing nothing."""
    from src.aiobs.metrics import Score
    store.record_scores("p", {"traceId": LIVE_TRACE["id"]},
                        [Score("relevance", 0.9, True, "good")])
    call = fake_opik.calls[-1]
    assert call["path"].endswith("feedback-scores")
    assert call["method"] == "PUT"

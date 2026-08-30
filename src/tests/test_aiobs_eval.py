"""Evaluation: metrics, judges, datasets, experiments, prompts, online sampling.

The behaviours worth protecting are the ones that decide whether a number can be
trusted: a judge that cannot be parsed must not read as a pass, a heuristic that
raises must not void a run, and sampling must be stable so reruns do not re-bill.
"""
from __future__ import annotations

import pytest

from src.aiobs import experiments, judges, metrics, online_eval, prompts
from src.aiobs.metrics import Score


# ── Heuristic metrics ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,kwargs,passed", [
    ("exact_match", {"output": "Hello There", "expected": "hello there"}, True),
    ("exact_match", {"output": "nope", "expected": "hello"}, False),
    ("contains", {"output": "the answer is 42 exactly", "expected": "42"}, True),
    ("contains", {"output": "no digits", "expected": "42"}, False),
    ("regex_match", {"output": "order #12345", "pattern": r"#\d+"}, True),
    ("is_json", {"output": '{"a": 1}'}, True),
    ("is_json", {"output": "not json"}, False),
    ("not_empty", {"output": "  "}, False),
    ("latency_under", {"latency_ms": 100, "threshold_ms": 500}, True),
    ("latency_under", {"latency_ms": 900, "threshold_ms": 500}, False),
    ("cost_under", {"cost_usd": 0.001, "threshold_usd": 0.05}, True),
])
def test_heuristics(name, kwargs, passed):
    assert metrics.run_heuristic(name, **kwargs).passed is passed


def test_exact_match_ignores_whitespace_and_case():
    """Otherwise every trailing newline from a model reads as a regression."""
    assert metrics.run_heuristic(
        "exact_match", output="  Yes \n", expected="yes").passed


def test_json_has_keys_reports_partial_credit():
    s = metrics.run_heuristic("json_has_keys", output='{"a":1,"b":2}',
                              keys=["a", "b", "c"])
    assert s.value == pytest.approx(2 / 3) and not s.passed
    assert "c" in s.reason


def test_an_unknown_metric_fails_visibly():
    s = metrics.run_heuristic("no_such_metric", output="x")
    assert not s.passed and "unknown metric" in s.reason


def test_a_raising_metric_does_not_void_the_run(monkeypatch):
    def boom(**_):
        raise RuntimeError("bad regex engine")
    monkeypatch.setitem(metrics.HEURISTICS, "explodes", boom)
    s = metrics.run_heuristic("explodes", output="x")
    assert not s.passed and "metric raised" in s.reason


def test_aggregate_reports_per_metric_and_overall():
    agg = metrics.aggregate([
        Score("a", 1.0, True), Score("a", 0.0, False), Score("b", 1.0, True, cost_usd=0.01)])
    assert agg["metrics"]["a"]["passRate"] == 0.5
    assert agg["metrics"]["a"]["mean"] == 0.5
    assert agg["metrics"]["b"]["costUsd"] == 0.01
    assert agg["overallPassRate"] == pytest.approx(2 / 3, abs=1e-4)
    assert agg["totalCostUsd"] == 0.01


def test_aggregate_of_nothing_is_not_a_crash():
    assert metrics.aggregate([])["overallPassRate"] == 0.0


# ── Judge verdict parsing ────────────────────────────────────────────────────

@pytest.mark.parametrize("reply,value", [
    ('{"score": 0.9, "reason": "grounded"}', 0.9),
    ('Sure! {"score": 0.25, "reason": "partly"} hope that helps', 0.25),
    ('```json\n{"score": 1.0, "reason": "ok"}\n```', 1.0),
    ('{"score": 5, "reason": "out of range"}', 1.0),      # clamped
    ('{"score": -2, "reason": "negative"}', 0.0),         # clamped
])
def test_verdict_parsing_tolerates_model_chatter(reply, value):
    """Models add prose despite instructions; a strict parser would score 0 on
    perfectly good verdicts."""
    assert judges._parse_verdict(reply)[0] == pytest.approx(value)


def test_an_unparseable_verdict_fails_closed():
    """An unreadable judge reply must never count as a pass."""
    value, reason = judges._parse_verdict("I cannot evaluate this content.")
    assert value == 0.0 and "unparseable" in reason


def test_judge_scores_and_records_cost(monkeypatch):
    class Call:
        cost_usd = 0.0021
    monkeypatch.setattr(judges, "_invoke",
                        lambda *a, **k: ('{"score": 0.85, "reason": "grounded"}', Call()))
    s = judges.run_judge("hallucination", output="x", context="y")
    assert s.passed and s.value == 0.85 and s.cost_usd == 0.0021


def test_a_judge_outage_degrades_rather_than_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bedrock unavailable")
    monkeypatch.setattr(judges, "_invoke", boom)
    s = judges.run_judge("relevance", output="x")
    assert not s.passed and "judge unavailable" in s.reason


def test_unknown_judge_is_reported():
    assert "unknown judge" in judges.run_judge("vibes", output="x").reason


def test_natural_language_assertion(monkeypatch):
    monkeypatch.setattr(judges, "_invoke",
                        lambda *a, **k: ('{"score": 1.0, "reason": "mentions refund"}', object()))
    s = judges.run_assertion("The reply must mention the refund policy", output="...")
    assert s.passed and s.metadata["assertion"].startswith("The reply must")


# ── Experiments ──────────────────────────────────────────────────────────────

@pytest.fixture
def dataset(fake_dynamo):
    from src.aiobs import datasets
    ds = datasets.create("regression", "proj", "alice")
    datasets.add_item(ds["datasetId"], "2+2?", "4")
    datasets.add_item(ds["datasetId"], "capital of France?", "Paris")
    return ds


def test_dataset_tracks_its_item_count(dataset):
    from src.aiobs import datasets
    assert datasets.get(dataset["datasetId"])["itemCount"] == 2
    assert len(datasets.items(dataset["datasetId"])) == 2


def test_experiment_scores_every_item_and_aggregates(dataset, fake_dynamo):
    exp = experiments.create("v1", dataset["datasetId"], "proj", "alice",
                             {"metrics": ["exact_match"]})
    result = experiments.run(exp["experimentId"], lambda text: {
        "output": "4" if "2+2" in text else "Berlin"})
    assert result["status"] == "completed"
    assert result["itemCount"] == 2
    assert result["overallPassRate"] == 0.5        # one right, one wrong
    rows = experiments.results(exp["experimentId"])
    assert len(rows) == 2
    assert sum(1 for r in rows if r["passed"]) == 1


def test_a_failing_target_is_recorded_not_fatal(dataset, fake_dynamo):
    exp = experiments.create("v2", dataset["datasetId"], "proj", "alice",
                             {"metrics": ["not_empty"]})

    def flaky(text):
        if "2+2" in text:
            raise RuntimeError("model timeout")
        return {"output": "Paris"}

    result = experiments.run(exp["experimentId"], flaky)
    assert result["status"] == "completed"
    assert result["failedItems"] == 1
    assert result["itemCount"] == 2               # the run still covered both


def test_an_empty_dataset_is_reported_not_run(fake_dynamo):
    from src.aiobs import datasets
    ds = datasets.create("empty", "proj", "alice")
    exp = experiments.create("x", ds["datasetId"], "proj", "alice", {})
    assert experiments.run(exp["experimentId"], lambda t: {})["status"] == "empty"


def test_experiment_summary_is_persisted_for_comparison(dataset, fake_dynamo):
    exp = experiments.create("v1", dataset["datasetId"], "proj", "alice",
                             {"metrics": ["not_empty"]})
    experiments.run(exp["experimentId"], lambda t: {"output": "something"})
    compared = experiments.compare([exp["experimentId"]])
    assert compared["experiments"][0]["summary"]["overallPassRate"] == 1.0


# ── Prompts ──────────────────────────────────────────────────────────────────

def test_versions_are_monotonic_and_immutable(fake_dynamo):
    prompts.save("greet", "Hello {name}", "alice")
    prompts.save("greet", "Hi {name}!", "alice")
    found = prompts.versions("greet")
    assert [v["version"] for v in found] == ["v000001", "v000002"]
    assert found[0]["template"] == "Hello {name}"      # v1 unchanged


def test_saving_an_identical_template_does_not_create_a_version(fake_dynamo):
    prompts.save("same", "text", "alice")
    prompts.save("same", "text", "alice")
    assert len(prompts.versions("same")) == 1


def test_version_ordering_survives_double_digits(fake_dynamo):
    """Zero-padding matters: DynamoDB sorts strings, so v10 < v9 without it."""
    for i in range(12):
        prompts.save("many", f"template {i}", "alice")
    assert prompts.latest("many")["version"] == "v000012"


def test_render_leaves_unfilled_variables_visible():
    assert prompts.render("Hi {name}, you are {age}", {"name": "Sam"}) == \
        "Hi Sam, you are {age}"


# ── Online evaluation ────────────────────────────────────────────────────────

def test_sampling_is_deterministic_per_trace():
    """Random sampling would score some traces twice across sweeps and others
    never, and each judge call costs money."""
    first = [online_eval.should_sample(f"trace-{i}", 0.5) for i in range(200)]
    second = [online_eval.should_sample(f"trace-{i}", 0.5) for i in range(200)]
    assert first == second


def test_sampling_rate_is_roughly_honoured():
    hits = sum(online_eval.should_sample(f"t{i}", 0.25) for i in range(2000))
    assert 400 < hits < 600            # 25% of 2000, with hash slack


@pytest.mark.parametrize("rate,expected", [(0.0, False), (1.0, True)])
def test_sampling_boundaries(rate, expected):
    assert online_eval.should_sample("any-trace", rate) is expected


def test_sweep_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(online_eval, "get_config",
                        lambda: {"enabled": False, "sampleRate": 1.0,
                                 "judges": [], "projectId": "p"})
    assert online_eval.run_sweep()["status"] == "disabled"


def test_sample_rate_is_clamped(fake_dynamo):
    """A rate above 1.0 is meaningless, and 100% sampling on a busy project is an
    unbounded bill."""
    assert online_eval.set_config(True, 9.0, ["relevance"], "p", "a")["sampleRate"] == 1.0
    assert online_eval.set_config(True, -3.0, ["relevance"], "p", "a")["sampleRate"] == 0.0


# ── Scheduler wiring ─────────────────────────────────────────────────────────

def test_online_eval_is_registered_as_a_job():
    from src.scheduler.jobs import JOB_DEFS
    job = next((j for j in JOB_DEFS if j["id"] == "online_eval_job"), None)
    assert job is not None
    # Hourly, not daily: a daily sweep would surface a quality regression up to a
    # day after it started.
    assert job["schedule"].split()[1] == "*"


def test_the_job_is_a_noop_while_disabled(monkeypatch):
    """Default-off matters — every judged trace is a billable model call."""
    from src.scheduler import jobs
    monkeypatch.setattr(online_eval, "get_config",
                        lambda: {"enabled": False, "sampleRate": 0.05,
                                 "judges": ["relevance"], "projectId": "p"})
    recorded = {}
    monkeypatch.setattr(jobs, "_record_run",
                        lambda job_id, result, duration: recorded.update(result))
    jobs.online_eval_job()
    assert recorded["status"] == "disabled"
    assert recorded["scored"] == 0


def test_a_failing_sweep_is_recorded_not_raised(monkeypatch):
    from src.scheduler import jobs

    def boom():
        raise RuntimeError("judge backend down")

    monkeypatch.setattr(online_eval, "run_sweep", boom)
    recorded = {}
    monkeypatch.setattr(jobs, "_record_run",
                        lambda job_id, result, duration: recorded.update(result))
    jobs.online_eval_job()            # must not propagate into the scheduler
    assert "judge backend down" in recorded["error"]

"""Coverage arithmetic — and the two ways it is easy to make it lie.

The number these produce is low on a real project, because non-GET routes and
parameterised paths cannot be executed and a Service node has no address. That is the
honest answer; the temptation is to inflate it by counting a case that never ran as
covered, and the first two tests here exist to make that impossible to do quietly.
"""
from __future__ import annotations

from src.qatest import coverage as cov
from src.qatest.types import Case, Report, Step


def _case(cid, eid, label="API", kind="api", method="GET", path="/x", skip=""):
    return Case(case_id=cid, kind=kind, name=f"{method} {path}",
                verifies_label=label, verifies_eid=eid, method=method, path=path,
                skip_reason=skip)


def _step(cid, status, index=1):
    return Step(index=index, action="a", target="t", status=status, case_id=cid)


def _report(cases):
    return Report(run_id="r1", project_id="p1", app_url="http://x", cases=cases)


# ── The numerator must mean "verified" ───────────────────────────────────────

def test_only_a_passing_case_covers_its_node():
    report = _report([_case("c1", "api:1"), _case("c2", "api:2")])
    out = cov.summarise(report, [_step("c1", "passed"), _step("c2", "failed")],
                        {"apis": 2, "services": 0})
    assert out["nodeCovered"] == 1
    assert out["nodePct"] == 50


def test_a_node_with_one_passing_and_one_failing_case_is_not_covered():
    """Worst status wins. Anything else lets a broken endpoint be reported as verified
    because some other case happened to touch the same node."""
    report = _report([_case("c1", "api:1"), _case("c2", "api:1")])
    out = cov.summarise(report, [_step("c1", "passed"), _step("c2", "failed")],
                        {"apis": 1, "services": 0})
    assert out["nodeCovered"] == 0


def test_a_skipped_case_does_not_cover_its_node():
    """The regression this whole module exists for: `covered` used to be derived from
    the PLAN, so a run where everything was skipped reported full coverage."""
    report = _report([_case("c1", "api:1", skip="POST needs a request body")])
    out = cov.summarise(report, [_step("c1", "skipped")], {"apis": 1, "services": 0})
    assert out["nodeCovered"] == 0
    assert out["uncovered"][0]["reason"] == "POST needs a request body"


def test_a_case_that_never_ran_is_reported_as_not_run_with_a_reason():
    """A kind the user excluded is not the application's fault, and the wording has to
    keep those apart."""
    report = _report([_case("c1", "api:1"), _case("c2", "api:2")])
    out = cov.summarise(report, [_step("c1", "passed")], {"apis": 2, "services": 0})
    missing = [n for n in out["uncovered"] if n["externalId"] == "api:2"][0]
    assert missing["result"] == "not-run"
    assert missing["reason"] == cov.NOT_SELECTED


# ── Services are counted apart, because they can never pass ──────────────────

def test_services_are_reported_separately_and_explain_their_zero():
    """Every smoke case is recorded as skipped, so a combined denominator would cap
    coverage below 100% for a reason that says nothing about the application."""
    report = _report([_case("c1", "api:1"),
                      _case("s1", "svc:1", label="Service", kind="smoke",
                            skip="no HTTP address for a Service node")])
    out = cov.summarise(report, [_step("c1", "passed"), _step("s1", "skipped")],
                        {"apis": 1, "services": 1})
    assert out["api"]["pct"] == 100
    assert out["service"]["covered"] == 0
    assert "not an address" in out["service"]["note"]


# ── Execution rate ───────────────────────────────────────────────────────────

def test_execution_rate_counts_cases_that_produced_a_step():
    report = _report([_case(f"c{i}", f"api:{i}") for i in range(1, 5)])
    out = cov.summarise(report, [_step("c1", "passed"), _step("c2", "skipped")],
                        {"apis": 4, "services": 0})
    assert (out["planned"], out["executed"], out["executionPct"]) == (4, 2, 50)
    assert out["skipped"] == 1


def test_unemulated_is_counted_apart_from_skipped():
    """"We asked and the harness could not answer" is not "we declined to ask", and
    only one of them is a statement about the application."""
    report = _report([_case("c1", "api:1"), _case("c2", "api:2")])
    out = cov.summarise(report, [_step("c1", "unemulated"), _step("c2", "skipped")],
                        {"apis": 2, "services": 0})
    assert out["unemulated"] == 1 and out["skipped"] == 1


# ── Denominator honesty ──────────────────────────────────────────────────────

def test_no_nodes_reports_none_rather_than_zero_percent():
    """0% says "nothing works". None says "there is nothing to measure". A project
    that was never analysed is the second one."""
    out = cov.summarise(_report([]), [], {"apis": 0, "services": 0})
    assert out["nodePct"] is None
    assert out["nodeTotal"] == 0


def test_a_plan_derived_denominator_says_so():
    """Without graph totals the denominator is the filtered plan's own node count,
    which understates a partial run. A reader has to be able to tell."""
    report = _report([_case("c1", "api:1")])
    out = cov.summarise(report, [_step("c1", "passed")])
    assert out["denominatorFromPlan"] is True
    assert out["nodePct"] == 100          # true of what ran, not of the project
    assert cov.summarise(report, [_step("c1", "passed")],
                         {"apis": 4, "services": 0})["nodePct"] == 25


def test_coverage_survives_the_round_trip_through_json():
    report = _report([_case("c1", "api:1")])
    report.coverage = cov.summarise(report, [_step("c1", "passed")],
                                    {"apis": 1, "services": 0})
    report.selected_kinds = ["api"]
    report.plan_total = 7
    back = Report.from_dict(report.as_dict())
    assert back.coverage["nodePct"] == 100
    assert back.selected_kinds == ["api"] and back.plan_total == 7


def test_an_old_report_with_no_coverage_still_loads():
    back = Report.from_dict({"runId": "r", "projectId": "p"})
    assert back.coverage == {} and back.selected_kinds == [] and back.plan_total == 0


def test_nodes_the_plan_never_referenced_are_accounted_for():
    """A run filtered to one kind still counts the other kinds' nodes in the
    denominator. Without saying how many, 2 of 5 covered with two uncovered rows on
    screen looks like a bug in the arithmetic."""
    from src.qatest import plan as plan_mod

    facts = {"apis": [{"eid": "a1", "method": "GET", "path": "/x"}],
             "services": [{"eid": "s1", "name": "Billing"}], "dependencies": []}
    cases = plan_mod.filter_by_kind(plan_mod.build_plan("p", facts), ["api"])
    steps = [_step("api-001", "passed")]

    out = cov.summarise(_report(cases), steps, plan_mod.graph_totals(facts))
    assert out["notPlanned"] == 1          # the Service case was filtered out
    assert out["nodeCovered"] + len(out["uncovered"]) + out["notPlanned"] \
        == out["nodeTotal"], "the coverage numbers do not add up"

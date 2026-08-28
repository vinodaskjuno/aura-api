"""The self-learning loop.

The loop is only real if the SECOND run differs from the first. These tests assert
exactly that, plus the three guards that keep it from degrading:
  * outcomes below the confidence threshold record but never teach
  * a case id can never be laundered into a citation
  * a learned artifact can be reverted, and the next run stops retrieving it
"""
from __future__ import annotations

import pytest

from src.observability import cases, outcomes, promotion, store
from src.observability.citations import validate


# ── Outcome composition ──────────────────────────────────────────────────────

def test_sources_compose_by_max_not_sum():
    verdict, conf = outcomes.compose([
        {"source": "verifier", "verdict": "confirmed", "observed_at": "1"},
        {"source": "pagerduty_notes", "verdict": "confirmed", "observed_at": "2"},
    ])
    # 0.3 and 0.5 observe the same event; summing them would fake certainty.
    assert (verdict, conf) == ("confirmed", 0.5)


def test_human_wrong_overrides_every_automated_confirm():
    verdict, conf = outcomes.compose([
        {"source": "verifier", "verdict": "confirmed", "observed_at": "1"},
        {"source": "git_correlation", "verdict": "confirmed", "observed_at": "2"},
        {"source": "human", "verdict": "wrong", "observed_at": "3"},
    ])
    assert (verdict, conf) == ("wrong", 1.0)


def test_verifier_alone_does_not_teach(fake_dynamo, fake_graph):
    store.create_investigation({"investigationId": "INV-A", "createdAt": store.now(),
                                "serviceName": "svc", "rootCause": {"statement": "x"}})
    outcome = outcomes.record("INV-A", "verifier", "confirmed")
    assert outcome.confidence == 0.3
    assert not outcome.teaches, "0.3 is below the learning threshold"
    assert store.list_cases() == [], "a weak signal must not create a case"


def test_human_verdict_teaches(fake_dynamo, fake_graph):
    store.create_investigation({"investigationId": "INV-B", "createdAt": store.now(),
                                "serviceName": "checkout-service",
                                "errorSignatures": ["OOMKilled <n>"],
                                "symptomShape": ["restart_loop"],
                                "rootCause": {"statement": "heap above pod limit",
                                              "category": "deploy"}})
    outcome = outcomes.record("INV-B", "human", "confirmed", confirmed_by="sre")
    assert outcome.confidence == 1.0 and outcome.teaches
    stored = store.list_cases()
    assert len(stored) == 1
    assert stored[0]["rootCauseCategory"] == "deploy"


# ── Retrieval ────────────────────────────────────────────────────────────────

def _seed(n: int, category: str = "deploy", service: str = "checkout-service"):
    for i in range(n):
        store.save_case({
            "caseId": f"case_{category}_{i}", "createdAt": "2026-08-0{}T10:00:00Z".format(i % 9 + 1),
            "investigationId": f"INV-{category}-{i}", "serviceName": service,
            "fingerprint": f"fp{i}", "errorSignatures": ["java.lang.OutOfMemoryError heap <n>"],
            "symptomShape": ["restart_loop", "deploy_nearby"],
            "rootCauseStatement": "heap ceiling above pod memory limit",
            "rootCauseCategory": category, "resolution": f"rollback-{i}",
            "resolutionHash": f"rh{i}", "outcomeVerdict": "confirmed",
            "outcomeConfidence": "1.0", "occurredAt": "2026-08-01T10:00:00Z"})


def test_retrieval_is_inert_below_the_corpus_floor(fake_dynamo, fake_graph):
    _seed(3)
    found = cases.retrieve("checkout-service", ["java.lang.OutOfMemoryError heap <n>"],
                           ["restart_loop"])
    assert found["below_floor"] is True
    assert found["cases"] == [], "one anecdote must not masquerade as a prior"


def test_retrieval_returns_priors_above_the_floor(fake_dynamo, fake_graph):
    _seed(6)
    found = cases.retrieve("checkout-service", ["java.lang.OutOfMemoryError heap <n>"],
                           ["restart_loop", "deploy_nearby"])
    assert not found["below_floor"]
    assert found["cases"], "similar past incidents should be retrieved"
    assert found["cases"][0]["similarity"] > 0.5
    assert found["category_priors"].get("deploy") == 1.0


def test_category_priors_dedupe_by_resolution_not_incident(fake_dynamo, fake_graph):
    """One flapping service must not manufacture a 0.9 prior."""
    for i in range(20):
        store.save_case({
            "caseId": f"case_flap_{i}", "createdAt": "2026-08-01T10:00:00Z",
            "investigationId": f"INV-flap-{i}", "serviceName": "checkout-service",
            "errorSignatures": ["OOM <n>"], "symptomShape": ["restart_loop"],
            "rootCauseStatement": "same thing again", "rootCauseCategory": "capacity",
            "resolution": "scale up", "resolutionHash": "SAME",  # identical resolution
            "outcomeVerdict": "confirmed", "outcomeConfidence": "1.0",
            "occurredAt": "2026-08-01T10:00:00Z"})
    store.save_case({
        "caseId": "case_deploy_x", "createdAt": "2026-08-02T10:00:00Z",
        "investigationId": "INV-deploy-x", "serviceName": "checkout-service",
        "errorSignatures": ["OOM <n>"], "symptomShape": ["restart_loop"],
        "rootCauseStatement": "bad deploy", "rootCauseCategory": "deploy",
        "resolution": "rollback", "resolutionHash": "OTHER",
        "outcomeVerdict": "confirmed", "outcomeConfidence": "1.0",
        "occurredAt": "2026-08-02T10:00:00Z"})

    found = cases.retrieve("checkout-service", ["OOM <n>"], ["restart_loop"])
    priors = found["category_priors"]
    # 20 identical incidents collapse to ONE distinct resolution, so the split is 50/50.
    assert priors["capacity"] == 0.5 and priors["deploy"] == 0.5, priors


def test_wrong_verdicts_become_negative_cases(fake_dynamo, fake_graph):
    _seed(6)
    store.save_case({
        "caseId": "case_wrong_1", "createdAt": "2026-08-05T10:00:00Z",
        "investigationId": "INV-wrong-1", "serviceName": "checkout-service",
        "errorSignatures": ["java.lang.OutOfMemoryError heap <n>"],
        "symptomShape": ["restart_loop"], "rootCauseStatement": "actually a bad deploy",
        "rootCauseCategory": "deploy", "wrongCategory": "network",
        "resolution": "rollback", "resolutionHash": "w1",
        "outcomeVerdict": "wrong", "outcomeConfidence": "1.0",
        "occurredAt": "2026-08-05T10:00:00Z"})
    found = cases.retrieve("checkout-service", ["java.lang.OutOfMemoryError heap <n>"],
                           ["restart_loop"])
    assert found["negative_cases"], "a wrong verdict is retained as a negative case"
    assert found["negative_cases"][0]["wrong_category"] == "network"
    assert all(c["outcome_verdict"] != "wrong" for c in found["cases"])


# ── The laundered-prior guard ────────────────────────────────────────────────

def test_case_ids_can_never_be_cited_as_evidence():
    index = [{"evidenceId": "ev_real"}]
    out = validate({"root_cause": {"statement": "x", "evidence_ids": ["ev_real"]},
                    "contributing_factors": [
                        {"statement": "seen before", "evidence_ids": ["case_abc"]}]},
                   index)
    assert "case_abc" in out["rejected_citations"]
    assert out["contributing_factors"] == []
    assert any(c["statement"] == "seen before" for c in out["unsupported_claims"])


# ── Governance: one-click revert ─────────────────────────────────────────────

def test_forget_removes_a_learned_case(fake_dynamo, fake_graph):
    _seed(6)
    before = cases.retrieve("checkout-service", ["java.lang.OutOfMemoryError heap <n>"],
                            ["restart_loop"])
    target = before["cases"][0]["case_id"]
    assert promotion.forget(target) is True
    after = cases.retrieve("checkout-service", ["java.lang.OutOfMemoryError heap <n>"],
                           ["restart_loop"])
    assert target not in [c["case_id"] for c in after["cases"]], \
        "a forgotten lesson must not be retrieved again"
    assert after["corpus_size"] == before["corpus_size"] - 1


def test_second_run_retrieves_what_the_first_run_taught(fake_dynamo, fake_graph):
    """The loop, end to end: confirm an outcome, then see it come back as a prior."""
    _seed(5)   # corpus floor is 5; this run adds the 6th
    store.create_investigation({
        "investigationId": "INV-FIRST", "createdAt": store.now(),
        "serviceName": "checkout-service",
        "errorSignatures": ["java.lang.OutOfMemoryError heap <n>"],
        "symptomShape": ["restart_loop", "deploy_nearby"],
        "rootCause": {"statement": "v2.14.3 raised heap above the pod limit",
                      "category": "deploy"},
        "resolution": "rollback to v2.14.2"})

    first = cases.retrieve("checkout-service", ["java.lang.OutOfMemoryError heap <n>"],
                           ["restart_loop"], exclude_investigation_id="INV-FIRST")
    assert "INV-FIRST" not in [c["incident_id"] for c in first["cases"]]

    outcomes.record("INV-FIRST", "human", "confirmed", confirmed_by="sre")

    second = cases.retrieve("checkout-service", ["java.lang.OutOfMemoryError heap <n>"],
                            ["restart_loop"], exclude_investigation_id="INV-SECOND")
    ids = [c["incident_id"] for c in second["cases"]]
    assert "INV-FIRST" in ids, "the confirmed investigation must now be a retrievable prior"
    assert second["corpus_size"] == first["corpus_size"] + 1

"""NIST policy checks over a project's infrastructure-as-code.

The property that matters is DISCRIMINATION. A check that always fails finds every
violation and is worthless; so is one that always passes. Every control here is asserted
in both directions against the same template with one value changed, because that is the
only evidence the check is reading what it claims to read.

The second property is honesty about scope. These read a template — they describe what a
project DECLARES, not what is deployed. A success message that omits that turns "we did
not look" into "this is fine", which is worse than having no control at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.qatest import policy

CLEAN = """
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Resources:
  Fn:
    Type: AWS::Serverless::Function
    Properties:
      Runtime: python3.12
      Environment:
        Variables:
          API_TOKEN: '{{resolve:secretsmanager:demo/token}}'
          TABLE: !Ref T
      Policies:
        - Statement:
            - Effect: Allow
              Action: dynamodb:Scan
              Resource: !GetAtt T.Arn
  T:
    Type: AWS::DynamoDB::Table
    Properties:
      SSESpecification:
        SSEEnabled: true
"""


def write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "template.yaml").write_text(body)
    return tmp_path


def run(root: Path, validator: str) -> tuple[bool, str]:
    check = next(c for c in policy.plan_checks(root) if c.validator == validator)
    return policy.run_check(root, check)


# ── IA-5(1) — the control the whole feature was asked for ────────────────────

def test_a_literal_secret_in_an_env_var_is_found(tmp_path):
    root = write(tmp_path, CLEAN.replace(
        "API_TOKEN: '{{resolve:secretsmanager:demo/token}}'", "DB_PASSWORD: hunter2"))
    ok, detail = run(root, "lambda_env_no_secrets")
    assert not ok
    assert "DB_PASSWORD" in detail


def test_a_referenced_secret_passes(tmp_path):
    """The compliant pattern. Flagging this too would make the control useless — every
    project would fail it and everyone would learn to ignore it."""
    ok, detail = run(write(tmp_path, CLEAN), "lambda_env_no_secrets")
    assert ok
    # The success message must not overstate what was checked.
    assert "declared config only" in detail


def test_a_non_secret_env_var_is_not_flagged(tmp_path):
    """TABLE is a literal too. Only names that suggest a credential are candidates."""
    ok, _ = run(write(tmp_path, CLEAN.replace("TABLE: !Ref T", "TABLE: my-table")),
                "lambda_env_no_secrets")
    assert ok


# ── AC-6(1) ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field", ["Action", "Resource"])
def test_a_wildcard_is_found_in_either_field(tmp_path, field):
    body = CLEAN.replace(f"{field}: dynamodb:Scan" if field == "Action"
                         else f"{field}: !GetAtt T.Arn", f"{field}: '*'")
    ok, detail = run(write(tmp_path, body), "iam_no_wildcards")
    assert not ok and field in detail


def test_named_actions_pass(tmp_path):
    ok, _ = run(write(tmp_path, CLEAN), "iam_no_wildcards")
    assert ok


# ── SI-2 ─────────────────────────────────────────────────────────────────────

def test_a_deprecated_runtime_is_found(tmp_path):
    ok, detail = run(write(tmp_path, CLEAN.replace("python3.12", "python3.8")),
                     "runtime_supported")
    assert not ok and "python3.8" in detail


def test_a_current_runtime_passes_and_dates_itself(tmp_path):
    """A list of deprecated runtimes is wrong the moment it stops being maintained, so
    the message says when it was true rather than implying it is timeless."""
    ok, detail = run(write(tmp_path, CLEAN), "runtime_supported")
    assert ok and "2026-09" in detail


# ── SC-13 ────────────────────────────────────────────────────────────────────

def test_an_unencrypted_table_is_found(tmp_path):
    body = CLEAN.replace("""      SSESpecification:
        SSEEnabled: true""", "      BillingMode: PAY_PER_REQUEST")
    ok, detail = run(write(tmp_path, body), "encryption_declared")
    assert not ok and "SSESpecification" in detail


def test_declared_encryption_passes_without_claiming_too_much(tmp_path):
    ok, detail = run(write(tmp_path, CLEAN), "encryption_declared")
    assert ok
    assert "not verified against a deployed resource" in detail


# ── The mechanism itself ─────────────────────────────────────────────────────

def test_sam_custom_tags_do_not_break_parsing(tmp_path):
    """!Ref and !GetAtt are rejected outright by SafeLoader. Every template in the wild
    uses them, so a checker that cannot read them checks nothing."""
    ok, _ = run(write(tmp_path, CLEAN), "iam_no_wildcards")
    assert ok


def test_a_malformed_template_fails_its_check_not_the_run(tmp_path):
    root = write(tmp_path, "Resources: [this is not: valid: yaml")
    ok, detail = run(root, "iam_no_wildcards")
    assert not ok
    assert "Error" in detail or "error" in detail


def test_check_ids_are_stable_across_calls(tmp_path):
    """An id that moves makes a result impossible to compare with the previous run."""
    root = write(tmp_path, CLEAN)
    assert [c.check_id for c in policy.plan_checks(root)] == \
           [c.check_id for c in policy.plan_checks(root)]


def test_a_project_with_no_iac_plans_nothing(tmp_path):
    """Most projects have no template. They must produce zero checks, not zero-of-zero
    passes that imply they were assessed."""
    (tmp_path / "main.py").write_text("x = 1")
    assert policy.plan_checks(tmp_path) == []


def test_no_root_is_not_an_error(tmp_path):
    assert policy.plan_checks(None) == []


# ── The per-resource pivot ───────────────────────────────────────────────────

def test_a_resource_no_control_reads_is_present_and_marked_unchecked():
    """The honesty requirement the whole per-resource view rests on.

    `aura-cloud-demo` declares an SNS::Topic, which NO control reads. Leaving it out of
    the view, or showing it as passing, would be Aura vouching for something it never
    examined — the same distinction the endpoint already draws for a project with no IaC.
    """
    from pathlib import Path
    from src.qatest import policy

    rows = policy.resource_view(Path("demo-project/aura-cloud-demo/backend"))
    topic = next((r for r in rows if r["name"] == "NotifyTopic"), None)
    assert topic is not None, "a resource nothing checks must still be listed"
    assert topic["type"] == "AWS::SNS::Topic"
    assert topic["applicable"] == 0
    assert topic["controls"] == []
    # And it sorts last, after everything that was actually assessed.
    assert rows[-1]["name"] == "NotifyTopic"


def test_each_resource_carries_only_the_controls_that_apply_to_it():
    from pathlib import Path
    from src.qatest import policy

    rows = {r["name"]: r for r in
            policy.resource_view(Path("demo-project/aura-lambda-demo"))}
    # Functions: IA-5(1), AC-6(1), SI-2. Stores: SC-13 alone.
    assert rows["AuditFunction"]["applicable"] == 3
    assert rows["PricingFunction"]["applicable"] == 3
    assert rows["CatalogTable"]["applicable"] == 1
    assert rows["MediaBucket"]["applicable"] == 1


def test_a_resource_compliant_on_one_control_shows_it():
    """The fact the old aggregate destroyed. PricingFunction resolves DB_PASSWORD from
    Secrets Manager — genuinely compliant on IA-5(1) — while failing AC-6(1)."""
    from pathlib import Path
    from src.qatest import policy

    rows = {r["name"]: r for r in
            policy.resource_view(Path("demo-project/aura-lambda-demo"))}
    pricing = {c["id"]: c for c in rows["PricingFunction"]["controls"]}
    assert pricing["lambda_env_no_secrets"]["ok"] is True
    assert pricing["iam_no_wildcards"]["ok"] is False
    assert rows["PricingFunction"]["passed"] == 2


def test_a_failing_finding_carries_a_remedy():
    """A finding that says what is wrong and not what to do is a chore, not a control."""
    from pathlib import Path
    from src.qatest import policy

    rows = policy.resource_view(Path("demo-project/aura-lambda-demo"))
    failing = [c for r in rows for c in r["controls"] if not c["ok"]]
    assert failing, "the demo template is authored to fail two controls"
    for c in failing:
        assert c["remedy"], f"{c['id']} gives no remedy"


def test_the_aggregate_reports_scale_not_a_bare_fail(tmp_path):
    """One offender among three used to read exactly like three offenders."""
    from src.qatest import policy

    template = """
Resources:
""" + "".join(f"""
  Fn{i}:
    Type: AWS::Serverless::Function
    Properties:
      Runtime: python3.12
      Environment:
        Variables:
          DB_PASSWORD: {'literal-secret' if i == 1 else "'{{resolve:secretsmanager:x:SecretString:p}}'"}
""" for i in (1, 2, 3))
    (tmp_path / "template.yaml").write_text(template)
    check = next(c for c in policy.plan_checks(tmp_path)
                 if c.validator == "lambda_env_no_secrets")
    ok, detail = policy.run_check(tmp_path, check)
    assert not ok
    assert "1 of 3" in detail, detail


def test_the_run_verdicts_are_unchanged_for_both_demos():
    """The refactor must not move a single case's outcome. Both real templates, all four
    controls — the cheapest possible guard against the pivot changing what a run says."""
    from pathlib import Path
    from src.qatest import policy

    expected = {"lambda_env_no_secrets": False, "iam_no_wildcards": False,
                "runtime_supported": True, "encryption_declared": True}
    for demo in ("demo-project/aura-lambda-demo",
                 "demo-project/aura-cloud-demo/backend"):
        root = Path(demo)
        for check in policy.plan_checks(root):
            ok, _ = policy.run_check(root, check)
            assert ok is expected[check.validator], f"{demo} {check.validator}"


def test_a_control_with_nothing_to_check_says_so(tmp_path):
    """Vacuously true, and it must not claim otherwise: "no resource of this kind" is a
    different fact from "all of them passed"."""
    from src.qatest import policy

    (tmp_path / "template.yaml").write_text(
        "Resources:\n  T:\n    Type: AWS::SNS::Topic\n    Properties: {}\n")
    check = next(c for c in policy.plan_checks(tmp_path)
                 if c.validator == "lambda_env_no_secrets")
    ok, detail = policy.run_check(tmp_path, check)
    assert ok
    assert "no resource of this kind" in detail

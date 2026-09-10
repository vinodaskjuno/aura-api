"""Guided migration: profiles, capability inference, and the stage machine.

The tests that matter here are the ones guarding claims the feature makes about
itself. "Any stack to any stack" is a claim; so is "Aura inferred your standards";
so is "the strategy tells you what CANNOT move". Each is easy to ship broken in a
way a demo would not reveal.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SKIP_BOOTSTRAP", "1")
os.environ.setdefault("NEO4J_ENABLED", "false")

from src.migration import capabilities as cap  # noqa: E402
from src.migration import session as sess  # noqa: E402
from src.migration.profiles import (  # noqa: E402
    GENERIC, WORKFUSION_TO_AIRFLOW, known_targets, profile_for,
)


# ── Profiles: is "any to any" real? ──────────────────────────────────────────

def test_a_curated_pair_returns_its_profile():
    p = profile_for("workfusion", "airflow")
    assert p is WORKFUSION_TO_AIRFLOW
    assert p.curated is True
    assert p.component_map["serviceTask"] == "PythonOperator"


def test_an_uncurated_pair_still_migrates():
    """The test that decides whether "any stack to any stack" is a fact or a slogan.

    A pair nobody has curated must fall back to the generic profile, not return None
    and not raise — otherwise the feature only does the pairs someone wrote down.
    """
    p = profile_for("cobol", "rust")
    assert p is GENERIC
    assert p.curated is False
    # And it still has something to ask, so the flow has a first step.
    assert p.questions


def test_source_and_target_matching_is_case_and_space_insensitive():
    assert profile_for("  WorkFusion ", "AIRFLOW") is WORKFUSION_TO_AIRFLOW


def test_the_target_list_is_not_limited_to_curated_pairs():
    """A dropdown showing only curated targets would make the product look narrower
    than it is — the generic path reaches all of these."""
    targets = known_targets()
    assert "airflow" in targets
    assert "step-functions" in targets
    assert len(targets) > 1


def test_a_curated_profile_names_what_cannot_move():
    """The risks are the half a customer actually scrutinises. A profile whose
    component_map is full and whose known_risks is empty is selling something."""
    assert WORKFUSION_TO_AIRFLOW.known_risks
    joined = " ".join(WORKFUSION_TO_AIRFLOW.known_risks).lower()
    # Attended bots and OCR/ML genuinely have no Airflow equivalent; if these stop
    # being called out, the profile has drifted into optimism.
    assert "attended" in joined
    assert "ocr" in joined or "cognitive" in joined


# ── Capability inference: is it honest? ──────────────────────────────────────

DEPS = [
    {"name": "splunk-sdk",        "ecosystem": "pypi", "repos": 12},
    {"name": "hvac",              "ecosystem": "pypi", "repos": 8},
    {"name": "autodynatrace",     "ecosystem": "pypi", "repos": 6},
    {"name": "boto3",             "ecosystem": "pypi", "repos": 14},
    {"name": "opentelemetry-sdk", "ecosystem": "pypi", "repos": 2},
]


@pytest.fixture
def graph(monkeypatch):
    """A graph holding a plausible estate: 14 repos with mixed tooling."""
    import src.graph.neo4j_client as nc

    def fake_query(q, params=None):
        if "count(DISTINCT r) AS total" in q:
            return [{"total": 14}]
        return DEPS

    monkeypatch.setattr(nc, "run_query", fake_query)


def test_a_widely_used_library_is_inferred_with_its_evidence(graph):
    found = {c["technology"]: c for c in cap.infer("p1")}
    splunk = found["Splunk"]
    assert splunk["capability"] == "logging"
    assert splunk["services"] == 12
    assert splunk["totalServices"] == 14
    assert splunk["evidence"], "a candidate with no evidence cannot be judged"


def test_boto3_alone_does_not_announce_a_standard(graph):
    """boto3 is in almost every Python service on AWS. Reading it as "they use
    Secrets Manager" would invent a standard from a dependency that means nothing
    on its own — and generated code would then target the wrong thing."""
    technologies = {c["technology"] for c in cap.infer("p1")}
    assert "AWS Secrets Manager" not in technologies
    assert "AWS S3" not in technologies
    assert "AWS SQS" not in technologies


def test_a_corroborating_hint_unlocks_the_ambiguous_signature(graph):
    found = cap.infer("p1", extra_evidence="arn:aws:secretsmanager:us-east-1:1234:secret:db")
    assert any(c["technology"] == "AWS Secrets Manager" for c in found)


def test_confidence_separates_a_standard_from_someone_experiment(graph):
    """12 of 14 is a standard. 2 of 14 is an experiment. If both read the same, the
    count on screen is decoration."""
    found = {c["technology"]: c for c in cap.infer("p1")}
    assert found["Splunk"]["confidence"] == "strong"          # 12/14
    assert found["HashiCorp Vault"]["confidence"] == "mixed"  # 8/14
    assert found["OpenTelemetry"]["confidence"] == "weak"     # 2/14


def test_inference_returns_empty_rather_than_raising_when_the_graph_is_down(monkeypatch):
    """An inference step that cannot run should leave an empty form, not a broken
    screen — the user can still fill it in by hand."""
    import src.graph.neo4j_client as nc
    monkeypatch.setattr(nc, "run_query",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no graph")))
    assert cap.infer("p1") == []


def test_every_capability_appears_in_the_default_mapping(graph):
    """A capability nothing was inferred for must still show as a row. A missing row
    is indistinguishable from one nobody thought about."""
    mapping = cap.default_mapping(cap.infer("p1"))
    assert {r["capability"] for r in mapping} == {c for c, _ in cap.CAPABILITIES}
    unset = [r for r in mapping if r["origin"] == "unset"]
    assert unset, "some capability should be unfilled by this estate"
    for row in mapping:
        assert row["capabilityLabel"]


# ── Stage machine ────────────────────────────────────────────────────────────

def _session(stage: str = "target", **extra) -> dict:
    return {"sessionId": "s1", "projectId": "p1", "stage": stage,
            "source": "workfusion", "target": "airflow", **extra}


def test_an_action_from_the_wrong_stage_is_refused_as_a_sequencing_error():
    with pytest.raises(sess.StageError) as err:
        sess.require_stage(_session("proposed"), "finalized")
    # The message has to tell the user what to do, not just that they were wrong.
    assert "finalized" in str(err.value)


def test_convert_before_finalize_is_refused():
    with pytest.raises(sess.StageError):
        sess.require_stage(_session("revised"), "finalized")
    sess.require_stage(_session("finalized"), "finalized")  # and permitted after


def test_a_session_cannot_move_backwards(monkeypatch):
    """Guards a real timing bug: a late poll response overwriting a later stage."""
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    with pytest.raises(sess.StageError):
        sess.save(_session("converted"), stage="proposed")


def test_failure_is_always_reachable(monkeypatch):
    """`failed` is the one stage reachable from anywhere — an error must be
    recordable however far along the session was."""
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    out = sess.save(_session("converting"), stage="failed")
    assert out["stage"] == "failed"


def test_switching_target_archives_the_strategy_rather_than_dropping_it(monkeypatch):
    """The only backwards move, and the only chat action that discards work. The old
    strategy is kept so an accidental switch is recoverable."""
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    before = _session("finalized", strategy={"summary": "airflow plan"},
                      mapping=[{"capability": "logging", "technology": "Splunk"}],
                      archivedStrategies=[])

    after = sess.reset_for_new_target(before, "step-functions")

    assert after["stage"] == "target"
    assert after["target"] == "step-functions"
    assert after["strategy"] == {}, "a plan for Airflow is not a plan for Step Functions"
    assert after["questions"] == []
    assert len(after["archivedStrategies"]) == 1
    assert after["archivedStrategies"][0]["target"] == "airflow"
    assert after["archivedStrategies"][0]["strategy"]["summary"] == "airflow plan"
    # The pair changed, so the profile must be re-resolved — Step Functions is not curated.
    assert after["curated"] is False


def test_mapping_records_where_each_choice_came_from(monkeypatch):
    """A strategy that cannot say why it targets Vault is one a reviewer will not
    sign. Rows the user changed are stamped; rows they left alone keep their origin."""
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    before = _session("architecture", mapping=[
        {"capability": "logging", "technology": "Splunk", "origin": "inferred"},
        {"capability": "secrets", "technology": "AWS Secrets Manager", "origin": "inferred"},
    ])

    after = sess.set_mapping(before, [
        {"capability": "logging", "technology": "Splunk"},                 # unchanged
        {"capability": "secrets", "technology": "HashiCorp Vault"},        # changed
    ], origin="chat")

    by_cap = {r["capability"]: r for r in after["mapping"]}
    assert by_cap["logging"]["origin"] == "inferred"
    assert by_cap["secrets"]["origin"] == "chat"


# ── Strategy normalisation: does the plan stay honest? ───────────────────────

from src.agents.migration_strategy_agent import (  # noqa: E402
    _looks_too_optimistic, _normalise,
)


def test_an_unrecognised_verdict_fails_safe_to_manual():
    """Guessing in the permissive direction is how something unportable gets
    silently converted. An unknown verdict must land on `manual`, never `migrate`."""
    out = _normalise({"components": [{"name": "ocr-bot", "verdict": "probably-fine"}]})
    assert out["components"][0]["verdict"] == "manual"


def test_effort_is_derived_when_the_model_omits_it():
    out = _normalise({"components": [
        {"name": "a", "verdict": "migrate"}, {"name": "b", "verdict": "migrate"},
        {"name": "c", "verdict": "drop"},    {"name": "d", "verdict": "manual"}]})
    assert out["effort"] == {"components": 4, "automatable": 2, "manual": 2}


def test_risks_given_as_bare_strings_are_still_usable():
    out = _normalise({"components": [], "risks": ["attended bots break"]})
    assert out["risks"] == [{"severity": "medium", "text": "attended bots break"}]


def test_a_strategy_with_no_risks_is_flagged_not_accepted():
    """The failure mode this guards: a report that only ever says yes. An empty risk
    list across a real component set is far more likely a weak run than an easy app."""
    components = [{"name": f"c{i}", "verdict": "migrate"} for i in range(6)]
    warning = _looks_too_optimistic({"components": components, "risks": []})
    assert warning and "incomplete" in warning.lower()


def test_a_strategy_where_nothing_needs_a_human_is_flagged():
    components = [{"name": f"c{i}", "verdict": "migrate"} for i in range(6)]
    warning = _looks_too_optimistic({"components": components,
                                     "risks": [{"severity": "high", "text": "x"}]})
    assert warning and "unusual" in warning.lower()


def test_a_strategy_naming_hard_parts_passes_the_check():
    components = [{"name": f"c{i}", "verdict": "migrate"} for i in range(6)]
    components.append({"name": "ocr", "verdict": "drop"})
    assert _looks_too_optimistic({"components": components,
                                  "risks": [{"severity": "high", "text": "x"}]}) == ""


# ── API contract ─────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient
    from src.main import app
    from src.routers.auth import get_current_user

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: {
        "username": "dev", "userId": "u1", "role": "admin",
        "permissions": ["dev_workspace"],
    }
    yield TestClient(app)
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


def test_profiles_offers_more_targets_than_curated_pairs(api):
    body = api.get("/api/migration/profiles").json()
    assert len(body["pairs"]) >= 1
    assert len(body["targets"]) > len(body["pairs"]), (
        "the generic path reaches targets nobody curated; the dropdown must show them")
    assert body["pairs"][0]["curated"] is True


def test_starting_a_migration_needs_a_target(api, monkeypatch):
    monkeypatch.setattr("src.database.dynamo_client.scan_items",
                        lambda *a, **k: [{"projectId": "p1", "name": "Claims"}])
    r = api.post("/api/migration/sessions",
                 json={"projectId": "p1", "source": "workfusion", "target": "  "})
    assert r.status_code == 400


def test_a_missing_session_is_404_not_an_empty_object(api, monkeypatch):
    monkeypatch.setattr("src.migration.session.get", lambda sid, pid: None)
    assert api.get("/api/migration/sessions/nope?projectId=p1").status_code == 404


def test_finalizing_before_a_strategy_exists_is_refused(api, monkeypatch):
    monkeypatch.setattr("src.migration.session.get", lambda sid, pid:
                        _session("proposed", strategy={}))
    r = api.post("/api/migration/sessions/s1/finalize?projectId=p1")
    assert r.status_code == 400
    assert "no strategy" in r.json()["detail"].lower()


def test_a_wrong_stage_is_a_conflict_not_a_bad_request(api, monkeypatch):
    """409, not 400: the request was well formed, the migration just is not there
    yet. A 400 sends the caller hunting for a bad field."""
    monkeypatch.setattr("src.migration.session.get", lambda sid, pid:
                        _session("target", strategy={}))
    r = api.post("/api/migration/sessions/s1/finalize?projectId=p1")
    assert r.status_code == 409
    assert "proposed" in r.json()["detail"] or "revised" in r.json()["detail"]


# ── Chat: propose, then confirm ──────────────────────────────────────────────
#
# The chat is the input; the mapping table is the state. What matters is that
# nothing applies without a decision, that a proposal naming something imaginary is
# refused, and that accepting one of three suggestions applies exactly one.

from src.migration import chat as mchat  # noqa: E402


def _chat_session(**extra) -> dict:
    return {
        "sessionId": "s1", "projectId": "p1", "stage": "proposed",
        "source": "workfusion", "target": "airflow",
        "mapping": [
            {"capability": "secrets", "capabilityLabel": "Secrets & credentials",
             "technology": "AWS Secrets Manager", "origin": "inferred"},
            {"capability": "logging", "capabilityLabel": "Logging",
             "technology": "stdout", "origin": "inferred"},
        ],
        "conversionShape": {"granularity": "one-per-process", "extractShared": True,
                            "repoLayout": "standard"},
        "components": [
            {"name": "ocr-bot", "verdict": "migrate"},
            {"name": "payment-flow", "verdict": "migrate"},
        ],
        "strategy": {"components": [
            {"name": "ocr-bot", "verdict": "migrate"},
            {"name": "payment-flow", "verdict": "migrate"},
        ]},
        **extra,
    }


def test_a_proposal_naming_an_imaginary_capability_is_refused():
    """A change applying to nothing is worse than a rejection — the user sees an
    accepted proposal that did not do anything, which looks like it worked."""
    ok, why = mchat._validate(
        {"kind": "mapping", "capability": "telepathy", "to": "Vault"}, _chat_session())
    assert not ok and "telepathy" in why


def test_a_proposal_naming_an_imaginary_component_is_refused():
    ok, why = mchat._validate(
        {"kind": "verdict", "component": "does-not-exist", "to": "drop"}, _chat_session())
    assert not ok


def test_a_verdict_outside_the_four_is_refused():
    ok, _ = mchat._validate(
        {"kind": "verdict", "component": "ocr-bot", "to": "maybe"}, _chat_session())
    assert not ok


def test_a_valid_mapping_proposal_describes_a_before_and_after():
    described = mchat._describe(
        {"kind": "mapping", "capability": "secrets", "to": "HashiCorp Vault",
         "reason": "we standardise on Vault"}, _chat_session())
    assert described["from"] == "AWS Secrets Manager"
    assert described["to"] == "HashiCorp Vault"
    assert described["destructive"] is False


def test_a_target_switch_is_marked_destructive_with_a_warning():
    """The one chat action that discards work. The UI must be able to warn."""
    described = mchat._describe(
        {"kind": "target", "to": "step-functions"}, _chat_session())
    assert described["destructive"] is True
    assert "discards" in described["warning"].lower()


def test_accepting_a_subset_applies_exactly_that_subset(monkeypatch):
    """The point of the confirmation step: a user who wants one of three swaps
    should not have to reject all three and retype."""
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    session = _chat_session()
    changes = [
        mchat._describe({"kind": "mapping", "capability": "secrets",
                         "to": "HashiCorp Vault"}, session),
        mchat._describe({"kind": "mapping", "capability": "logging",
                         "to": "Splunk"}, session),
    ]

    out = mchat.apply(session, changes, accept=[0])
    by_cap = {m["capability"]: m for m in out["mapping"]}
    assert by_cap["secrets"]["technology"] == "HashiCorp Vault"
    assert by_cap["secrets"]["origin"] == "chat"
    assert by_cap["logging"]["technology"] == "stdout", "index 1 was not accepted"


def test_rejecting_everything_changes_nothing(monkeypatch):
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    session = _chat_session()
    changes = [mchat._describe({"kind": "mapping", "capability": "secrets",
                                "to": "HashiCorp Vault"}, session)]
    out = mchat.apply(session, changes, accept=[])
    assert out is session


def test_a_verdict_override_updates_the_strategy_too(monkeypatch):
    """Otherwise the component table and the strategy disagree about a verdict the
    user just changed."""
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    session = _chat_session()
    changes = [mchat._describe({"kind": "verdict", "component": "ocr-bot",
                                "to": "drop"}, session)]
    out = mchat.apply(session, changes)

    assert {c["name"]: c["verdict"] for c in out["components"]}["ocr-bot"] == "drop"
    assert {c["name"]: c["verdict"] for c in out["strategy"]["components"]}["ocr-bot"] == "drop"


def test_a_shape_boolean_arrives_as_a_boolean(monkeypatch):
    """The model returns strings. `extractShared: "false"` is truthy in Python, so
    without coercion "turn off shared extraction" would turn it on."""
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    session = _chat_session()
    changes = [mchat._describe({"kind": "shape", "field": "extractShared",
                                "to": "false"}, session)]
    out = mchat.apply(session, changes)
    assert out["conversionShape"]["extractShared"] is False


def test_a_target_switch_applies_alone_and_discards_the_rest(monkeypatch):
    """Applying anything else in the same batch would write into state that is
    about to be discarded."""
    monkeypatch.setattr("src.database.dynamo_client.update_item", lambda *a, **k: {})
    session = _chat_session(archivedStrategies=[])
    changes = [
        mchat._describe({"kind": "mapping", "capability": "secrets",
                         "to": "HashiCorp Vault"}, session),
        mchat._describe({"kind": "target", "to": "step-functions"}, session),
    ]
    out = mchat.apply(session, changes)

    assert out["stage"] == "target"
    assert out["target"] == "step-functions"
    assert out["strategy"] == {}
    assert len(out["archivedStrategies"]) == 1


# ── Conversion ───────────────────────────────────────────────────────────────
#
# The claims to guard: a `drop` verdict is not converted, one bad component does not
# abandon the rest, an interrupted run resumes, the manifest reports what was NOT
# done, and the confirmed standards actually reach the prompt.

from src.migration import convert  # noqa: E402


def _convert_session(**extra) -> dict:
    return {
        "sessionId": "s1", "projectId": "p1", "projectName": "Claims",
        "stage": "finalized", "source": "workfusion", "target": "airflow",
        "mapping": [
            {"capability": "secrets", "capabilityLabel": "Secrets & credentials",
             "technology": "HashiCorp Vault", "origin": "chat"},
            {"capability": "logging", "capabilityLabel": "Logging",
             "technology": "Splunk", "origin": "inferred"},
        ],
        "conversionShape": {"granularity": "one-per-process", "extractShared": True},
        "strategy": {
            "summary": "Port six processes, drop the OCR bot.",
            "risks": [{"severity": "high", "text": "attended bots cannot port"}],
            "components": [
                {"name": "payment-flow", "verdict": "migrate"},
                {"name": "ocr-bot", "verdict": "drop"},
                {"name": "approval", "verdict": "manual"},
            ],
        },
        **extra,
    }


@pytest.mark.asyncio
async def test_a_dropped_component_is_never_sent_for_conversion():
    """`drop` means it should not exist on the target. Converting it anyway would
    put back exactly what the strategy said to leave behind — and cost a model call
    to do it."""
    out = await convert.convert_one({"name": "ocr-bot", "verdict": "drop"},
                                    _convert_session())
    assert out["status"] == "skipped"
    assert out["files"] == []
    assert "drop" in " ".join(out["notes"]).lower()


def test_the_confirmed_standards_reach_the_conversion_prompt():
    """If the user said Vault, generated code must be asked for Vault. This is the
    one thing that makes the whole architecture step worth having."""
    prompt = convert._prompt({"name": "payment-flow", "verdict": "migrate"},
                             _convert_session(), source_text="<mule/>")
    assert "HashiCorp Vault" in prompt
    assert "Splunk" in prompt
    assert "CONFIRMED STANDARDS" in prompt


def test_a_manual_component_is_told_not_to_invent_logic():
    prompt = convert._prompt({"name": "approval", "verdict": "manual"},
                             _convert_session(), source_text="")
    assert "do not invent" in prompt.lower()


def test_colliding_filenames_are_kept_apart_not_overwritten(monkeypatch):
    """Two components generating dags/main.py would silently lose one. Suffixing
    keeps both and makes the collision visible in the zip."""
    written: dict[str, str] = {}
    monkeypatch.setattr("src.storage.s3_client.put_object",
                        lambda bucket, key, body, ct=None: written.update(key=key) or key)

    import io as _io, zipfile as _zip
    captured: dict = {}
    real_zip = _zip.ZipFile

    class Spy(real_zip):
        def writestr(self, name, data, *a, **k):
            captured.setdefault("names", []).append(name)
            return super().writestr(name, data, *a, **k)

    monkeypatch.setattr(_zip, "ZipFile", Spy)
    convert.package(_convert_session(), [
        {"component": "a", "status": "converted",
         "files": [{"filename": "dags/main.py", "content": "# a"}], "notes": [], "todos": []},
        {"component": "b", "status": "converted",
         "files": [{"filename": "dags/main.py", "content": "# b"}], "notes": [], "todos": []},
    ])
    names = [n for n in captured["names"] if n.endswith(".py")]
    assert len(names) == 2 and len(set(names)) == 2, names


def test_the_manifest_reports_what_was_not_done():
    """A folder of generated code with no account of what was skipped or failed is
    not reviewable, and reviewable is what was promised."""
    md = convert._manifest(_convert_session(), [
        {"component": "payment-flow", "status": "converted", "files": [{}],
         "notes": [], "todos": ["Confirm the retry window"]},
        {"component": "ocr-bot", "status": "skipped", "files": [],
         "notes": ["Marked `drop` in the strategy."], "todos": []},
        {"component": "approval", "status": "failed", "files": [],
         "notes": ["Conversion failed: timeout"], "todos": []},
    ])

    assert "Deliberately not converted" in md and "ocr-bot" in md
    assert "Needs attention" in md and "approval" in md
    assert "Decisions left to you" in md and "retry window" in md
    # The standards, with where each came from — the audit trail a reviewer wants.
    assert "HashiCorp Vault" in md and "asked for in chat" in md
    # And it must not oversell itself.
    assert "not runnable" in md.lower()
    assert "attended bots cannot port" in md


def test_the_manifest_counts_are_honest():
    md = convert._manifest(_convert_session(), [
        {"component": "a", "status": "converted", "files": [{}], "notes": [], "todos": []},
        {"component": "b", "status": "skipped", "files": [], "notes": [], "todos": []},
        {"component": "c", "status": "failed", "files": [], "notes": [], "todos": []},
        {"component": "d", "status": "empty", "files": [], "notes": [], "todos": []},
    ])
    # `empty` counts as needing attention, not as converted — a component that
    # produced no files has not been migrated whatever the model said.
    assert "Converted: 1" in md
    assert "Skipped: 1" in md
    assert "Needs attention: 2" in md


# ── The demo fixture ─────────────────────────────────────────────────────────
#
# The fixture and the curated profile have to agree. If the fixture grows an element
# the profile does not map, the demo silently shows an unmapped component — and if
# the profile is corrected against a real WorkFusion export, this catches a fixture
# that was not corrected with it.

import glob  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
from pathlib import Path  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "demo-project" / "workfusion-claims"
BPMN_NS = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
WF_NS = "{http://www.workfusion.com/schema/bpmn/extensions}"

# Children and attributes rather than components — a `documentation` element is not
# something you migrate, so the profile correctly has no mapping for it.
NOT_COMPONENTS = {
    # `definitions` is the document root and `process` its container; the rest are
    # child elements or attributes rather than things you migrate.
    "definitions", "process",
    "documentation", "script", "conditionExpression", "timeCycle",
}


def _fixture_files() -> list[str]:
    return sorted(glob.glob(str(FIXTURE / "**" / "*.bpmn"), recursive=True))


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_every_fixture_process_is_well_formed_xml():
    files = _fixture_files()
    assert len(files) >= 4, "the fixture should carry several processes"
    for path in files:
        root = ET.parse(path).getroot()   # raises on malformed XML
        assert root.find(f"{BPMN_NS}process") is not None, path


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_the_profile_maps_every_component_the_fixture_uses():
    """Ties fixture and profile together. A demo that shows an unmapped component
    undermines the exact claim it is there to make."""
    types: set[str] = set()
    for path in _fixture_files():
        for el in ET.parse(path).getroot().iter():
            types.add(el.tag.replace(BPMN_NS, ""))
            wf_type = el.get(f"{WF_NS}taskType")
            if wf_type:
                types.add(wf_type)

    unmapped = sorted(t for t in types - NOT_COMPONENTS
                      if t not in WORKFUSION_TO_AIRFLOW.component_map)
    assert not unmapped, f"fixture uses component types the profile does not map: {unmapped}"


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_the_fixture_contains_things_that_cannot_be_migrated():
    """The fixture's job is to make a migration report tell the truth. If everything
    in it ports cleanly, a report saying so is correct and proves nothing."""
    all_xml = "\n".join(Path(p).read_text() for p in _fixture_files())
    assert 'taskType="ocrTask"' in all_xml, "no OCR task — nothing with no equivalent"
    assert 'taskType="mlTask"' in all_xml, "no ML task"
    assert "userTask" in all_xml, "no attended step — the hardest case is missing"


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_the_fixture_gives_capability_inference_something_to_find():
    """An empty architecture step is a bad demo of an inference feature."""
    deps = (FIXTURE / "requirements.txt").read_text().lower()
    for package in ("hvac", "splunk-sdk", "autodynatrace"):
        assert package in deps, f"{package} missing — inference would find nothing"


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_the_fixture_says_plainly_that_it_is_not_a_real_export():
    """Guards against this being mistaken for a customer artifact once the real
    export exists and this one is still lying around."""
    readme = (FIXTURE / "README.md").read_text().lower()
    assert "provisional" in readme
    assert "not" in readme and "export" in readme


# ── Source inventory ─────────────────────────────────────────────────────────
#
# The gap that made the first real run useless: the strategy agent saw only the
# knowledge graph, and for a platform with no parser the graph holds no components —
# so it correctly refused to list any and asked for file paths instead. These guard
# the fix and the honest fallback.

from src.migration import source as msource  # noqa: E402


@pytest.fixture
def fixture_project(monkeypatch):
    monkeypatch.setattr("src.database.dynamo_client.scan_items",
                        lambda *a, **k: [{"projectId": "p1", "clonedPath": str(FIXTURE)}])


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_the_inventory_finds_the_processes_the_graph_cannot_see(fixture_project):
    inv = msource.inventory("p1", WORKFUSION_TO_AIRFLOW.detect)
    assert inv["byExtension"].get(".bpmn") == 4
    assert inv["byExtension"].get(".groovy") == 3
    paths = " ".join(e["path"] for e in inv["excerpts"])
    for process in ("claims-intake", "claims-adjudication", "payout-processing",
                    "fraud-screening"):
        assert process in paths, f"{process} never reaches the prompt"


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_profile_matching_files_are_excerpted_before_the_readme(fixture_project):
    """The excerpt budget is small. Spent on README.md instead of the process
    definitions, the agent still cannot see the application."""
    inv = msource.inventory("p1", WORKFUSION_TO_AIRFLOW.detect)
    order = [e["path"] for e in inv["excerpts"]]
    bpmn_at = min(i for i, p in enumerate(order) if p.endswith(".bpmn"))
    readme_at = next((i for i, p in enumerate(order) if p.endswith("README.md")), 999)
    assert bpmn_at < readme_at


def test_no_working_copy_returns_empty_rather_than_raising(monkeypatch):
    """The strategy must still run off the graph alone — degraded, not broken."""
    monkeypatch.setattr("src.database.dynamo_client.scan_items",
                        lambda *a, **k: [{"projectId": "p1", "clonedPath": "/nope"}])
    assert msource.inventory("p1") == {}


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_build_artifacts_and_binaries_are_not_read(fixture_project, tmp_path):
    inv = msource.inventory("p1", WORKFUSION_TO_AIRFLOW.detect)
    listed = " ".join(inv["files"])
    for junk in ("node_modules", "__pycache__", "/target/", ".pyc"):
        assert junk not in listed


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_the_prompt_tells_the_agent_the_files_are_the_application(fixture_project):
    """Without this the agent has the files and still asks for file paths."""
    from src.agents.migration_strategy_agent import MigrationStrategyAgent
    from src.agents.base_agent import AgentContext

    inv = msource.inventory("p1", WORKFUSION_TO_AIRFLOW.detect)
    ctx = AgentContext(user_id="u", username="dev", role="admin",
                       intent="migrate", project_id="p1",
                       extra={"source": "workfusion", "target": "airflow",
                              "facts": inv})
    prompt = MigrationStrategyAgent()._build_prompt(ctx, WORKFUSION_TO_AIRFLOW)

    assert "claims-intake.bpmn" in prompt
    assert "ocrTask" in prompt, "the excerpt content itself must be present"
    assert "Do not ask for file paths" in prompt


def test_an_empty_inventory_says_so_instead_of_implying_an_empty_application():
    """Left unsaid, the agent concludes the application is empty and returns no
    components with no explanation — which is what a user sees as 'nothing happened'."""
    from src.agents.migration_strategy_agent import MigrationStrategyAgent
    from src.agents.base_agent import AgentContext

    ctx = AgentContext(user_id="u", username="dev", role="admin", intent="migrate",
                       project_id="p1",
                       extra={"source": "workfusion", "target": "airflow", "facts": {}})
    prompt = MigrationStrategyAgent()._build_prompt(ctx, WORKFUSION_TO_AIRFLOW)

    assert "NO SOURCE FILES WERE AVAILABLE" in prompt
    assert "do not invent components" in prompt.lower()

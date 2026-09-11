"""Structural checks for projects that do not serve HTTP.

The point of these is the FAILING cases. A validator that only ever passes is
decoration, and the defects below — a flow pointing at nothing, a script task naming
a class nobody wrote, a DAG file that defines no DAG — are exactly what a migration
introduces and what nobody notices until production.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.qatest import structure

FIXTURE = Path(__file__).resolve().parents[2] / "demo-project" / "workfusion-claims"


def _check(tmp_path: Path, filename: str, body: str, validator: str):
    (tmp_path / filename).write_text(body)
    chk = structure.Check(check_id="c1", name=filename, rel_path=filename,
                          validator=validator)
    return structure.run_check(tmp_path, chk)


BPMN = """<?xml version="1.0"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="p1" name="P">
    <startEvent id="s"/>
    <sequenceFlow id="f1" sourceRef="s" targetRef="{target}"/>
    <scriptTask id="t" scriptFormat="groovy"><script>{script}</script></scriptTask>
  </process>
</definitions>"""


# ── BPMN ─────────────────────────────────────────────────────────────────────

def test_a_flow_pointing_at_nothing_fails(tmp_path):
    """The single most common thing a hand-edited migration gets wrong."""
    ok, detail = _check(tmp_path, "p.bpmn", BPMN.format(target="ghost", script="X.y()"),
                        "bpmn_flows_resolve")
    assert not ok
    assert "ghost" in detail and "unresolved" in detail


def test_a_flow_that_resolves_passes_and_says_how_many(tmp_path):
    ok, detail = _check(tmp_path, "p.bpmn", BPMN.format(target="t", script="X.y()"),
                        "bpmn_flows_resolve")
    assert ok and "1 sequence flow" in detail


def test_malformed_bpmn_is_reported_as_malformed(tmp_path):
    ok, detail = _check(tmp_path, "p.bpmn", "<definitions><process></definitions>",
                        "bpmn_parses")
    assert not ok and "well-formed" in detail


def test_xml_that_is_not_bpmn_is_not_called_bpmn(tmp_path):
    """Otherwise any XML in the repo is reported as a broken process."""
    ok, detail = _check(tmp_path, "p.bpmn", "<config><a/></config>", "bpmn_parses")
    assert not ok and "not a BPMN definition" in detail


def test_a_script_task_naming_a_class_nobody_wrote_fails(tmp_path):
    ok, detail = _check(tmp_path, "p.bpmn",
                        BPMN.format(target="t", script="Missing.run(execution)"),
                        "bpmn_scripts_exist")
    assert not ok and "Missing" in detail


def test_a_script_task_resolves_when_the_groovy_file_exists(tmp_path):
    (tmp_path / "Present.groovy").write_text("class Present {}")
    ok, detail = _check(tmp_path, "p.bpmn",
                        BPMN.format(target="t", script="Present.run(execution)"),
                        "bpmn_scripts_exist")
    assert ok and "1 script task" in detail


# ── Groovy ───────────────────────────────────────────────────────────────────

def test_unbalanced_groovy_fails(tmp_path):
    ok, detail = _check(tmp_path, "A.groovy", "class A { def f() { return 1 }",
                        "groovy_balanced")
    assert not ok and "unclosed" in detail


def test_brackets_inside_strings_and_comments_are_not_counted(tmp_path):
    """A naive counter fails every real file — `"}"` and `// {` are everywhere."""
    body = '''
    // a stray { in a comment
    /* and a } in a block */
    class A {
      def f() { return "a } brace" + 'another {' }
    }
    '''
    ok, _ = _check(tmp_path, "A.groovy", body, "groovy_balanced")
    assert ok


def test_a_groovy_pass_does_not_claim_to_have_compiled(tmp_path):
    """Compiling needs a JVM. A green tick that implies more than it checked is worse
    than no check."""
    ok, detail = _check(tmp_path, "A.groovy", "class A {}", "groovy_balanced")
    assert ok and "not compiled" in detail


# ── Airflow DAGs — the migration's own output ────────────────────────────────

DAG_OK = """
from airflow import DAG
from airflow.operators.python import PythonOperator
with DAG("claims") as dag:
    a = PythonOperator(task_id="intake", python_callable=lambda: None)
    b = PythonOperator(task_id="adjudicate", python_callable=lambda: None)
"""


def test_a_dag_file_that_defines_no_dag_fails(tmp_path):
    """Airflow ignores it silently — the worst way for a migration to fail, because
    nothing errors and the pipeline is simply absent."""
    body = "from airflow.operators.python import PythonOperator\nx = 1\n"
    ok, detail = _check(tmp_path, "d.py", body, "dag_defines_a_dag")
    assert not ok and "silently" in detail


def test_duplicate_task_ids_fail(tmp_path):
    body = DAG_OK.replace('task_id="adjudicate"', 'task_id="intake"')
    ok, detail = _check(tmp_path, "d.py", body, "dag_defines_a_dag")
    assert not ok and "intake" in detail


def test_a_valid_dag_reports_its_tasks(tmp_path):
    ok, detail = _check(tmp_path, "d.py", DAG_OK, "dag_defines_a_dag")
    assert ok and "2 unique task id" in detail


def test_a_dag_with_a_syntax_error_fails_with_the_line(tmp_path):
    ok, detail = _check(tmp_path, "d.py", "from airflow import DAG\ndef (:\n",
                        "dag_parses")
    assert not ok and "line 2" in detail


def test_only_airflow_files_are_planned_as_dags(tmp_path):
    """A plain module reported as a broken DAG would be a false positive on every
    Python project."""
    (tmp_path / "plain.py").write_text("x = 1\n")
    (tmp_path / "dag.py").write_text(DAG_OK)
    planned = {c.rel_path for c in structure.plan_checks(tmp_path)}
    assert "dag.py" in planned and "plain.py" not in planned


# ── Planning ─────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_the_workfusion_fixture_plans_and_passes():
    """The project QA Mind previously called `unavailable` and could say nothing about."""
    checks = structure.plan_checks(FIXTURE)
    assert len(checks) >= 12
    results = [structure.run_check(FIXTURE, c) for c in checks]
    failed = [(c.name, d) for c, (ok, d) in zip(checks, results) if not ok]
    assert not failed, f"the fixture should be structurally sound: {failed}"


def test_dependency_directories_are_never_planned(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.xml").write_text("<a/>")
    (tmp_path / "real.xml").write_text("<a/>")
    assert {c.rel_path for c in structure.plan_checks(tmp_path)} == {"real.xml"}


def test_a_missing_file_fails_rather_than_raising(tmp_path):
    chk = structure.Check("c", "gone", "gone.bpmn", "bpmn_parses")
    ok, detail = structure.run_check(tmp_path, chk)
    assert not ok and "does not exist" in detail


def test_an_unknown_validator_fails_loudly(tmp_path):
    (tmp_path / "a.xml").write_text("<a/>")
    ok, detail = structure.run_check(tmp_path, structure.Check("c", "n", "a.xml", "nope"))
    assert not ok and "no validator" in detail


# ── The run pipeline ────────────────────────────────────────────────────────

@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_a_project_with_no_server_is_planned_and_is_not_unavailable(monkeypatch):
    """The whole point. A BPMN/Groovy project has no ASGI module and no npm dev
    script, so every run of it reported `unavailable` and said nothing about the
    application at all."""
    from src.qatest import plan, runner
    from src.qatest.types import Report

    cases = plan.build_plan("p1", {"apis": [], "services": [], "dependencies": []},
                            root=FIXTURE)
    assert cases, "nothing was planned for a project with no HTTP surface"
    structural = [c for c in cases if c.kind == "structure"]
    assert len(structural) >= 12

    # Only the root case needs a browser; the rest do not.
    report = Report(run_id="r", project_id="p1", app_url="")
    rec = runner._Recorder("p1", "r", total=len(structural))
    runner._run_structure(rec, structural, FIXTURE)

    assert len(rec.steps) == len(structural), "every check must report exactly once"
    assert all(s.status == "passed" for s in rec.steps)
    # A green tick has to be able to say what it checked.
    assert any("sequence flow" in s.action for s in rec.steps)


@pytest.mark.skipif(not FIXTURE.exists(), reason="demo fixture not present")
def test_a_broken_process_is_reported_as_a_failure_not_as_unavailable(tmp_path):
    """The difference that matters: `unavailable` says we could not look,
    `failed` says we looked and it is wrong."""
    from src.qatest import plan, runner

    (tmp_path / "broken.bpmn").write_text(
        '<?xml version="1.0"?>'
        '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">'
        '<process id="p"><startEvent id="s"/>'
        '<sequenceFlow id="f" sourceRef="s" targetRef="nowhere"/>'
        '</process></definitions>')

    cases = [c for c in plan.build_plan("p1", {"apis": [], "services": [],
                                               "dependencies": []}, root=tmp_path)
             if c.kind == "structure"]
    rec = runner._Recorder("p1", "r", total=len(cases))
    runner._run_structure(rec, cases, tmp_path)

    failed = [s for s in rec.steps if s.status == "failed"]
    assert failed, "a dangling sequence flow must fail"
    assert "nowhere" in failed[0].error


def test_a_structure_case_is_never_marked_unrunnable():
    """`skip_reason` is what makes a case skip. A file check needs nothing started,
    so it must always execute."""
    from src.qatest import plan
    from src.qatest.types import Case

    case = Case(case_id="s1", kind="structure", name="x.bpmn — parses",
                path="x.bpmn", method="bpmn_parses")
    assert plan.why_unrunnable(case) == ""


def test_structure_is_a_selectable_kind():
    from src.qatest import plan
    assert "structure" in plan.ALL_KINDS


def test_a_dag_folder_file_that_defines_no_dag_is_still_checked(tmp_path):
    """The regression that mattered: the detector required `DAG(` in the file, so the
    one defect it exists to find — a converted file that defines no DAG — was skipped
    for not looking like a DAG."""
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "payout.py").write_text(
        "from airflow.operators.python import PythonOperator\nx = 1\n")

    planned = [c for c in structure.plan_checks(tmp_path)
               if c.rel_path.endswith("payout.py")]
    assert planned, "a file Airflow will load was not checked"

    failing = [structure.run_check(tmp_path, c) for c in planned]
    assert any(not ok and "silently" in detail for ok, detail in failing)


def test_a_plain_module_outside_dags_is_still_left_alone(tmp_path):
    """Widening the detector must not put a false failure on every Python project."""
    (tmp_path / "util.py").write_text("def add(a, b):\n    return a + b\n")
    assert not [c for c in structure.plan_checks(tmp_path)
                if c.rel_path.endswith("util.py")]

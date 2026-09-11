"""Can this machine run a test, and does it say the right thing when it cannot.

Two properties carry most of the weight here. The doctor must never be the thing that
crashes — it is what someone runs when something is already wrong — and the shallow
path must never launch a browser, because it is on the state-report path that runs
every fifteen seconds.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.qatest import doctor, toolpath

REPO = Path(__file__).resolve().parents[3]   # the workspace root


@pytest.fixture(autouse=True)
def _fresh():
    doctor.invalidate()
    doctor._launch = None
    yield
    doctor.invalidate()


# ── Real captured output, not invented ───────────────────────────────────────
# From `podman machine list --format json` on a working Mac.

MACHINE_RUNNING = """[{"Name":"podman-machine-default","Default":true,"Running":true,
 "Starting":false,"VMType":"applehv","CPUs":4,"Memory":"8589934592",
 "DiskSize":"64424509440"}]"""
MACHINE_STOPPED = MACHINE_RUNNING.replace('"Running":true', '"Running":false')
MACHINE_SMALL = MACHINE_RUNNING.replace('"CPUs":4', '"CPUs":2').replace(
    '"Memory":"8589934592"', '"Memory":"2147483648"')


def _podman(monkeypatch, *, path="/opt/podman/bin/podman", ready=(True, ""),
            machines=MACHINE_RUNNING, version="5.7.0"):
    from src.qatest import emulators

    monkeypatch.setattr(emulators, "podman_path", lambda: path)
    monkeypatch.setattr(emulators, "podman_ready", lambda: ready)

    def fake_run(args, timeout=None):
        if args[:2] == ["machine", "list"]:
            return (0, machines) if machines is not None else (1, "boom")
        if args[0] == "version":
            return 0, version
        return 0, ""
    monkeypatch.setattr(emulators, "_run", fake_run)


# ── The machine states — bug 2 ───────────────────────────────────────────────

def test_a_stopped_machine_is_blocking_and_names_the_command(monkeypatch):
    _podman(monkeypatch, ready=(False, "Cannot connect to Podman"),
            machines=MACHINE_STOPPED)
    monkeypatch.setattr(toolpath, "platform_name", lambda: "darwin")

    found = doctor.diagnose().find("podman.working")
    assert not found.ok and found.severity == doctor.BLOCKS
    assert "not running" in found.title
    assert found.remedy == ("podman machine start",)


def test_no_machine_at_all_says_init_not_start(monkeypatch):
    """Different problem, different command — and init downloads several GB, which
    the detail has to warn about before anyone agrees to it."""
    _podman(monkeypatch, ready=(False, "no such machine"), machines="[]")
    monkeypatch.setattr(toolpath, "platform_name", lambda: "darwin")

    found = doctor.diagnose().find("podman.working")
    assert found.remedy == (doctor.MACHINE_INIT,)
    assert "GB" in found.detail


def test_an_undersized_machine_degrades_but_does_not_block(monkeypatch):
    _podman(monkeypatch, machines=MACHINE_SMALL)
    monkeypatch.setattr(toolpath, "platform_name", lambda: "darwin")

    found = doctor.diagnose().find("podman.machine.size")
    assert not found.ok and found.severity == doctor.DEGRADES
    # Print-only: resizing stops the machine and recreating one destroys its images.
    assert found.fixable is False


def test_unreadable_machine_output_does_not_raise(monkeypatch):
    for bad in (None, "not json", '{"not": "a list"}'):
        doctor.invalidate()
        _podman(monkeypatch, ready=(False, "unusable"), machines=bad)
        monkeypatch.setattr(toolpath, "platform_name", lambda: "darwin")
        found = doctor.diagnose().find("podman.working")
        assert not found.ok and found.remedy


def test_linux_has_no_machine_and_says_something_useful_instead(monkeypatch):
    _podman(monkeypatch, ready=(False, "permission denied"))
    monkeypatch.setattr(toolpath, "platform_name", lambda: "linux")

    diag = doctor.diagnose()
    assert diag.find("podman.machine.size") is None      # no VM on Linux
    assert "subuid" in " ".join(diag.find("podman.working").remedy)


# ── Cost — this is what stops the fix becoming a performance bug ─────────────

def test_a_shallow_diagnosis_never_launches_a_browser():
    """`_machine_state` runs this every 15s and `/capabilities` on every page load.
    Launching Chromium there would be 1-3s and a real process each time.

    Asserted on the contract rather than with a sentinel: the shallow path reports
    `browser.binary` (a file that exists) and produces no `browser.launch` finding at
    all unless a real run already reported one.
    """
    shallow = doctor.diagnose(deep=False)
    assert shallow.find("browser.binary") is not None
    assert shallow.find("browser.launch") is None

    doctor.invalidate()
    deep = doctor.diagnose(deep=True)
    assert deep.find("browser.launch") is not None


def test_the_result_is_cached_within_its_ttl(monkeypatch):
    calls = {"n": 0}
    real = doctor._check_disk

    def counted(deep):
        calls["n"] += 1
        return real(deep)
    monkeypatch.setattr(doctor, "_check_disk", counted)

    doctor.diagnose(ttl_s=60)
    doctor.diagnose(ttl_s=60)
    assert calls["n"] == 1


def test_a_zero_ttl_re_runs(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(doctor, "_check_disk",
                        lambda deep: calls.__setitem__("n", calls["n"] + 1) or [])
    doctor.diagnose(ttl_s=0)
    doctor.diagnose(ttl_s=0)
    assert calls["n"] == 2


def test_nothing_installed_makes_no_subprocess_calls(monkeypatch):
    """The Fargate shape. `/capabilities` imports this module on a machine with no
    podman and no browser; it must not shell out at all."""
    import subprocess
    monkeypatch.setattr(toolpath, "which", lambda name: None)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytest.fail("the doctor shelled out"))
    from src.qatest import emulators, runner
    monkeypatch.setattr(emulators, "podman_path", lambda: None)
    monkeypatch.setattr(runner, "_playwright_available", lambda: (False, "absent"))

    diag = doctor.diagnose(deep=False)
    assert not diag.ok
    assert diag.find("podman.present").remedy


# ── A failing check is a Finding, never an exception ─────────────────────────

def test_a_check_that_explodes_becomes_a_finding(monkeypatch):
    def explode(_deep):
        raise RuntimeError("the disk melted")
    monkeypatch.setattr(doctor, "_check_disk", explode)

    diag = doctor.diagnose()
    found = next(f for f in diag.findings if "could not run" in f.title)
    assert "the disk melted" in found.detail
    assert found.severity == doctor.INFO          # never blocks on our own bug


# ── Platform tables ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("platform", ["darwin", "linux", "windows"])
def test_every_platform_offers_a_command_for_a_missing_podman(platform, monkeypatch):
    """This proves table lookup and string composition. It proves NOTHING about
    whether the commands work — in particular the Windows winget id is unverified,
    and a green test here is not Windows support."""
    monkeypatch.setattr(toolpath, "platform_name", lambda: platform)
    monkeypatch.setattr(toolpath, "which", lambda name: "/usr/bin/apt-get"
                        if name == "apt-get" else None)

    remedy = doctor._podman_install_remedy()
    assert remedy and all(isinstance(r, str) and r for r in remedy)
    if platform == "darwin":
        assert any("brew install podman" in r for r in remedy)
    if platform == "windows":
        assert any("winget" in r for r in remedy)
    if platform == "linux":
        assert any("apt-get" in r for r in remedy)


def test_windows_says_its_commands_are_unverified(monkeypatch):
    monkeypatch.setattr(toolpath, "platform_name", lambda: "windows")
    assert "provisional" in doctor.platform_caveat()
    monkeypatch.setattr(toolpath, "platform_name", lambda: "darwin")
    assert doctor.platform_caveat() == ""


def test_macos_compose_recommends_the_binary_not_the_python_package(monkeypatch):
    """podman 5.x probes for a `docker-compose` BINARY. `pip install podman-compose`
    is what the docs said and it does not satisfy podman — verified on a real Mac."""
    monkeypatch.setattr(toolpath, "platform_name", lambda: "darwin")
    remedy = doctor._compose_remedy()
    assert remedy == ("brew install docker-compose",)


def test_remedies_use_the_running_interpreter_not_a_bare_python(monkeypatch):
    """A bare `python` is whichever one the shell finds, which is routinely not the
    venv the agent is running in."""
    from src.qatest import runner
    monkeypatch.setattr(runner, "_playwright_available", lambda: (False, "absent"))
    found = doctor._check_browser(False)[0]
    assert all(r.startswith(doctor._py()) for r in found.remedy)


@pytest.mark.skipif(not (REPO / "aura-infra" / "DEPLOYMENT.md").exists(),
                    reason="sibling aura-infra repo not present")
def test_the_machine_init_line_matches_the_deployment_doc():
    """Two different recommended machine sizes is worse than either one."""
    body = (REPO / "aura-infra" / "DEPLOYMENT.md").read_text()
    assert doctor.MACHINE_INIT in body


# ── The reported shape ───────────────────────────────────────────────────────

def test_only_failures_are_reported_and_they_are_capped():
    """Every runner's findings live in ONE DynamoDB item under a 400 KB limit."""
    diag = doctor.Diagnosis(platform="linux", arch="x86_64", findings=[
        doctor.Finding(f"c{i}", False, doctor.BLOCKS, f"t{i}") for i in range(20)
    ] + [doctor.Finding("fine", True, doctor.BLOCKS, "ok")])

    payload = diag.as_dict()
    assert len(payload["findings"]) == 6
    assert all(not f["ok"] for f in payload["findings"])
    assert payload["ok"] is False


def test_info_findings_are_not_reported():
    diag = doctor.Diagnosis(platform="linux", arch="x86_64", findings=[
        doctor.Finding("noisy", False, doctor.INFO, "just context")])
    assert diag.as_dict()["findings"] == []
    assert diag.ok is True          # info never blocks


def test_the_printed_lines_are_ascii_only():
    """cp1252 stdout on Windows raises UnicodeEncodeError on ✓, which would turn a
    diagnostic into a crash on the platform that most needs one."""
    diag = doctor.Diagnosis(platform="windows", arch="AMD64", findings=[
        doctor.Finding("a", True, doctor.BLOCKS, "fine"),
        doctor.Finding("b", False, doctor.BLOCKS, "broken"),
        doctor.Finding("c", False, doctor.DEGRADES, "partial")])
    text = "\n".join(diag.lines())
    text.encode("cp1252")           # raises if anything is not encodable
    assert "[ok]" in text and "[!!]" in text and "[--]" in text


def test_a_recorded_launch_failure_surfaces_without_relaunching():
    """A real run launches Chromium anyway, so its outcome is free information."""
    doctor.record_launch(False, "libnss3.so missing")
    found = doctor.diagnose(deep=False).find("browser.launch")
    if found is not None:           # only when the package and binary are present
        assert not found.ok and "libnss3" in found.detail


# ── The CLI ──────────────────────────────────────────────────────────────────
#
# The important property is not what these print, it is what they do NOT require and
# do NOT run: diagnosing a machine must work before a key exists, and --setup must
# never execute anything a person has not agreed to.

def test_doctor_needs_neither_api_nor_key(monkeypatch, capsys):
    """A user whose runner will not start has to be able to ask why before they have
    a URL or a gateway key."""
    from src.qatest import agent

    monkeypatch.setattr(agent.sys, "argv", ["agent"])
    code = agent.main(["--doctor"])
    out = capsys.readouterr().out
    assert code in (0, 1)                    # 0 ready, 1 blocked — never a usage error
    assert "QualityMind runner" in out
    assert "[ok]" in out or "[!!]" in out


def test_doctor_json_is_machine_readable(capsys):
    import json as _json
    from src.qatest import agent

    agent.main(["--doctor", "--json"])
    payload = _json.loads(capsys.readouterr().out)
    assert "findings" in payload and "platform" in payload
    assert isinstance(payload["ok"], bool)


def test_running_the_agent_still_requires_api(capsys):
    """Making --api optional for --doctor must not make it optional for a real run."""
    from src.qatest import agent

    with pytest.raises(SystemExit):
        agent.main(["--key", "gw-x"])


def test_setup_dry_run_executes_nothing(monkeypatch):
    """The sentinel goes on `_run_steps`, not on `subprocess.run`: the DIAGNOSIS
    legitimately shells out (podman info, machine list), so a global patch fails for
    the wrong reason and proves nothing about --dry-run."""
    from src.qatest import setup

    monkeypatch.setattr(setup, "_run_steps",
                        lambda *a, **k: pytest.fail("--dry-run executed a command"))
    monkeypatch.setattr(setup, "_ask",
                        lambda _p: pytest.fail("--dry-run prompted"))
    assert setup.run_setup(dry_run=True) in (0, 1)


def test_setup_defaults_to_no(monkeypatch):
    """An empty answer is not consent. This is the whole safety argument."""
    from src.qatest import setup

    monkeypatch.setattr("builtins.input", lambda _p: "")
    assert setup._ask("? ") == "n"
    monkeypatch.setattr("builtins.input", lambda _p: "  Y ")
    assert setup._ask("? ") == "y"


def test_setup_treats_an_interrupt_as_quit(monkeypatch):
    from src.qatest import setup

    def interrupt(_p):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupt)
    assert setup._ask("? ") == "q"


def test_setup_never_runs_sudo_itself(monkeypatch, capsys):
    """Aura must not be the thing that asked for your password."""
    import subprocess as sp
    from src.qatest import setup

    ran = []
    monkeypatch.setattr(sp, "run", lambda cmd, **k: ran.append(cmd) or
                        type("R", (), {"returncode": 0})())
    monkeypatch.setattr(setup, "_ask", lambda _p: "")

    reporter = setup._Reporter("", "", "test")
    setup._run_steps(("sudo apt-get install -y podman", "echo safe"), reporter)

    assert not any("sudo" in str(c) for c in ran), "Aura invoked sudo"
    assert "echo safe" in " ".join(str(c) for c in ran)


def test_setup_reports_nothing_when_not_given_a_key():
    """--api/--key are optional and outbound-only. Without them it is silent."""
    from src.qatest import setup

    assert setup._Reporter("", "", "n").enabled is False
    assert setup._Reporter("http://x", "", "n").enabled is False


def test_a_server_that_returns_commands_cannot_make_setup_run_them(monkeypatch):
    """The server may WATCH an install. It may not steer one.

    Behavioural rather than reading the source: a stub server answers every report
    with a command to run, and nothing must execute it.
    """
    from src.qatest import setup

    class _Client:
        def __init__(self, *_a, **_k):
            pass

        def post(self, *_a, **_k):
            return type("R", (), {
                "json": lambda _s: {"commands": [{"id": "c1", "kind": "install",
                                                  "container": "anything"}]},
                "status_code": 200})()

        def close(self):
            pass

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(setup, "_run_steps",
                        lambda *a, **k: pytest.fail("a server command was executed"))
    monkeypatch.setattr(setup, "_ask", lambda _p: "n")

    reporter = setup._Reporter("http://server", "gw-key", "laptop")
    assert reporter.enabled
    reporter.send(step="x", index=1, total=1)     # the response is simply not read


def test_the_setup_log_is_bounded():
    """It is resent in full on every report, and every runner's state shares one
    DynamoDB item."""
    from src.qatest import setup

    reporter = setup._Reporter("", "", "n")
    for i in range(200):
        reporter.say(f"line {i}")
    assert len(reporter.log) == setup.LOG_KEEP
    assert reporter.log[-1]["text"] == "line 199"        # the tail survives


def test_windows_setup_is_print_only(monkeypatch):
    """Detection is safe to ship untested; executing an unverified installer is not."""
    from src.qatest import setup

    monkeypatch.setattr(toolpath, "platform_name", lambda: "windows")
    monkeypatch.setattr(setup, "_run_steps",
                        lambda *a, **k: pytest.fail("Windows setup executed a command"))
    monkeypatch.setattr(setup, "_ask",
                        lambda _p: pytest.fail("Windows setup prompted to run something"))
    setup.run_setup()

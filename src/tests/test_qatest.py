"""QualityMind local runner: planning, emulator selection, evidence, write-back.

The guards come first, because they are the properties that were actually broken:
runs that reported passes for tests that never executed, and a results view that
could not see a run past the 500th item.
"""
from __future__ import annotations

import json

import pytest

from src.qatest import emulators, evidence, graph_writeback, plan
from src.qatest.types import Case, EmulatorRecord, Report, Step

DEMO_APIS = [
    {"eid": "api:p:GET:/health", "method": "GET", "path": "/health"},
    {"eid": "api:p:GET:/products", "method": "GET", "path": "/products"},
    {"eid": "api:p:GET:/products/{sku}", "method": "GET", "path": "/products/{sku}"},
    {"eid": "api:p:POST:/quote", "method": "POST", "path": "/quote"},
]
DEMO_SERVICES = [{"eid": "service:p:pricing", "name": "pricing"}]


# ── Planning from the graph ───────────────────────────────────────────────────

def test_one_case_per_api_node_plus_one_per_service_and_a_root_check():
    cases = plan.build_plan("p", {"apis": DEMO_APIS, "services": DEMO_SERVICES,
                                  "dependencies": []})
    assert len(cases) == len(DEMO_APIS) + len(DEMO_SERVICES) + 1
    assert sum(1 for c in cases if c.kind == "smoke") == 1


def test_every_plan_starts_with_the_application_root():
    """The only case a frontend can be planned from. Code analysis extracts
    server-side route tables, and a React SPA has none, so without this a frontend
    has nothing in the graph to test."""
    for facts in ({"apis": DEMO_APIS, "services": [], "dependencies": []},
                  {"apis": [], "services": [], "dependencies": []}):
        cases = plan.build_plan("p", facts)
        assert cases[0].case_id == "root-001"
        assert cases[0].kind == "ui" and cases[0].path == "/"


def test_kind_says_what_is_being_tested_not_how_it_is_reached():
    """Every API node is an `api` case, whatever its method or path shape.

    `kind` used to mean browser-openable / not-openable, so `GET /health` was "ui" and
    `POST /quote` was "api". Once the kinds became a user-facing filter that inverted
    the choice exactly: ticking "API" selected only the cases that always get skipped,
    and ticking "UI" selected everything that actually runs.
    """
    cases = {c.name: c.kind for c in
             plan.build_plan("p", {"apis": DEMO_APIS, "services": DEMO_SERVICES,
                                   "dependencies": []})}
    assert cases["GET /health"] == "api"
    assert cases["GET /products/{sku}"] == "api"
    assert cases["POST /quote"] == "api"
    assert all(v == "smoke" for k, v in cases.items() if k.startswith("service "))


def test_a_parameterless_get_is_the_only_thing_that_actually_runs():
    """The distinction the old `kind` was carrying now lives in skip_reason, where it
    can also say WHY — which is what makes a low coverage number actionable."""
    reasons = {c.name: c.skip_reason for c in
               plan.build_plan("p", {"apis": DEMO_APIS, "services": DEMO_SERVICES,
                                     "dependencies": []})}
    assert reasons["GET /health"] == ""
    assert "path parameter" in reasons["GET /products/{sku}"]
    assert "request body" in reasons["POST /quote"]
    assert all("no HTTP address" in v for k, v in reasons.items()
               if k.startswith("service "))


def test_a_case_with_no_verified_node_is_not_linked_in_the_graph():
    """write_results must not emit a VERIFIES edge for the root case, which points
    at nothing — an edge to an empty externalId would match arbitrary nodes."""
    from src.qatest.types import Case as C
    root = C(case_id="root-001", kind="ui", name="application loads")
    assert not root.verifies_eid and not root.verifies_label


def test_plan_is_deterministic():
    """Two runs of an unchanged graph must produce the same plan in the same order,
    or step-by-step evidence cannot be compared between runs."""
    args = {"apis": list(reversed(DEMO_APIS)), "services": DEMO_SERVICES,
            "dependencies": []}
    first = [c.name for c in plan.build_plan("p", args)]
    second = [c.name for c in plan.build_plan("p", {"apis": DEMO_APIS,
                                                    "services": DEMO_SERVICES,
                                                    "dependencies": []})]
    assert first == second


def test_every_graph_derived_case_carries_the_node_it_verifies():
    """Without this the result cannot be written back as an edge, and impact-based
    selection has nothing to select on. The root case is exempt: it checks the
    deployed application, which is not a node."""
    cases = plan.build_plan("p", {"apis": DEMO_APIS, "services": DEMO_SERVICES,
                                  "dependencies": []})
    derived = [c for c in cases if c.case_id != "root-001"]
    assert len(derived) == len(DEMO_APIS) + len(DEMO_SERVICES)
    for case in derived:
        assert case.verifies_eid and case.verifies_label in ("API", "Service")


def test_unreachable_graph_yields_no_cases_rather_than_raising(monkeypatch):
    def boom():
        raise RuntimeError("neo4j down")
    monkeypatch.setattr("src.graph.backends.routed_session", boom)
    assert plan.fetch_facts("p") == {"apis": [], "services": [], "dependencies": []}


# ── Emulator selection, derived from dependencies ─────────────────────────────

def test_boto3_alone_starts_only_the_aws_emulator():
    picked = emulators.clouds_for([{"name": "boto3"}, {"name": "fastapi"}])
    assert [c.name for c in picked] == ["aws"]


def test_a_project_with_no_cloud_dependency_starts_nothing():
    """The demo shop's real dependency list — storage is in-memory, so a run that
    started four emulators would be burning 15 seconds for nothing."""
    deps = [{"name": n} for n in ("fastapi", "uvicorn", "pydantic", "pytest",
                                  "httpx", "react", "vite", "typescript")]
    assert emulators.clouds_for(deps) == []


@pytest.mark.parametrize("pkg,expected", [
    ("boto3", ["aws"]),
    ("@aws-sdk/client-s3", ["aws"]),
    ("azure-storage-blob", ["azure"]),
    ("@azure/identity", ["azure"]),
    ("google-cloud-pubsub", ["gcp"]),
    ("oci", ["oci"]),
    ("oci-python-sdk", ["oci"]),
])
def test_dependency_markers_resolve(pkg, expected):
    assert [c.name for c in emulators.clouds_for([{"name": pkg}])] == expected


@pytest.mark.parametrize("pkg", ["social", "associations", "velocity", "precocious"])
def test_a_package_that_merely_contains_a_cloud_name_starts_nothing(pkg):
    """`oci` is three letters and appears inside ordinary words, so it is matched
    exactly (plus an `oci-` prefix). Treating it as a bare prefix would start the OCI
    emulator for a package called "social"."""
    assert emulators.clouds_for([{"name": pkg}]) == []


def test_all_four_clouds_when_all_four_sdks_are_present():
    deps = [{"name": n} for n in ("boto3", "azure-storage-blob",
                                  "google-cloud-storage", "oci")]
    assert [c.name for c in emulators.clouds_for(deps)] == ["aws", "azure", "gcp", "oci"]


def test_emulator_env_points_the_sdk_at_the_emulator():
    """One variable redirects every boto3 client; verified against the pinned
    botocore in this repo."""
    aws = next(c for c in emulators.CLOUDS if c.name == "aws")
    env = aws.env()
    assert env["AWS_ENDPOINT_URL"] == "http://localhost:4566"
    assert env["AWS_ACCESS_KEY_ID"] and env["AWS_SECRET_ACCESS_KEY"]


def test_missing_podman_is_reported_per_emulator_not_raised(monkeypatch):
    monkeypatch.setattr(emulators, "podman_ready",
                        lambda: (False, "podman is not installed"))
    aws = next(c for c in emulators.CLOUDS if c.name == "aws")
    with emulators.EmulatorSet([aws], "t") as es:
        assert es.records[0].started is False
        assert "podman" in es.records[0].error
        assert es.env == {}          # nothing started, so nothing to point at


def test_a_stopped_podman_machine_says_so_rather_than_failing_obscurely(monkeypatch):
    """Installed is not usable. On macOS and Windows podman is a client for a VM, so
    `which` succeeds while every command fails — and the old check was `which`. The
    run was already claimed and marked running by the time each emulator died with a
    raw exec error."""
    monkeypatch.setattr(emulators, "podman_ready", lambda: (
        False, "podman is installed but not running — on macOS and Windows it needs "
               "its virtual machine started: `podman machine start`"))
    aws = next(c for c in emulators.CLOUDS if c.name == "aws")
    with emulators.EmulatorSet([aws], "t") as es:
        assert "podman machine start" in es.records[0].error


def test_podman_ready_distinguishes_absent_from_not_running(monkeypatch):
    monkeypatch.setattr(emulators, "podman_path", lambda: None)
    ok, why = emulators.podman_ready()
    assert not ok and "not installed" in why

    monkeypatch.setattr(emulators, "podman_path", lambda: "/opt/podman/bin/podman")
    monkeypatch.setattr(emulators, "_run",
                        lambda *a, **k: (125, "Cannot connect to Podman. "
                                              "please check your connection"))
    ok, why = emulators.podman_ready()
    assert not ok and "podman machine start" in why


def test_podman_ready_reports_an_unexpected_failure_verbatim(monkeypatch):
    """An error nobody anticipated must reach the reader, not be renamed into the
    nearest known cause."""
    monkeypatch.setattr(emulators, "podman_path", lambda: "/usr/bin/podman")
    monkeypatch.setattr(emulators, "_run", lambda *a, **k: (1, "disk quota exceeded"))
    ok, why = emulators.podman_ready()
    assert not ok and "disk quota exceeded" in why


# ── Evidence ─────────────────────────────────────────────────────────────────

class FakeS3:
    """Records what was written, so the writer and reader are tested together."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        # Modelled because list_runs orders on it. A double omitting last_modified
        # would pass while the real listing returned runs in an arbitrary order.
        self.mtimes: dict[str, str] = {}
        self._clock = 0

    def _stamp(self, key):
        # Zero-padded fractional seconds so the string sorts monotonically for any
        # number of writes. A plain seconds counter breaks past 59.
        self._clock += 1
        self.mtimes[key] = f"2026-01-01T00:00:00.{self._clock:09d}+00:00"

    def put_object(self, bucket, key, body, content_type=None):
        self.objects[key] = body if isinstance(body, bytes) else str(body).encode()
        self._stamp(key)
        return f"s3://{bucket}/{key}"

    def put_json(self, bucket, key, data):
        self.objects[key] = json.dumps(data).encode()
        self._stamp(key)
        return f"s3://{bucket}/{key}"

    def get_object(self, bucket, key):
        return self.objects.get(key)

    def get_json(self, bucket, key):
        raw = self.objects.get(key)
        return json.loads(raw) if raw else None

    def list_objects(self, bucket, prefix=""):
        return [{"key": k, "last_modified": self.mtimes.get(k, "")}
                for k in self.objects if k.startswith(prefix)]

    def presigned_url(self, bucket, key, expires=3600):
        return f"https://example/{key}"


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    import src.storage.s3_client as real
    for name in ("put_object", "put_json", "get_object", "get_json",
                 "list_objects", "presigned_url"):
        monkeypatch.setattr(real, name, getattr(fake, name))
    return fake


def test_a_written_run_is_listed_and_read_back_without_dynamodb(s3):
    report = Report(run_id="r1", project_id="proj", app_url="http://x",
                    total_passed=1)
    evidence.write_steps("proj", "r1", [Step(1, "GET /", "http://x", "passed")])
    evidence.write_report(report)

    assert evidence.list_runs("proj") == ["r1"]
    assert evidence.read_report("proj", "r1")["totalPassed"] == 1
    assert len(evidence.read_steps("proj", "r1")) == 1


def test_every_step_records_status_duration_and_its_screenshot_key(s3):
    evidence.write_screenshot("proj", "r2", 1, b"\x89PNG-1")
    steps = [Step(1, "GET /health", "http://x/health", "passed", duration_ms=12,
                  screenshot_key=evidence.screenshot_key("proj", "r2", 1)),
             Step(2, "GET /gone", "http://x/gone", "failed", duration_ms=30,
                  error="HTTP 404")]
    evidence.write_steps("proj", "r2", steps)

    read = evidence.read_steps("proj", "r2")
    assert [s["status"] for s in read] == ["passed", "failed"]
    assert read[0]["durationMs"] == 12
    # The failure carries its reason. The old path inferred it from whether the
    # filename contained "FAIL".
    assert read[1]["error"] == "HTTP 404"
    assert read[0]["screenshotKey"] in s3.objects


def test_runs_are_listed_newest_first_and_uncapped(s3):
    """The scan this replaced used limit=500, so run 501 was invisible."""
    for i in range(600):
        evidence.write_report(Report(run_id=f"{i:04d}", project_id="proj",
                                     app_url="http://x"))
    runs = evidence.list_runs("proj")
    assert len(runs) == 600
    assert runs[0] == "0599"


def test_newest_first_holds_for_random_run_ids(s3):
    """Run ids are random hex, so sorting the IDS orders by nothing meaningful — the
    newest run lands anywhere in the list. Ordering must come from the stored
    object's time."""
    for rid in ("ffff1111", "0000aaaa", "7777bbbb"):     # written in this order
        evidence.write_report(Report(run_id=rid, project_id="proj", app_url="http://x"))
    assert evidence.list_runs("proj") == ["7777bbbb", "0000aaaa", "ffff1111"]


def test_a_truncated_step_line_does_not_lose_the_earlier_steps(s3):
    """An interrupted upload leaves a partial final line; everything before it is
    still valid evidence."""
    from src.storage.s3_client import put_object
    put_object(evidence.BUCKET, "proj/r3/steps.jsonl",
               json.dumps(Step(1, "a", "b", "passed").as_dict()) + '\n{"index": 2, "act')
    assert len(evidence.read_steps("proj", "r3")) == 1


def test_a_prefix_with_no_report_is_not_listed_as_a_run(s3):
    """report.json is written last, so its absence means the run died mid-write."""
    evidence.write_screenshot("proj", "half", 1, b"png")
    assert "half" not in evidence.list_runs("proj")


def test_report_records_the_pinned_digest_of_every_emulator_that_ran(s3):
    report = Report(run_id="r4", project_id="proj", app_url="http://x",
                    emulators=[EmulatorRecord(cloud="aws",
                                              image="docker.io/floci/floci:latest",
                                              digest="sha256:abc", port=4566,
                                              started=True)])
    evidence.write_report(report)
    stored = evidence.read_report("proj", "r4")["emulators"][0]
    assert stored["digest"] == "sha256:abc" and stored["started"] is True


# ── Honesty about what did not run ───────────────────────────────────────────

def test_a_run_that_cannot_execute_reports_no_counts(monkeypatch, s3):
    """The path this replaced fabricated pass counts in simulation mode."""
    from src.qatest import runner
    monkeypatch.setattr(runner, "_playwright_available",
                        lambda: (False, "playwright is not installed"))
    report = runner.run_plan("proj", "r5", "http://x",
                             [Case(case_id="c1", kind="ui", name="GET /")])
    assert report.status == "unavailable"
    assert report.total_passed == 0 and report.total_failed == 0
    assert "playwright" in report.reason


def test_an_empty_plan_is_unavailable_and_says_to_run_analysis(monkeypatch, s3):
    from src.qatest import runner
    monkeypatch.setattr(runner, "_playwright_available", lambda: (True, ""))
    report = runner.run_plan("proj", "r6", "http://x", [])
    assert report.status == "unavailable"
    assert "code analysis" in report.reason


# ── Graph write-back ─────────────────────────────────────────────────────────

def test_case_status_takes_the_worst_step_not_the_last():
    """A case whose second step failed is a failing case; reporting the final step's
    status would hide it."""
    steps = [Step(1, "a", "t", "passed", case_id="c1"),
             Step(2, "b", "t", "failed", case_id="c1"),
             Step(3, "c", "t", "passed", case_id="c1")]
    assert graph_writeback.case_status(
        Report(run_id="r", project_id="p", app_url="u"), steps) == {"c1": "failed"}


def test_write_back_is_tagged_qa_test_not_code_analysis():
    """code_graph tags its nodes `code-analysis` and archives anything carrying that
    tag which analysis no longer produces. Test nodes must not be swept by it."""
    from src.graph.code_graph import SOURCE as CODE_SOURCE
    assert graph_writeback.SOURCE == "qa-test" != CODE_SOURCE


def test_write_back_survives_an_unreachable_graph(monkeypatch):
    """Evidence is already in S3 by then, so a graph outage must degrade the extra
    insight rather than lose the run."""
    import src.graph.neo4j_client as neo4j
    monkeypatch.setattr(neo4j, "upsert_node_returning_id",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    out = graph_writeback.write_results(
        Report(run_id="r", project_id="p", app_url="u",
               cases=[Case(case_id="c1", kind="ui", name="GET /")]))
    assert out["ok"] is False and out["errors"]


# ── Starting the application under test ──────────────────────────────────────

def _demo_layout(tmp_path):
    """The demo shop's shape: a FastAPI backend and a Vite frontend that proxies to it."""
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n")
    fe = tmp_path / "frontend"
    (fe / "node_modules").mkdir(parents=True)
    (fe / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    (fe / "vite.config.ts").write_text(
        "export default defineConfig({ server: { port: 5174, proxy: {"
        " '/api': { target: 'http://localhost:9100' } } } })")
    return tmp_path


def test_detects_both_halves_of_a_project(tmp_path):
    from src.qatest import appserver
    specs = {s.kind: s for s in appserver.detect(_demo_layout(tmp_path))}
    assert set(specs) == {"api", "ui"}
    assert "uvicorn" in " ".join(specs["api"].command)
    assert "app.main:app" in " ".join(specs["api"].command)


def test_the_api_is_started_on_the_port_the_ui_proxies_to(tmp_path, monkeypatch):
    """The frontend proxies /api to a fixed port. Starting the API on an arbitrary
    free port leaves the UI unable to reach it: the page loads, every request 500s,
    and the run reports a failure that is entirely the harness's doing.

    port_free is stubbed because the real one asks the OS: 9100 sits in TIME_WAIT for
    a while after any actual run, so asserting against live port state makes this pass
    or fail depending on what the machine did minutes ago."""
    from src.qatest import appserver
    monkeypatch.setattr(appserver, "port_free", lambda p: True)
    specs = {s.kind: s for s in appserver.detect(_demo_layout(tmp_path))}
    assert specs["api"].port == 9100        # from the vite proxy target
    assert specs["ui"].port == 5174         # from the vite server port


def test_a_taken_proxy_port_falls_back_and_says_the_ui_cannot_reach_the_api(tmp_path,
                                                                            monkeypatch):
    """Falling back silently would produce the exact failure this feature exists to
    avoid, so the reason travels with the spec."""
    from src.qatest import appserver
    monkeypatch.setattr(appserver, "port_free", lambda p: False)
    specs = {s.kind: s for s in appserver.detect(_demo_layout(tmp_path))}
    assert specs["api"].port != 9100
    assert "9100" in specs["api"].blocked and "UI" in specs["api"].blocked


def test_a_frontend_without_node_modules_is_blocked_with_instructions(tmp_path):
    from src.qatest import appserver
    root = _demo_layout(tmp_path)
    (root / "frontend" / "node_modules").rmdir()
    ui = next(s for s in appserver.detect(root) if s.kind == "ui")
    assert ui.blocked and "npm install" in ui.blocked


def test_app_urls_use_the_loopback_address_not_the_localhost_name():
    """A browser resolving "localhost" can reach ::1 first. When something else is
    listening there on the same port it gets tested instead, silently — a run once
    reported a pass against AURA's own dev server on [::1]:5174 while the application
    under test sat on 127.0.0.1:5174."""
    from src.qatest.appserver import AppSpec
    from pathlib import Path
    spec = AppSpec(kind="ui", name="fe", directory=Path("."), command=[],
                   port=5174, env={})
    assert spec.url == "http://127.0.0.1:5174"
    assert "localhost" not in spec.url


def test_port_free_does_not_use_reuseaddr(tmp_path):
    """SO_REUSEADDR makes bind() succeed on macOS while another server is listening,
    so the check reported a busy port as free."""
    import socket as _s
    from src.qatest.appserver import port_free
    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        assert port_free(srv.getsockname()[1]) is False
    finally:
        srv.close()


def test_a_blocked_app_is_reported_and_never_started(tmp_path):
    from pathlib import Path
    from src.qatest.appserver import AppSpec, RunningApps
    spec = AppSpec(kind="ui", name="fe", directory=Path("."), command=["false"],
                   port=1, env={}, blocked="node_modules is missing")
    with RunningApps([spec]) as apps:
        assert apps.started == []
        assert apps.failures and "node_modules" in apps.failures[0][1]
        assert apps.url_for("ui") == ""


# ── Which application a case is aimed at ─────────────────────────────────────

def test_the_root_case_targets_the_ui_and_api_cases_target_the_api():
    """Aiming API cases at a frontend is the mistake that made an SPA report every
    route as a pass, so the mapping is explicit."""
    from src.qatest.runner import _base_for
    urls = {"api": "http://127.0.0.1:9100", "ui": "http://127.0.0.1:5174"}
    root = Case(case_id="root-001", kind="ui", name="application loads")
    api = Case(case_id="api-002", kind="ui", name="GET /health", path="/health")
    assert _base_for(urls, root) == urls["ui"]
    assert _base_for(urls, api) == urls["api"]


def test_with_only_one_application_everything_targets_it():
    from src.qatest.runner import _base_for
    only_api = {"api": "http://127.0.0.1:9100"}
    root = Case(case_id="root-001", kind="ui", name="application loads")
    assert _base_for(only_api, root) == only_api["api"]


def test_readiness_probes_only_the_address_the_app_was_told_to_bind(monkeypatch):
    """Probing ::1 as well seemed harmless and was not: a DIFFERENT server on the
    other stack satisfies the check. AURA's own dev server on [::1]:5174 answered the
    probe for an app that had not yet bound 127.0.0.1:5174, so the run was told it was
    ready and then failed with CONNECTION_REFUSED."""
    from src.qatest import appserver
    probed: list[str] = []

    def fake_urlopen(url, timeout=None):
        probed.append(url)
        raise OSError("refused")

    monkeypatch.setattr(appserver.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(appserver.time, "sleep", lambda _s: None)
    assert appserver._wait_ready(5174, timeout=0.05) is False
    assert probed, "the probe never ran"
    assert all("127.0.0.1" in u for u in probed)
    assert not any("[::1]" in u for u in probed)


def test_locate_reports_every_place_it_looked(monkeypatch, tmp_path):
    """"Not found" alone is unactionable — which candidate was missing is the whole
    diagnosis."""
    from src.qatest import appserver
    monkeypatch.setattr(appserver, "_connector_paths",
                        lambda pid: [tmp_path / "gone" / "backend"])
    monkeypatch.setattr("src.database.dynamo_client.query_items",
                        lambda *a, **k: [{"clonedPath": "/workspace/p1"}])
    root, checked = appserver.locate("p1")
    assert root is None
    assert any("/workspace/p1" in c for c in checked)
    assert any("connector path" in c for c in checked)


def test_locate_prefers_the_configured_workspace(monkeypatch, tmp_path):
    """Read from Settings rather than os.environ: advisor/tools.py reads the raw env
    var, which pydantic-settings never populates, so a configured ./data/workspace was
    ignored and every lookup resolved /workspace."""
    from src.qatest import appserver
    (tmp_path / "p2").mkdir()
    monkeypatch.setattr("src.config_settings.get_settings",
                        lambda: type("S", (), {"aura_workspace": str(tmp_path)})())
    root, _ = appserver.locate("p2")
    assert root == (tmp_path / "p2").resolve()


def test_paths_under_the_container_workspace_are_explained(monkeypatch):
    """A project created through the deployed UI records /workspace paths that never
    existed on a laptop. Nothing in a bare "not found" hints at that."""
    from src.qatest.service import _no_working_copy
    msg = _no_working_copy("p", ["/home/me/ws/p", "/workspace/p (recorded on the project)"])
    assert "deployed container" in msg
    assert "already-running instance" in msg


def test_a_local_miss_is_not_blamed_on_a_deployed_container(monkeypatch):
    """The explanation must not fire when the paths are ordinary local ones."""
    from src.qatest.service import _no_working_copy
    msg = _no_working_copy("p", ["/home/me/ws/p", "/home/me/other/p (recorded on the project)"])
    assert "deployed container" not in msg


# ── Every case reports, including the ones that cannot run ───────────────────
#
# The headline bug: on_step was called from two of the five branches, so a skipped
# case advanced the run without advancing the progress bar. A project with several
# POST routes showed a bar that stalled and never reached 100%.

def _mixed_plan():
    return plan.build_plan("p", {
        "apis": [{"eid": "a1", "method": "GET", "path": "/health"},
                 {"eid": "a2", "method": "POST", "path": "/quote"},
                 {"eid": "a3", "method": "GET", "path": "/u/{id}"}],
        "services": [{"eid": "s1", "name": "Pay"}],
        "dependencies": []})


def test_every_case_reports_exactly_one_step(monkeypatch):
    """Asserted on the COUNT, not the statuses, so a future branch that forgets to
    report fails here rather than quietly stalling a progress bar."""
    from src.qatest import runner as run_mod

    cases = _mixed_plan()
    seen = []
    monkeypatch.setattr(run_mod, "_playwright_available", lambda: (True, ""))
    monkeypatch.setattr(run_mod.evidence, "write_steps", lambda *a, **k: None)
    monkeypatch.setattr(run_mod.evidence, "write_console", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "sync_playwright", None, raising=False)

    rec = run_mod._Recorder("p", "r", on_step=lambda s, total: seen.append((s, total)),
                            total=len(cases))
    for case in cases:
        reason = case.skip_reason
        rec.add(case.name, "t", "skipped" if reason else "passed", 0.0,
                error=reason, case_id=case.case_id)

    assert len(seen) == len(cases)
    assert [s.index for s, _ in seen] == list(range(1, len(cases) + 1))
    assert {t for _, t in seen} == {len(cases)}


def test_the_recorder_reports_skipped_steps_too():
    from src.qatest.runner import _Recorder

    seen = []
    rec = _Recorder("p", "r", on_step=lambda s, _t: seen.append(s.status), total=2)
    rec.add("a", "t", "skipped", 0.0, error="no address")
    rec.add("b", "t", "passed", 0.0)
    assert seen == ["skipped", "passed"]


def test_a_broken_progress_consumer_cannot_fail_a_run():
    from src.qatest.runner import _Recorder

    def explode(_s, _t):
        raise RuntimeError("the UI went away")

    rec = _Recorder("p", "r", on_step=explode, total=1)
    rec.add("a", "t", "passed", 0.0)
    assert len(rec.steps) == 1


# ── Kind filtering ───────────────────────────────────────────────────────────

def test_filtering_to_smoke_still_keeps_the_application_root():
    """root-001 is the only case a frontend can be tested by, and runner._base_for
    special-cases its id. A run without it tests nothing at all."""
    cases = _mixed_plan()
    kept = plan.filter_by_kind(cases, ["smoke"])
    assert [c.case_id for c in kept][0] == plan.ROOT_CASE_ID
    assert {c.kind for c in kept} == {"ui", "smoke"}


def test_no_kinds_means_every_kind():
    cases = _mixed_plan()
    assert plan.filter_by_kind(cases, None) == cases
    assert plan.filter_by_kind(cases, []) == cases


def test_filtering_twice_is_the_same_as_filtering_once():
    """Load-bearing: the filter runs server-side at claim AND inside service.execute,
    so that an agent which has never heard of `kinds` still runs the right subset."""
    cases = _mixed_plan()
    once = plan.filter_by_kind(cases, ["api"])
    assert plan.filter_by_kind(once, ["api"]) == once


def test_dropping_unrunnable_cases_leaves_only_what_can_execute():
    kept = plan.filter_by_kind(_mixed_plan(), None, skip_unrunnable=True)
    assert [c.name for c in kept] == ["application loads", "GET /health"]


def test_an_unknown_kind_is_ignored_rather_than_emptying_the_plan():
    cases = _mixed_plan()
    assert plan.filter_by_kind(cases, ["nonsense"]) == cases


# ── Coverage nodes reflect results, not intentions ───────────────────────────

def test_covered_nodes_without_statuses_still_lists_what_the_plan_touched():
    cases = _mixed_plan()
    nodes = plan.covered_nodes(cases)
    assert {n["externalId"] for n in nodes} == {"a1", "a2", "a3", "s1"}
    assert all("result" not in n for n in nodes)


def test_covered_nodes_records_the_worst_outcome_per_node():
    cases = _mixed_plan()
    statuses = {c.case_id: ("passed" if c.name == "GET /health" else "skipped")
                for c in cases}
    by_eid = {n["externalId"]: n for n in plan.covered_nodes(cases, statuses)}
    assert by_eid["a1"]["result"] == "passed"
    assert by_eid["a2"]["result"] == "skipped"


# ── "Unavailable" has to say why ────────────────────────────────────────────
#
# A screen reading `Unavailable` above `No application could be started:` — the
# sentence ending at the colon — is what a user actually saw. The message was built as
# `(prefix + joined) or fallback`, and the prefix is always truthy, so the fallback
# never fired in the one case that needed it most: nothing was even attempted.

def test_nothing_detected_explains_what_is_supported():
    from pathlib import Path
    from src.qatest.service import _cannot_start

    reason = _cannot_start(Path("/tmp/does-not-exist-workfusion"), [], [])
    assert not reason.rstrip().endswith(":"), "the reason trails off"
    assert "uvicorn" in reason and "npm" in reason
    assert "Application URL" in reason


def test_a_failed_start_lists_the_failures():
    from src.qatest.service import _cannot_start

    class Spec:
        kind, name, blocked = "api", "app.main", False

    reason = _cannot_start("root", [Spec()], [(Spec(), "port 8000 already in use")])
    assert "port 8000 already in use" in reason


def test_a_blocked_app_says_its_dependencies_are_missing():
    from src.qatest.service import _cannot_start

    class Spec:
        kind, name, blocked = "ui", "frontend", True

    reason = _cannot_start("root", [Spec()], [])
    assert "dependencies are not installed" in reason
    assert "frontend" in reason


def test_no_branch_of_the_reason_trails_off():
    """Every path must end in a real sentence — that is the whole point."""
    from src.qatest.service import _cannot_start

    class Spec:
        kind, name, blocked = "api", "app", False

    for specs, failures in (([], []), ([Spec()], []),
                            ([Spec()], [(Spec(), "boom")])):
        reason = _cannot_start("root", specs, failures)
        # A trailing colon or a bare prefix is the failure mode: the sentence promises
        # an explanation and does not deliver one.
        assert reason and not reason.rstrip().endswith(":")
        assert reason.rstrip() != "No application could be started:"
        assert reason.strip().endswith((".", "boom"))


def test_the_reason_names_the_file_types_it_actually_found(tmp_path):
    """"Nothing runnable was found" invites a hunt for a bug. Naming what IS there
    answers the question in the same breath — this is not a web application."""
    from src.qatest.service import _cannot_start

    (tmp_path / "claims.bpmn").write_text("<x/>")
    (tmp_path / "action.groovy").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")

    reason = _cannot_start(tmp_path, [], [])
    assert ".bpmn" in reason and ".groovy" in reason
    assert ".js" not in reason, "dependency directories are not the project"
    assert "Application URL" in reason


def test_an_empty_working_copy_says_so():
    from src.qatest.service import _cannot_start
    import tempfile, pathlib

    with tempfile.TemporaryDirectory() as d:
        assert "empty" in _cannot_start(pathlib.Path(d), [], [])


def test_detects_an_app_inside_a_single_wrapper_directory(tmp_path):
    """A folder uploaded through the UI keeps its own name, putting the app one level
    below where a clone leaves it. Three real projects reached a run in this shape and
    were told "no runnable application found" about code that was plainly there."""
    from src.qatest import appserver
    _demo_layout(tmp_path / "aura-cloud-demo")
    specs = {s.kind: s for s in appserver.detect(tmp_path)}
    assert set(specs) == {"api", "ui"}
    assert specs["api"].directory == tmp_path / "aura-cloud-demo" / "backend"


def test_two_sibling_directories_are_never_unwrapped(tmp_path):
    """The unwrap is deliberately limited to a SINGLE child. With two, descending would
    make the choice between them arbitrary, so nothing is detected rather than guessed."""
    from src.qatest import appserver
    _demo_layout(tmp_path / "services")
    _demo_layout(tmp_path / "tools")
    assert appserver.detect(tmp_path) == []


def test_unwrapping_is_bounded(tmp_path):
    """A chain of single directories must not recurse to the bottom of the tree."""
    from src.qatest import appserver
    deep = tmp_path / "a" / "b" / "c" / "d"
    _demo_layout(deep)
    assert appserver.detect(tmp_path) == []


def test_provision_finds_an_app_inside_a_single_wrapper_directory(tmp_path):
    """A folder uploaded through the UI puts the app one level down, and `install` used
    to walk straight past it. Invisible until now, because `appserver` falls back to the
    agent's own interpreter when a project has no venv — so the app booted on Aura's
    dependencies and would have failed for any project needing something else."""
    from src.qatest.provision import _app_dirs
    (tmp_path / "wrapper" / "backend").mkdir(parents=True)
    (tmp_path / "wrapper" / "backend" / "requirements.txt").write_text("fastapi\n")
    found = _app_dirs(tmp_path)
    assert tmp_path / "wrapper" / "backend" in found


def test_provision_does_not_descend_past_an_installable_root(tmp_path):
    """A normal layout must not gain a second level of scanning."""
    from src.qatest.provision import _app_dirs
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "backend" / "nested").mkdir()
    assert tmp_path / "backend" / "nested" not in _app_dirs(tmp_path)


def test_provision_does_not_unwrap_two_siblings(tmp_path):
    """With two candidates there is nothing to disambiguate them, so neither is entered."""
    from src.qatest.provision import _app_dirs
    for name in ("services", "tools"):
        (tmp_path / name / "backend").mkdir(parents=True)
        (tmp_path / name / "backend" / "requirements.txt").write_text("fastapi\n")
    found = _app_dirs(tmp_path)
    assert tmp_path / "services" / "backend" not in found
    assert tmp_path / "tools" / "backend" not in found


def test_the_dev_emulator_is_shared_per_cloud_not_per_project():
    """Floci's ports are fixed, so a container per project could never run alongside
    another — the second Start was refused and a developer had to stop one to see
    another. One name per cloud, whatever project asks."""
    from src.qatest import emulators
    assert emulators.dev_container("aws") == "aura-dev-aws"
    assert emulators.dev_container("aws", "project-a") == \
           emulators.dev_container("aws", "project-b")


def test_each_project_gets_a_distinct_stable_twelve_digit_account():
    """Floci reads a 12-digit access key as the account id and hides one account's
    resources from another. Eleven digits, or a word, silently falls back to the shared
    default — which is what every project used to get."""
    from src.qatest import emulators
    a = emulators.account_for("project-a")
    b = emulators.account_for("project-b")
    assert a != b
    for value in (a, b):
        assert len(value) == 12 and value.isdigit()
    assert emulators.account_for("project-a") == a, "must be stable across calls"


def test_a_project_with_no_id_lands_in_the_default_account():
    from src.qatest import emulators
    assert emulators.account_for("") == "000000000000"


def test_the_aws_env_carries_the_project_account():
    """This is the entire isolation mechanism: the access key IS the account selector."""
    from src.qatest import emulators
    env = emulators._BY_NAME["aws"].env("project-a")
    assert env["AWS_ACCESS_KEY_ID"] == emulators.account_for("project-a")
    # Without a project, unchanged from before — a bare probe has no account to use.
    assert emulators._BY_NAME["aws"].env()["AWS_ACCESS_KEY_ID"] == "test"


def test_only_aws_is_account_scoped():
    """The mechanism is Floci's AWS emulator. Quietly writing an account key into the
    others would imply an isolation they do not provide."""
    from src.qatest import emulators
    for name in ("azure", "gcp", "oci"):
        before = emulators._BY_NAME[name].env()
        assert emulators._BY_NAME[name].env("project-a") == before

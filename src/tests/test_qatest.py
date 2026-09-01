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


def test_only_a_parameterless_get_is_treated_as_browser_reachable():
    """A parameterised path has no value to substitute, and inventing one produces a
    red test that says nothing about the application."""
    cases = {c.name: c.kind for c in
             plan.build_plan("p", {"apis": DEMO_APIS, "services": [], "dependencies": []})}
    assert cases["GET /health"] == "ui"
    assert cases["GET /products/{sku}"] == "api"
    assert cases["POST /quote"] == "api"


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
    monkeypatch.setattr(emulators, "podman_available", lambda: False)
    aws = next(c for c in emulators.CLOUDS if c.name == "aws")
    with emulators.EmulatorSet([aws], "t") as es:
        assert es.records[0].started is False
        assert "podman" in es.records[0].error
        assert es.env == {}          # nothing started, so nothing to point at


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

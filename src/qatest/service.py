"""Orchestrate one test run: plan, emulators, execute, evidence, graph.

The single entry point everything else calls — the CLI, the API router and the
WebSocket all go through `execute`, so there is one definition of what a run is.
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import datetime, timezone

from src.qatest import emulators, evidence, graph_writeback, plan
from src.qatest.types import Case, Report

log = logging.getLogger(__name__)


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def _no_working_copy(project_id: str, checked: list[str]) -> str:
    """Explain a missing working copy in terms someone can act on.

    Naming the paths is most of it, but one pattern is worth calling out by name: when
    every candidate is an absolute /workspace path, the project was uploaded or cloned
    inside a deployed container and its code never existed on this machine. Nothing in
    a bare "not found" hints at that, and it is the single most likely cause for any
    project created through the deployed UI.
    """
    lines = ["No working copy found for this project, so there is nothing to start."]
    lines.append("Looked in: " + "; ".join(checked) + ".")

    elsewhere = [c for c in checked[1:] if c.startswith("/workspace/")]
    if elsewhere:
        lines.append(
            "Every recorded path is under /workspace, which is the workspace inside a "
            "deployed container — so this project's code was uploaded or cloned there, "
            "not here.")
    # "this machine" is ambiguous now that a run can be executed by a self-hosted
    # runner: the reader is looking at a browser pointed at a deployed environment,
    # while the paths above are the RUNNER's. Say which machine is meant.
    lines.append(
        "Two ways to fix it: clone the project into the workspace of the machine "
        "running the test — the paths above are that machine's, not the one you are "
        "reading this on — or start the run with the URL of an already-running "
        "instance instead.")
    return " ".join(lines)


@contextlib.contextmanager
def _maybe_apps(app_url: str, specs: list, env: dict, emit):
    """Start the given applications, unless a URL was supplied.

    A context manager either way so the caller has one code path — the alternative is
    a try/finally that has to remember whether it started anything.
    """
    if app_url:
        yield None
        return

    from src.qatest import appserver

    with appserver.RunningApps(specs, extra_env=env) as apps:
        for spec in apps.started:
            emit("app", kind=spec.kind, name=spec.name, url=spec.url, started=True,
                 message=f"{spec.kind} app started on {spec.url}")
        yield apps


def execute(project_id: str, app_url: str = "", run_id: str | None = None,
            ran_by: str = "", exploratory: bool = False,
            write_graph: bool = True, on_event=None,
            cases: list | None = None, clouds: list[str] | None = None) -> dict:
    """Run the project's plan and store the evidence.

    With no `app_url`, the application is STARTED from the project's own working copy
    — the API and the UI separately, each on a free port — and stopped afterwards.
    That is the difference between "test this project" and "test whatever is at this
    URL", and it removes the mistake of aiming an API plan at a frontend.

    Passing `app_url` still targets something already running, which is what you want
    for a deployed environment.

    Emulators and app processes are torn down whatever happens: a leaked one holds its
    port, and the next run then fails for a reason that looks nothing like the cause.

    `cases` and `clouds` let a caller supply the plan instead of reading it from the
    knowledge graph. That exists for the self-hosted runner: it executes on a developer
    machine and cannot reach Neo4j, which lives on a private subnet, so the API plans
    server-side and ships the result with the claim. Left as None — every existing
    caller — the graph is read exactly as before.
    """
    run_id = run_id or new_run_id()

    def emit(_event: str, **data):
        # Underscore-prefixed so a payload field can be called anything — `kind` is a
        # natural name for an application's kind, and a plain `kind` parameter here
        # collided with it: "emit() got multiple values for argument 'kind'".
        if on_event:
            try:
                on_event({"type": _event, **data})
            except Exception:  # noqa: BLE001 — a progress consumer must not fail a run
                pass

    if cases is None:
        emit("plan", message=f"Reading the knowledge graph for {project_id}")
        facts = plan.fetch_facts(project_id)
        cases = plan.build_plan(project_id, facts)
        needed = emulators.clouds_for(facts.get("dependencies") or [])
    else:
        # Plan supplied by the caller. Case objects may arrive as plain dicts over the
        # wire, so rebuild them — run_plan reads attributes, not keys.
        emit("plan", message=f"Using a plan supplied for {project_id}")
        cases = [c if isinstance(c, Case) else Case(**c) for c in cases]
        wanted = set(clouds or [])
        needed = [c for c in emulators.CLOUDS if c.name in wanted]
    emit("planned", cases=len(cases),
         emulators=[c.name for c in needed],
         message=(f"{len(cases)} case(s); "
                  f"{'emulators: ' + ', '.join(c.name for c in needed) if needed else 'no cloud dependencies'}"))

    # ── The application under test ───────────────────────────────────────────
    # Detected once, here, and handed to _maybe_apps. Detecting twice would allocate
    # two sets of ports and announce ones that never get used.
    urls: dict[str, str] = {}
    specs: list = []
    if app_url:
        urls = {"api": app_url, "ui": app_url}
    else:
        from src.qatest import appserver

        root, checked = appserver.locate(project_id)
        if not root:
            report = Report(run_id=run_id, project_id=project_id, app_url="",
                            ran_by=ran_by, cases=cases, exploratory=exploratory,
                            status="unavailable",
                            reason=_no_working_copy(project_id, checked),
                            completed_at=datetime.now(timezone.utc).isoformat())
            evidence.write_report(report)
            emit("done", status=report.status, reason=report.reason,
                 passed=0, failed=0, skipped=0)
            return report.as_dict()

        specs = appserver.detect(root)
        # Structured, not just a sentence: the timeline renders one row per
        # application with its own state, and parsing that back out of prose would
        # break the first time the wording changed.
        emit("app", stage="detect",
             apps=[{"kind": sp.kind, "name": sp.name, "port": sp.port,
                    "blocked": sp.blocked} for sp in specs],
             message=("Starting " + ", ".join(f"{sp.kind} ({sp.name})" for sp in specs)
                      if specs else f"No runnable application found in {root.name}"))

    if needed and not emulators.podman_available():
        # Say which emulators were wanted. "podman not found" alone leaves the reader
        # guessing whether it mattered for this project.
        report = Report(run_id=run_id, project_id=project_id, app_url=app_url,
                        ran_by=ran_by, cases=cases, exploratory=exploratory,
                        status="unavailable",
                        reason=("podman is not installed, and this project needs the "
                                f"{', '.join(c.name for c in needed)} emulator(s). "
                                "Install podman, or run where it is available."),
                        completed_at=datetime.now(timezone.utc).isoformat())
        evidence.write_report(report)
        emit("done", status=report.status, reason=report.reason,
             passed=0, failed=0, skipped=0)
        return report.as_dict()

    from src.qatest.runner import run_plan

    with emulators.EmulatorSet(needed, run_id) as emus:
        for rec in emus.records:
            emit("emulator", cloud=rec.cloud, started=rec.started,
                 message=(f"{rec.cloud} emulator ready on :{rec.port}" if rec.started
                          else f"{rec.cloud} emulator failed: {rec.error[:160]}"))

        # Started INSIDE the emulator block and after it, so the application inherits
        # the endpoint variables and talks to the emulators rather than real cloud.
        with _maybe_apps(app_url, specs, emus.env, emit) as apps:
            if apps is not None:
                urls = {k: apps.url_for(k) for k in ("api", "ui") if apps.url_for(k)}
                for spec, why in apps.failures:
                    emit("app", kind=spec.kind, name=spec.name, started=False,
                         error=why[:400],
                         message=f"{spec.kind} app not started: {why[:200]}")
                if not urls:
                    report = Report(run_id=run_id, project_id=project_id, app_url="",
                                    ran_by=ran_by, cases=cases, exploratory=exploratory,
                                    status="unavailable",
                                    reason=("No application could be started: "
                                            + "; ".join(w for _, w in apps.failures)
                                            or "nothing runnable was detected"),
                                    completed_at=datetime.now(timezone.utc).isoformat())
                    evidence.write_report(report)
                    emit("done", status=report.status, reason=report.reason,
                         passed=0, failed=0, skipped=0)
                    return report.as_dict()

            target = ", ".join(f"{k}={v}" for k, v in urls.items())
            emit("running", message=f"Executing {len(cases)} case(s) against {target}")
            report = run_plan(project_id, run_id, urls, cases,
                              emulators=emus.records, env=emus.env,
                              ran_by=ran_by, exploratory=exploratory,
                              on_step=lambda s: emit("step", index=s.index,
                                                     action=s.action, status=s.status))

    report.covered = plan.covered_nodes(cases)
    evidence.write_report(report)
    emit("evidence", message=f"Evidence stored under {project_id}/{run_id}/")

    if write_graph:
        steps = evidence.read_steps(project_id, run_id)
        written = graph_writeback.write_results(report, steps)
        emit("graph", message=(f"Graph updated: {written['created']} created, "
                               f"{written['updated']} updated, {written['links']} links")
             if written.get("ok") else
             f"Graph write-back incomplete: {'; '.join(written.get('errors') or [])[:160]}")

    # A one-row index so the existing runs list keeps working without a table scan.
    try:
        from src.database.dynamo_client import put_item
        put_item("test-results", {
            "testRunId": run_id, "projectId": project_id,
            "userId": ran_by, "type": "qatest", "status": report.status,
            "totalPassed": report.total_passed, "totalFailed": report.total_failed,
            "totalSkipped": report.total_skipped,
            "appUrl": app_url,
            "createdAt": report.started_at,
            "completedAt": report.completed_at,
        })
    except Exception as exc:  # noqa: BLE001 — S3 is the source of truth
        log.debug("qatest: index row not written: %s", exc)

    emit("done", status=report.status, passed=report.total_passed,
         failed=report.total_failed, skipped=report.total_skipped,
         durationMs=report.duration_ms, runId=run_id)
    return report.as_dict()

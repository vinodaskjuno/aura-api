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


def _endpoints(emus) -> dict[str, str]:
    """{cloud: endpoint} for the emulators that actually started.

    Built from the same env the application under test was given, so the inventory reads
    exactly the endpoint the app wrote to — not one reconstructed from the port, which
    would drift the moment a cloud changed its variable.
    """
    urls = {"aws": "AWS_ENDPOINT_URL", "gcp": "STORAGE_EMULATOR_HOST",
            "azure": "AZURE_ENDPOINT_URL", "oci": "OCI_ENDPOINT_URL"}
    env = emus.env
    return {rec.cloud: env.get(urls.get(rec.cloud, ""), "")
            for rec in emus.records if rec.started and urls.get(rec.cloud)}


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


def _what_is_there(root) -> str:
    """The file types actually present, so the reader can see WHY nothing matched.

    "No runnable application was found" alone invites the reader to look for a bug.
    "…it holds .bpmn, .groovy and .xml files" answers the question in the same breath:
    this is not a web application, and no amount of retrying will change that.
    """
    from collections import Counter
    from pathlib import Path as _Path

    try:
        root = _Path(root)
        if not root.is_dir():
            return ""
        skip = {"node_modules", ".venv", ".git", "__pycache__", "dist", "build"}
        counts: Counter = Counter()
        for path in root.rglob("*"):
            if path.is_file() and not any(part in skip for part in path.parts):
                if path.suffix:
                    counts[path.suffix.lower()] += 1
        top = [ext for ext, _ in counts.most_common(4)]
        if not top:
            return " — which is empty"
        return " — it holds " + ", ".join(top) + " files"
    except Exception:                                         # noqa: BLE001
        return ""


def _cannot_start(root, specs, failures) -> str:
    """Why no application started, in words the reader can act on.

    This used to read `"No application could be started: " + "; ".join(...) or
    "nothing runnable was detected"`, which binds as `(prefix + joined) or fallback` —
    the prefix is always truthy, so with no failures to list the fallback never fired
    and the message was a sentence ending in a colon. That is the case that needs the
    explanation MOST: nothing was even attempted, and the reader is left with a screen
    that says "Unavailable" and nothing else.
    """
    if failures:
        return ("No application could be started: "
                + "; ".join(why for _, why in failures))
    if not specs:
        return ("No runnable application was found in this project's working copy"
                + _what_is_there(root) + ". QualityMind starts a Python ASGI app "
                "(uvicorn, from a FastAPI/Starlette module) or a Node dev server (an "
                "npm `dev` script), and deliberately does not guess a start command "
                "for anything else — a wrong guess spawns a process that never serves, "
                "and the run then fails for a reason that looks nothing like the "
                "cause. If this project is not a web application, give the run an "
                "Application URL that is already serving instead.")
    blocked = "; ".join(f"{sp.kind} ({sp.name})" for sp in specs if sp.blocked)
    if blocked:
        return (f"The application was detected but could not be started: {blocked}. "
                "Its dependencies are not installed on the runner.")
    return ("An application was detected but none of them started, and no reason was "
            "recorded — this is a bug worth reporting.")


def execute(project_id: str, app_url: str = "", run_id: str | None = None,
            ran_by: str = "", exploratory: bool = False,
            write_graph: bool = True, on_event=None,
            cases: list | None = None, clouds: list[str] | None = None,
            kinds: list[str] | None = None,
            skip_unrunnable: bool = False) -> dict:
    """Run the project's plan and store the evidence.

    With no `app_url`, the application is STARTED from the project's own working copy
    — the API and the UI separately, each on a free port — and stopped afterwards.
    That is the difference between "test this project" and "test whatever is at this
    URL", and it removes the mistake of aiming an API plan at a frontend.

    Passing `app_url` still targets something already running, which is what you want
    for a deployed environment.

    Emulators and app processes are torn down whatever happens: a leaked one holds its
    port, and the next run then fails for a reason that looks nothing like the cause.

    `kinds` restricts the run to the kinds of case the person chose — the root case
    always survives, since it is the only one a frontend can be tested by. Applied here
    AND server-side at claim time; `filter_by_kind` is idempotent precisely so the two
    cannot fight, and the claim-time pass is what lets a runner that has never heard of
    `kinds` still execute the right subset.

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

    facts: dict = {}
    if cases is None:
        emit("plan", message=f"Reading the knowledge graph for {project_id}")
        facts = plan.fetch_facts(project_id)
        from src.qatest import appserver as _appserver
        plan_root, _checked = _appserver.locate(project_id)
        cases = plan.build_plan(project_id, facts, root=plan_root)
        needed = emulators.clouds_for(facts.get("dependencies") or [])
    else:
        # Plan supplied by the caller. Case objects may arrive as plain dicts over the
        # wire, so rebuild them — run_plan reads attributes, not keys.
        emit("plan", message=f"Using a plan supplied for {project_id}")
        cases = [Case.from_wire(c) for c in cases]
        wanted = set(clouds or [])
        needed = [c for c in emulators.CLOUDS if c.name in wanted]
    plan_total = len(cases)
    cases = plan.filter_by_kind(cases, kinds, skip_unrunnable)
    emit("planned", cases=len(cases), planTotal=plan_total, kinds=list(kinds or []),
         emulators=[c.name for c in needed],
         message=(f"{len(cases)} case(s); "
                  f"{'emulators: ' + ', '.join(c.name for c in needed) if needed else 'no cloud dependencies'}"))

    # ── The application under test ───────────────────────────────────────────
    # Detected once, here, and handed to _maybe_apps. Detecting twice would allocate
    # two sets of ports and announce ones that never get used.
    urls: dict[str, str] = {}
    specs: list = []
    # None when the caller pointed the run at a URL: there is no working copy in play,
    # so file checks have nothing to read and are simply not run.
    root = None
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

    # EmulatorSet emits per container as it starts AND as it is removed, so a panel
    # can show Floci coming up and going away rather than only learning it started.
    with emulators.EmulatorSet(needed, run_id, on_event=on_event) as emus:
        # Started INSIDE the emulator block and after it, so the application inherits
        # the endpoint variables and talks to the emulators rather than real cloud.
        with _maybe_apps(app_url, specs, emus.env, emit) as apps:
            if apps is not None:
                urls = {k: apps.url_for(k) for k in ("api", "ui") if apps.url_for(k)}
                for spec, why in apps.failures:
                    emit("app", kind=spec.kind, name=spec.name, started=False,
                         error=why[:400],
                         message=f"{spec.kind} app not started: {why[:200]}")
                structural = [c for c in cases if c.kind == "structure"]
                if not urls and not structural:
                    report = Report(run_id=run_id, project_id=project_id, app_url="",
                                    ran_by=ran_by, cases=cases, exploratory=exploratory,
                                    status="unavailable",
                                    reason=_cannot_start(root, specs, apps.failures),
                                    completed_at=datetime.now(timezone.utc).isoformat())
                    evidence.write_report(report)
                    emit("done", status=report.status, reason=report.reason,
                         passed=0, failed=0, skipped=0)
                    return report.as_dict()
                if not urls:
                    # Nothing serves HTTP, but the file checks need nothing to serve.
                    # This is the whole reason they exist: an RPA application or a set
                    # of migrated DAGs can still be told apart from a broken one.
                    cases = structural
                    emit("app", stage="detect", apps=[],
                         message=("No application to start — running "
                                  f"{len(structural)} file check(s) instead"))

            target = ", ".join(f"{k}={v}" for k, v in urls.items())
            emit("running", message=f"Executing {len(cases)} case(s) against {target}")
            report = run_plan(project_id, run_id, urls, cases, root=root,
                              emulators=emus.records, env=emus.env,
                              ran_by=ran_by, exploratory=exploratory,
                              on_step=lambda s, total: emit(
                                  "step", index=s.index, total=total,
                                  action=s.action, status=s.status,
                                  caseId=s.case_id))

            # Read the emulators back BEFORE the `with` unwinds and removes them. This
            # is the only moment both things are true: the tests have finished, so the
            # resources are whatever they left behind, and the containers are still up,
            # so they can be asked. `evidence.write_report` runs after the block, so the
            # inventory rides the existing write rather than adding one.
            #
            # A green test says the application answered; this says what it reached.
            from src.qatest import inventory

            report.resources = inventory.collect(_endpoints(emus))
            for line in inventory.summarise(report.resources):
                emit("evidence", message=line)

    from src.qatest import coverage as cov

    # run_plan hands these back in memory; falling back to S3 keeps a caller that
    # supplied its own report working.
    steps = report.__dict__.get("_steps")
    if steps is None:
        steps = evidence.read_steps(project_id, run_id)

    statuses = cov.case_statuses(report.cases, steps)
    report.covered = plan.covered_nodes(cases, statuses or None)
    report.selected_kinds = list(kinds or [])
    report.plan_total = plan_total
    # Scored here while the graph facts are still in hand. Without them the denominator
    # falls back to the plan's own node count, which understates a filtered run —
    # `denominatorFromPlan` records which of the two a reader is looking at.
    report.coverage = cov.summarise(
        report, steps, plan.graph_totals(facts) if facts else None)
    evidence.write_report(report)
    emit("evidence", message=f"Evidence stored under {project_id}/{run_id}/")

    if write_graph:
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

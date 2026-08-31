"""Orchestrate one test run: plan, emulators, execute, evidence, graph.

The single entry point everything else calls — the CLI, the API router and the
WebSocket all go through `execute`, so there is one definition of what a run is.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from src.qatest import emulators, evidence, graph_writeback, plan
from src.qatest.types import Report

log = logging.getLogger(__name__)


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def execute(project_id: str, app_url: str, run_id: str | None = None,
            ran_by: str = "", exploratory: bool = False,
            write_graph: bool = True, on_event=None) -> dict:
    """Run the project's plan against `app_url` and store the evidence.

    Returns the report as a dict. Emulators are torn down whatever happens — a leaked
    one holds its port, and the next run then fails for a reason that looks nothing
    like the cause.
    """
    run_id = run_id or new_run_id()

    def emit(kind: str, **data):
        if on_event:
            try:
                on_event({"type": kind, **data})
            except Exception:  # noqa: BLE001 — a progress consumer must not fail a run
                pass

    emit("plan", message=f"Reading the knowledge graph for {project_id}")
    facts = plan.fetch_facts(project_id)
    cases = plan.build_plan(project_id, facts)
    needed = emulators.clouds_for(facts.get("dependencies") or [])
    emit("planned", cases=len(cases),
         emulators=[c.name for c in needed],
         message=(f"{len(cases)} case(s); "
                  f"{'emulators: ' + ', '.join(c.name for c in needed) if needed else 'no cloud dependencies'}"))

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
        emit("done", status=report.status, reason=report.reason)
        return report.as_dict()

    from src.qatest.runner import run_plan

    with emulators.EmulatorSet(needed, run_id) as emus:
        for rec in emus.records:
            emit("emulator", cloud=rec.cloud, started=rec.started,
                 message=(f"{rec.cloud} emulator ready on :{rec.port}" if rec.started
                          else f"{rec.cloud} emulator failed: {rec.error[:160]}"))

        emit("running", message=f"Executing {len(cases)} case(s) against {app_url}")
        report = run_plan(project_id, run_id, app_url, cases,
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
         failed=report.total_failed, skipped=report.total_skipped)
    return report.as_dict()

"""Write test results back into the knowledge graph.

Why this matters beyond a node colour: the edge

    TestCase -[:VERIFIES]-> API | Service

is what makes selection possible. When re-analysis changes an API node, the cases to
re-run are the ones pointing at it — four tests instead of two hundred. It also lets
Onto Verse answer "which parts of this system are actually tested".

**Deliberately NOT reusing `code_graph._upsert`**, though it does the same job. That
function hardcodes `source: "code-analysis"` and audits under `analyse:{project}`.
Test nodes carrying that tag would be swept by `_archive_stale` on the next analysis
and mislabelled in the changelog, so this keeps the same diff-then-audit discipline on
the same primitives, under its own source.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.qatest.types import Report

log = logging.getLogger(__name__)

SOURCE = "qa-test"


def _audit(actor: str, change_type: str, label: str, external_id: str,
           element_id: str, name: str, before, after, project_id: str) -> None:
    """Dual-write history, best-effort — same contract as code_graph._audit.

    Losing an audit row is bad; failing a run whose evidence is already in S3 because
    the changelog table is unreachable would be worse.
    """
    from src.database import dynamo_client as dynamo
    from src.graph import neo4j_client as neo4j
    try:
        neo4j.write_audit_log(actor=actor, action=f"{change_type}:{label}",
                              target_id=element_id, before=before, after=after)
    except Exception as exc:  # noqa: BLE001
        log.debug("qatest audit node failed for %s: %s", external_id, exc)
    try:
        dynamo.write_changelog(dynamo.build_changelog_entry(
            entity_id=element_id, entity_type="Node", entity_label=label,
            entity_name=name, change_type=change_type, actor=actor,
            before=before, after=after,
            session_id=f"qatest:{project_id}", source=SOURCE,
            external_id=external_id,
        ))
    except Exception as exc:  # noqa: BLE001
        log.debug("qatest changelog failed for %s: %s", external_id, exc)


def _upsert(label: str, external_id: str, props: dict, actor: str,
            project_id: str, counters: dict) -> str | None:
    """Upsert one node, auditing only when a managed field actually changed."""
    from src.graph import code_graph
    from src.graph import neo4j_client as neo4j

    # `status` is the node LIFECYCLE field across the whole graph and is forced to
    # "active" here, exactly as code_graph does. A test OUTCOME therefore cannot live
    # in it — writing one there was silently overwritten. Outcomes use `result`.
    props = {**props, "source": SOURCE, "status": "active"}
    managed = [k for k in props if k not in ("createdAt", "updatedAt")]
    try:
        before, after, element_id, created = neo4j.upsert_node_returning_id(
            label, external_id, props)
    except Exception as exc:  # noqa: BLE001 — one bad node must not abort the write-back
        counters["errors"].append(f"{label} {external_id}: {exc}")
        return None
    if not element_id:
        counters["errors"].append(f"{label} {external_id}: upsert returned no node")
        return None

    diff = code_graph._changed(before, after, managed)
    if created:
        counters["created"] += 1
        _audit(actor, "CREATE", label, external_id, element_id,
               props.get("name", external_id), None, after, project_id)
    elif diff:
        counters["updated"] += 1
        _audit(actor, "UPDATE", label, external_id, element_id,
               props.get("name", external_id),
               {k: v[0] for k, v in diff.items()},
               {k: v[1] for k, v in diff.items()}, project_id)
    else:
        counters["unchanged"] += 1
    return element_id


def _link(from_label: str, from_eid: str, to_label: str, to_eid: str, rel: str,
          fact_type: str = "known") -> bool:
    from src.graph import neo4j_client as neo4j
    try:
        neo4j.upsert_relationship(
            from_label, from_eid, to_label, to_eid, rel,
            provenance={"source": SOURCE, "discoveredBy": "qatest",
                        "factType": fact_type})
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("qatest link %s->%s failed: %s", from_eid, to_eid, exc)
        return False


def case_status(report: Report, steps: list) -> dict[str, str]:
    """Worst observed status per case.

    Worst, not last: a case whose second step failed is a failing case, and reporting
    the final step's status would hide it.
    """
    rank = {"passed": 0, "skipped": 1, "unemulated": 2, "failed": 3}
    out: dict[str, str] = {}
    for step in steps:
        cid = step.case_id if hasattr(step, "case_id") else step.get("caseId", "")
        st = step.status if hasattr(step, "status") else step.get("status", "")
        if not cid or not st:
            continue
        if cid not in out or rank.get(st, 0) > rank.get(out[cid], 0):
            out[cid] = st
    return out


def write_results(report: Report, steps: list | None = None,
                  actor: str = "qatest") -> dict:
    """Upsert a TestRun and one TestCase per case, linked to what each verifies.

    Never raises. The evidence is already in S3 by the time this runs, so a graph
    outage must degrade the extra insight rather than lose the result.
    """
    counters = {"created": 0, "updated": 0, "unchanged": 0, "links": 0,
                "errors": []}
    project_id = report.project_id
    run_eid = f"testrun:{project_id}:{report.run_id}"
    statuses = case_status(report, steps or [])

    try:
        run_id_written = _upsert("TestRun", run_eid, {
            "name": f"run {report.run_id}",
            "runId": report.run_id, "projectId": project_id,
            "result": report.status, "appUrl": report.app_url,
            "totalPassed": report.total_passed, "totalFailed": report.total_failed,
            "totalSkipped": report.total_skipped,
            "startedAt": report.started_at,
            "completedAt": report.completed_at or datetime.now(timezone.utc).isoformat(),
            "ranBy": report.ran_by,
        }, actor, project_id, counters)
        if not run_id_written:
            return {**counters, "ok": False}

        for case in report.cases:
            case_eid = f"testcase:{project_id}:{case.case_id}"
            _upsert("TestCase", case_eid, {
                "name": case.name, "kind": case.kind, "projectId": project_id,
                "caseId": case.case_id,
                "result": statuses.get(case.case_id, "skipped"),
                "lastRun": report.completed_at or report.started_at,
                "runId": report.run_id, "sourceFile": case.source_file,
            }, actor, project_id, counters)

            if _link("TestRun", run_eid, "TestCase", case_eid, "EXECUTED"):
                counters["links"] += 1
            if case.verifies_eid and case.verifies_label:
                # inferred: the link comes from a regex-derived route, not a resolved
                # router table — the edge says so, as code_graph's EXPOSES does.
                if _link("TestCase", case_eid, case.verifies_label,
                         case.verifies_eid, "VERIFIES", fact_type="inferred"):
                    counters["links"] += 1
    except Exception as exc:  # noqa: BLE001
        counters["errors"].append(str(exc))
        log.warning("qatest: graph write-back failed: %s", exc)
        return {**counters, "ok": False}

    return {**counters, "ok": not counters["errors"]}

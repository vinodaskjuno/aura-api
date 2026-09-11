"""How much of the application a run actually verified.

Two numbers, deliberately, because they answer different questions and either one
alone misleads:

    graph coverage   API and Service nodes verified by a PASSING case, over all such
                     nodes the project has. What proportion of the application is
                     known to work.
    execution rate   cases that ran, over cases planned. How much of the plan the
                     harness could actually carry out.

A run can score 100% execution and 20% graph coverage — every case it chose to run
ran, but most of the application was never selected. Showing only the first would
read as a clean bill of health.

**The number will be low, and that is correct.** Non-GET routes cannot be called
without a request body the graph does not describe, parameterised paths have no known
value, and a Service node names a code unit rather than an address — so a typical
project lands near a third. `uncovered` carries a reason per node, which is what makes
a low number actionable instead of demoralising. Counting skipped cases as covered
would make the whole measurement worthless.

API and Service are reported separately for the same reason: `runner` records every
smoke case as skipped, so Service nodes can never be verified and folding them into
one denominator caps coverage below 100% for a reason that says nothing about the
application.
"""
from __future__ import annotations

from typing import Any

from src.qatest.types import Case, Report, Step

#: Worst-status ordering. A node checked by two cases, one passing and one failing, is
#: not verified — so `failed` must outrank `passed`.
_RANK = {"passed": 0, "unemulated": 1, "skipped": 2, "failed": 3}

#: Why a node ended up uncovered, when the case never produced a step at all. A case
#: that was filtered out by the user is not the application's fault, and the wording
#: has to make that difference visible.
NOT_SELECTED = "not selected for this run"


def case_statuses(cases: list[Case], steps: list[Step]) -> dict[str, str]:
    """Worst step status per case id. A case with no steps is absent, not passing."""
    out: dict[str, str] = {}
    for step in steps:
        cid = getattr(step, "case_id", "") or ""
        if not cid:
            continue
        status = getattr(step, "status", "skipped")
        if cid not in out or _RANK.get(status, 3) > _RANK.get(out[cid], 0):
            out[cid] = status
    return out


def summarise(report: Report, steps: list[Step],
              graph_totals: dict[str, int] | None = None) -> dict[str, Any]:
    """The coverage block stored on a report and rendered by the UI.

    `graph_totals` is the denominator — `{"apis": n, "services": n}` from
    `plan.graph_totals`. Passed in rather than read here so this stays pure and a
    stored report can be re-summarised later without touching the graph. Absent, the
    plan's own node count stands in, which is right for a full run and understates the
    denominator for a filtered one — reported as `denominatorFromPlan` so a reader
    knows which they are looking at.
    """
    statuses = case_statuses(report.cases, steps)

    # Nodes this plan referenced, and how each fared.
    nodes: dict[str, dict[str, Any]] = {}
    for case in report.cases:
        if not case.verifies_eid:
            continue
        entry = nodes.setdefault(case.verifies_eid, {
            "externalId": case.verifies_eid,
            "label": case.verifies_label or "API",
            "name": case.name,
            "method": case.method,
            "path": case.path,
            "result": None,
            "reason": "",
        })
        status = statuses.get(case.case_id)
        if status is None:
            # Planned but never executed: either the user excluded its kind, or the
            # plan itself declined it up front.
            status, reason = "not-run", (case.skip_reason or NOT_SELECTED)
        else:
            reason = case.skip_reason if status == "skipped" else ""
        if entry["result"] is None or \
                _RANK.get(status, 3) >= _RANK.get(entry["result"], 0):
            entry["result"] = status
            entry["reason"] = reason

    by_label = {"API": {"total": 0, "covered": 0}, "Service": {"total": 0, "covered": 0}}
    uncovered = []
    for node in nodes.values():
        bucket = by_label.setdefault(node["label"], {"total": 0, "covered": 0})
        bucket["total"] += 1
        if node["result"] == "passed":
            bucket["covered"] += 1
        else:
            uncovered.append({k: node[k] for k in
                              ("externalId", "label", "name", "method", "path",
                               "reason")} | {"result": node["result"]})

    totals = graph_totals or {}
    api_total = int(totals.get("apis") or 0) or by_label["API"]["total"]
    svc_total = int(totals.get("services") or 0) or by_label["Service"]["total"]
    node_total = api_total + svc_total
    node_covered = by_label["API"]["covered"] + by_label["Service"]["covered"]

    planned = len(report.cases)
    executed = sum(1 for c in report.cases if c.case_id in statuses)
    skipped = sum(1 for s in statuses.values() if s == "skipped")
    unemulated = sum(1 for s in statuses.values() if s == "unemulated")
    untestable = skipped + unemulated

    # Nodes the project has that this run's plan never referenced — because the user
    # excluded their kind, or because the plan was built before they existed. They are
    # counted in the denominator, so without this the arithmetic does not add up on
    # screen: a coverage of 2/5 that lists only two uncovered nodes reads as a bug.
    not_planned = max(0, node_total - len(nodes))

    return {
        "nodeTotal": node_total,
        "nodeCovered": node_covered,
        # None, never 0. A project with no API or Service nodes has no coverage to
        # report, which is a different statement from "nothing is covered".
        "nodePct": _pct(node_covered, node_total),
        "api": {"total": api_total, "covered": by_label["API"]["covered"],
                "pct": _pct(by_label["API"]["covered"], api_total)},
        "service": {"total": svc_total, "covered": by_label["Service"]["covered"],
                    "pct": _pct(by_label["Service"]["covered"], svc_total),
                    # Said plainly so nobody spends an afternoon on why it is zero.
                    "note": ("A Service node names a code unit, not an address, so a "
                             "smoke case is recorded as skipped and can never verify "
                             "one." if svc_total else "")},
        "planned": planned,
        "executed": executed,
        "executionPct": _pct(executed, planned),
        "skipped": skipped,
        "unemulated": unemulated,
        # The two above, as one number, because they answer the question a
        # reader actually has: how much of this plan could never have worked.
        # A run reporting "0 failed" and a run where nothing executed look
        # identical without it, and the second is the common case.
        "untestable": untestable,
        "untestablePct": _pct(untestable, planned),
        "notPlanned": not_planned,
        "denominatorFromPlan": not bool(totals),
        "uncovered": sorted(uncovered, key=lambda n: (n["label"], n["externalId"])),
        "runId": report.run_id,
    }


def _pct(part: int, whole: int) -> int | None:
    return round(part / whole * 100) if whole else None

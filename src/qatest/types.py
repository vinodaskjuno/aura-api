"""The record of what a test run did.

One shape, written once to S3 and read by the UI, the CLI and the plugin. Kept in
its own module with no imports from the rest of AURA so the writer, the reader and
the tests cannot drift from each other.

`Step` is the piece that did not exist before: runs previously recorded only counts,
so a stored result could not answer "what actually happened, and where did it fail".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Literal

# A step either ran and passed, ran and failed, was never reached, or asked for a
# cloud service nothing emulates. The last one is deliberately NOT "failed": the
# application may be perfectly correct and the harness simply cannot answer.
StepStatus = Literal["passed", "failed", "skipped", "unemulated"]

# A run that could not execute is "unavailable" — never a count of tests that did
# not run. The container path used to report fabricated passes; this type makes that
# unrepresentable.
RunStatus = Literal["passed", "failed", "unavailable"]

# What KIND of thing is being tested — not how the harness reaches it.
#
#   ui     the application root: does it load, render, and not throw
#   api    an API node from the graph, whatever its method or path shape
#   smoke  a Service node
#   structure  a file, checked without running anything — for projects that do not
#              serve HTTP at all (BPMN/Groovy, or migrated Airflow DAGs)
#   stack      a property of the RUNNING target platform — "every converted DAG
#              imports" — asked of the compose stack the converter shipped
#
# These values used to mean browser-openable / not-openable / service, which made
# `GET /health` a "ui" case and `POST /quote` an "api" one. That is the opposite of
# what anyone reading a filter labelled "API" expects, so the meaning was corrected
# when the kinds became user-selectable. Reports written before that carry the old
# meaning; `Report.selected_kinds` is present only on new ones and is the discriminator.
CaseKind = Literal["api", "ui", "smoke", "structure", "stack"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Case:
    """One thing to test, derived from a knowledge-graph node."""
    case_id: str
    kind: CaseKind
    name: str
    # The graph node this verifies, so results can be written back as an edge and a
    # code change can select the cases that cover what changed.
    verifies_label: str = ""        # "API" | "Service"
    verifies_eid: str = ""
    method: str = ""
    path: str = ""
    source_file: str = ""
    # Non-empty means "planned, but cannot be executed" — and says why, in the words
    # the step's error will carry. Set by plan.why_unrunnable so the preview, the
    # runner and the coverage report cannot disagree about which cases can run.
    skip_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_wire(cls, data: "Case | dict[str, Any]") -> "Case":
        """Rebuild a Case from a dict, ignoring fields this build does not know.

        A plain `Case(**d)` raises TypeError on an unknown key. That matters because
        cases cross a network boundary: the API plans server-side and ships them to a
        self-hosted runner, so a server that gains a field kills every agent already
        running — uncaught, inside its poll loop. Filtering here makes that class of
        break impossible in the new-server/old-agent direction.
        """
        if isinstance(data, cls):
            return data
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Step:
    index: int
    action: str                     # what was attempted, in plain words
    target: str                     # URL, selector or service call
    status: StepStatus
    duration_ms: int = 0
    error: str = ""
    screenshot_key: str = ""        # S3 key, relative to the run prefix
    case_id: str = ""
    started_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {
            "index": d["index"], "action": d["action"], "target": d["target"],
            "status": d["status"], "durationMs": d["duration_ms"],
            "error": d["error"], "screenshotKey": d["screenshot_key"],
            "caseId": d["case_id"], "startedAt": d["started_at"],
        }


@dataclass
class EmulatorRecord:
    """Which emulator served a run, pinned so the result stays identifiable.

    A result is only evidence if you can say what produced it — hence the digest
    rather than a moving tag.
    """
    cloud: str
    image: str
    digest: str
    port: int
    container: str = ""
    started: bool = False
    error: str = ""
    #: Already running when the run began — started by `floci-cli` or from DevMate, not
    #: by this run. Aura must never stop one of these.
    adopted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"cloud": self.cloud, "image": self.image, "digest": self.digest,
                "port": self.port, "container": self.container,
                "started": self.started, "error": self.error,
                "adopted": self.adopted}

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "EmulatorRecord":
        """Rebuild from a stored or transmitted report, dropping keys we do not know.

        `EmulatorRecord(**data)` raises TypeError on an unknown key, so a report written
        by a NEWER runner would crash an older server reading it — the failure landing
        after the run had already succeeded and stored its evidence. Filtering instead
        means a field added later degrades to "not shown" rather than to an exception.
        Same reasoning, and the same shape, as `Case.from_wire`.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class Report:
    run_id: str
    project_id: str
    app_url: str
    status: RunStatus = "passed"
    reason: str = ""                # why, when status is unavailable
    started_at: str = field(default_factory=_now)
    completed_at: str = ""
    ran_by: str = ""
    total_passed: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    total_unemulated: int = 0
    duration_ms: int = 0
    cases: list[Case] = field(default_factory=list)
    emulators: list[EmulatorRecord] = field(default_factory=list)
    # Graph nodes this run touched, so OntoVerse can colour them.
    covered: list[dict[str, str]] = field(default_factory=list)
    # What the person asked for, so a reader can tell a partial run from a full one.
    # A run with selected_kinds == ["api"] that reports 3 cases is not a project with
    # 3 endpoints.
    selected_kinds: list[str] = field(default_factory=list)
    plan_total: int = 0              # cases in the FULL plan, before any filtering
    coverage: dict[str, Any] = field(default_factory=dict)
    exploratory: bool = False
    # What was actually inside the emulators when the run finished — {cloud: {service:
    # [{name, count, items}]}}. On the REPORT rather than on each EmulatorRecord,
    # because `from_dict` rebuilds records positionally and an unknown key there is
    # fatal; see EmulatorRecord.from_wire.
    resources: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id, "projectId": self.project_id,
            "appUrl": self.app_url, "status": self.status, "reason": self.reason,
            "startedAt": self.started_at, "completedAt": self.completed_at,
            "ranBy": self.ran_by,
            "totalPassed": self.total_passed, "totalFailed": self.total_failed,
            "totalSkipped": self.total_skipped,
            "totalUnemulated": self.total_unemulated,
            "durationMs": self.duration_ms,
            "cases": [c.as_dict() for c in self.cases],
            "emulators": [e.as_dict() for e in self.emulators],
            "covered": self.covered,
            "selectedKinds": self.selected_kinds,
            "planTotal": self.plan_total,
            "coverage": self.coverage,
            "exploratory": self.exploratory,
            "resources": self.resources,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Report":
        """Rebuild a Report from what as_dict() produced.

        The inverse exists because a report now crosses a network boundary: a
        self-hosted runner executes the run and POSTs `as_dict()` back, and the API then
        writes it to the knowledge graph. `graph_writeback.write_results` reads
        ATTRIBUTES — `report.project_id`, `report.cases[].case_id` — so a plain dict
        fails with "'dict' object has no attribute 'project_id'" after the run has
        already succeeded and its evidence has been stored. Found exactly that way.

        Tolerant of missing keys on purpose: an older runner should degrade to a
        partial graph write, not crash the endpoint.
        """
        cases = [Case.from_wire(c) for c in (data.get("cases") or [])]
        emulators = [e if isinstance(e, EmulatorRecord) else EmulatorRecord.from_wire(e)
                     for e in (data.get("emulators") or [])]
        return cls(
            run_id=data.get("runId", ""),
            project_id=data.get("projectId", ""),
            app_url=data.get("appUrl", ""),
            status=data.get("status", "passed"),
            reason=data.get("reason", ""),
            started_at=data.get("startedAt", "") or _now(),
            completed_at=data.get("completedAt", ""),
            ran_by=data.get("ranBy", ""),
            total_passed=int(data.get("totalPassed") or 0),
            total_failed=int(data.get("totalFailed") or 0),
            total_skipped=int(data.get("totalSkipped") or 0),
            total_unemulated=int(data.get("totalUnemulated") or 0),
            duration_ms=int(data.get("durationMs") or 0),
            cases=cases,
            emulators=emulators,
            covered=data.get("covered") or [],
            selected_kinds=list(data.get("selectedKinds") or []),
            plan_total=int(data.get("planTotal") or 0),
            coverage=data.get("coverage") or {},
            exploratory=bool(data.get("exploratory")),
            # Absent from every report written before this existed, which is exactly
            # what the default covers.
            resources=data.get("resources") or {},
        )

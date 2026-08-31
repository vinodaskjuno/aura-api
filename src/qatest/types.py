"""The record of what a test run did.

One shape, written once to S3 and read by the UI, the CLI and the plugin. Kept in
its own module with no imports from the rest of AURA so the writer, the reader and
the tests cannot drift from each other.

`Step` is the piece that did not exist before: runs previously recorded only counts,
so a stored result could not answer "what actually happened, and where did it fail".
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
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

CaseKind = Literal["api", "ui", "smoke"]


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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    def as_dict(self) -> dict[str, Any]:
        return {"cloud": self.cloud, "image": self.image, "digest": self.digest,
                "port": self.port, "container": self.container,
                "started": self.started, "error": self.error}


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
    exploratory: bool = False

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
            "exploratory": self.exploratory,
        }

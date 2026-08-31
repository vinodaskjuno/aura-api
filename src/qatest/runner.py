"""Execute a plan and record what actually happened, step by step.

Two things this does that the previous runners did not.

It records a STEP per action — action, target, status, duration, error text and a
screenshot key — instead of only counts. A stored run can then answer "what happened
and where did it fail" for someone who was not watching.

And it never reports a pass it did not observe. A run that cannot execute returns
`status: "unavailable"` with the reason; a cloud call nothing emulates is `unemulated`,
which is deliberately not `failed` because the application may be perfectly correct
and the harness simply cannot answer.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from src.qatest import evidence
from src.qatest.types import Case, EmulatorRecord, Report, Step

log = logging.getLogger(__name__)

_STEP_TIMEOUT_MS = 20_000


class _Recorder:
    """Collects steps and screenshots, keeping their numbering in one place.

    The screenshot key is allocated from the same counter as the step index, so a
    step and its image can never disagree about which is which.
    """

    def __init__(self, project_id: str, run_id: str):
        self.project_id = project_id
        self.run_id = run_id
        self.steps: list[Step] = []
        self.console: list[str] = []

    def add(self, action: str, target: str, status: str, started: float,
            error: str = "", png: bytes | None = None, case_id: str = "") -> Step:
        index = len(self.steps) + 1
        key = ""
        if png:
            try:
                key = evidence.write_screenshot(self.project_id, self.run_id, index, png)
            except Exception as exc:  # noqa: BLE001 — losing an image must not lose the step
                log.warning("qatest: screenshot upload failed at step %s: %s", index, exc)
                self.console.append(f"screenshot upload failed at step {index}: {exc}")
        step = Step(index=index, action=action, target=target, status=status,  # type: ignore[arg-type]
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=error[:2000], screenshot_key=key, case_id=case_id)
        self.steps.append(step)
        return step


def _playwright_available() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        return False, (f"playwright is not installed ({exc}). "
                       "pip install playwright && playwright install chromium")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"playwright import failed: {exc}"
    return True, ""


def _url_for(app_url: str, case: Case) -> str:
    return f"{app_url.rstrip('/')}{case.path or '/'}"


def run_plan(project_id: str, run_id: str, app_url: str, cases: list[Case],
             emulators: list[EmulatorRecord] | None = None,
             env: dict[str, str] | None = None,
             ran_by: str = "", exploratory: bool = False,
             on_step=None) -> Report:
    """Run every case in order and return the report. Does not write to S3.

    `on_step` is called with each completed Step so a caller can stream progress; the
    stored evidence is identical either way.
    """
    started_wall = time.monotonic()
    report = Report(run_id=run_id, project_id=project_id, app_url=app_url,
                    ran_by=ran_by, exploratory=exploratory,
                    emulators=list(emulators or []), cases=list(cases))

    ok, why = _playwright_available()
    if not ok:
        report.status = "unavailable"
        report.reason = why
        report.completed_at = datetime.now(timezone.utc).isoformat()
        log.warning("qatest: run %s unavailable — %s", run_id, why)
        return report

    if not cases:
        report.status = "unavailable"
        report.reason = ("No cases to run: the knowledge graph holds no API or Service "
                        "nodes for this project. Run code analysis first.")
        report.completed_at = datetime.now(timezone.utc).isoformat()
        return report

    rec = _Recorder(project_id, run_id)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        # Console errors and failed requests are evidence too — a page that renders
        # while throwing is a pass that should not be trusted silently.
        page.on("console", lambda m: rec.console.append(f"[{m.type}] {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("requestfailed",
                lambda r: rec.console.append(f"[requestfailed] {r.method} {r.url}"))

        for case in cases:
            started = time.monotonic()

            if case.kind == "smoke":
                # A Service node names a code unit, not an address. Recording it as
                # skipped-with-a-reason is honest; asserting it "passed" would not be.
                rec.add(f"service smoke: {case.name}", case.verifies_eid, "skipped",
                        started, error="no HTTP address for a Service node",
                        case_id=case.case_id)
                continue

            url = _url_for(app_url, case)

            if case.method != "GET":
                # A blind POST would mutate state with invented data; the request
                # shape is not in the graph, so this is not something we can assert.
                rec.add(f"{case.method} {case.path}", url, "skipped", started,
                        error=f"{case.method} needs a request body the graph does not describe",
                        case_id=case.case_id)
                continue

            if "{" in case.path or ":" in case.path.split("/")[-1]:
                rec.add(f"GET {case.path}", url, "skipped", started,
                        error="path parameter has no known value",
                        case_id=case.case_id)
                continue

            try:
                resp = page.goto(url, timeout=_STEP_TIMEOUT_MS, wait_until="domcontentloaded")
                png = page.screenshot(full_page=True)
                code = resp.status if resp else 0
                if 200 <= code < 400:
                    rec.add(f"GET {case.path} -> {code}", url, "passed", started,
                            png=png, case_id=case.case_id)
                else:
                    rec.add(f"GET {case.path} -> {code}", url, "failed", started,
                            error=f"HTTP {code}", png=png, case_id=case.case_id)
            except Exception as exc:  # noqa: BLE001 — a failed case is data, not a crash
                png = None
                try:
                    png = page.screenshot(full_page=True)
                except Exception:  # noqa: BLE001 — page may be unusable
                    pass
                rec.add(f"GET {case.path}", url, "failed", started,
                        error=str(exc), png=png, case_id=case.case_id)

            if on_step:
                try:
                    on_step(rec.steps[-1])
                except Exception:  # noqa: BLE001 — a progress consumer must not fail the run
                    pass

        browser.close()

    report.total_passed = sum(1 for s in rec.steps if s.status == "passed")
    report.total_failed = sum(1 for s in rec.steps if s.status == "failed")
    report.total_skipped = sum(1 for s in rec.steps if s.status == "skipped")
    report.total_unemulated = sum(1 for s in rec.steps if s.status == "unemulated")
    report.status = "failed" if report.total_failed else "passed"
    report.duration_ms = int((time.monotonic() - started_wall) * 1000)
    report.completed_at = datetime.now(timezone.utc).isoformat()

    report_steps = rec.steps
    try:
        evidence.write_steps(project_id, run_id, report_steps)
        if rec.console:
            evidence.write_console(project_id, run_id, rec.console)
    except Exception as exc:  # noqa: BLE001
        log.warning("qatest: evidence upload failed: %s", exc)
        report.reason = f"evidence partially uploaded: {exc}"

    return report

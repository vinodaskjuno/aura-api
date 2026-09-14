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
from src.qatest import plan
from src.qatest.types import Case, EmulatorRecord, Report, Step

log = logging.getLogger(__name__)

_STEP_TIMEOUT_MS = 20_000


class _Recorder:
    """Collects steps and screenshots, keeping their numbering in one place.

    The screenshot key is allocated from the same counter as the step index, so a
    step and its image can never disagree about which is which.
    """

    def __init__(self, project_id: str, run_id: str, on_step=None, total: int = 0):
        self.project_id = project_id
        self.run_id = run_id
        # Reporting lives HERE rather than at each call site. It used to be called from
        # two of the five branches, so every skipped case — a Service smoke test, a
        # non-GET route, a parameterised path — advanced the run without advancing the
        # progress bar, and a project with many POST routes showed a bar that stalled
        # and never reached 100%. One case produces exactly one step, so this is also
        # the only place that can guarantee one report per case.
        self.on_step = on_step
        self.total = total
        self.steps: list[Step] = []
        self.console: list[str] = []
        # Problems seen since the last step closed, so each step owns the errors its
        # own navigation produced. A page that renders while throwing is not a pass.
        self.pending: list[str] = []

    def take_pending(self) -> list[str]:
        out, self.pending = self.pending, []
        return out

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
        if self.on_step:
            try:
                self.on_step(step, self.total)
            except Exception:  # noqa: BLE001 — a progress consumer must not fail a run
                pass
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


def _base_for(urls: dict[str, str], case: Case) -> str:
    """Which running application a case belongs to.

    A project can expose two: an API and a UI. Sending API cases at the UI is the
    mistake that made an SPA report every route as a pass, so the mapping is explicit
    rather than "whatever URL was typed".
    """
    if case.case_id == "root-001":
        # The root check is about the thing a person opens — the UI when there is one.
        return urls.get("ui") or urls.get("api") or ""
    return urls.get("api") or urls.get("ui") or ""


def _url_for(urls: dict[str, str], case: Case) -> str:
    base = _base_for(urls, case)
    return f"{base.rstrip('/')}{case.path or '/'}" if base else ""


def _run_structure(rec: "_Recorder", cases: list[Case], root) -> None:
    """Run every file check. No browser, no application, no emulator."""
    from src.qatest import structure

    for case in cases:
        started = time.monotonic()
        check = structure.Check(check_id=case.case_id, name=case.name,
                                rel_path=case.path, validator=case.method)
        ok, detail = structure.run_check(root, check)
        rec.add(case.name, case.path, "passed" if ok else "failed", started,
                error="" if ok else detail, case_id=case.case_id)
        # The detail of a PASS is worth keeping too — "8 sequence flows all resolve"
        # is what makes a green tick mean something.
        if ok and detail:
            rec.steps[-1].action = f"{case.name} — {detail}"


def _run_policy(rec: "_Recorder", cases: list[Case], root) -> None:
    """NIST controls over the project's IaC.

    Needs no app, no browser and no emulator — it reads files — so it runs in the same
    early pass as the structure checks, before Playwright is even looked for.
    """
    from src.qatest import policy

    for case in cases:
        started = time.monotonic()
        check = policy.Check(check_id=case.case_id, name=case.name,
                             rel_path=case.path, validator=case.method)
        ok, detail = policy.run_check(root, check)
        rec.add(case.name, case.path, "passed" if ok else "failed", started,
                error="" if ok else detail, case_id=case.case_id)
        if ok and detail:
            # A passing control has to say what it verified, and what it did not. The
            # step text is the only place a reader sees that.
            rec.steps[-1].action = f"{case.name} — {detail}"


def _run_stack(rec: "_Recorder", cases: list[Case], urls: dict[str, str]) -> None:
    """Ask the running stack the questions only it can answer."""
    from src.qatest import stack as stack_mod

    base = urls.get("api") or urls.get("ui") or ""
    for case in cases:
        started = time.monotonic()
        if not base:
            rec.add(case.name, case.path, "skipped", started,
                    error="the stack did not start, so it could not be asked",
                    case_id=case.case_id)
            continue
        assertion = stack_mod.Assertion(
            assertion_id=case.case_id, name=case.name, path=case.path,
            checker=case.method,
            auth=stack_mod._AIRFLOW_AUTH if case.method.startswith("airflow") else None)
        ok, detail = stack_mod.run_assertion(base, assertion)
        rec.add(f"{case.name} — {detail}" if ok else case.name, base + case.path,
                "passed" if ok else "failed", started,
                error="" if ok else detail, case_id=case.case_id)


def _finish(report: Report, rec: "_Recorder", project_id: str, run_id: str,
            started_wall: float) -> Report:
    """Tally, store evidence, and stamp the report. Shared by both exit paths."""
    report.total_passed = sum(1 for s in rec.steps if s.status == "passed")
    report.total_failed = sum(1 for s in rec.steps if s.status == "failed")
    report.total_skipped = sum(1 for s in rec.steps if s.status == "skipped")
    report.total_unemulated = sum(1 for s in rec.steps if s.status == "unemulated")
    report.status = "failed" if report.total_failed else "passed"
    report.duration_ms = int((time.monotonic() - started_wall) * 1000)
    report.completed_at = datetime.now(timezone.utc).isoformat()
    report.__dict__["_steps"] = list(rec.steps)
    try:
        evidence.write_steps(project_id, run_id, rec.steps)
        if rec.console:
            evidence.write_console(project_id, run_id, rec.console)
    except Exception as exc:  # noqa: BLE001
        log.warning("qatest: evidence upload failed: %s", exc)
        report.reason = f"evidence partially uploaded: {exc}"
    return report


def run_plan(project_id: str, run_id: str, urls: dict[str, str] | str,
             cases: list[Case],
             emulators: list[EmulatorRecord] | None = None,
             env: dict[str, str] | None = None,
             ran_by: str = "", exploratory: bool = False,
             on_step=None, root=None) -> Report:
    """Run every case in order and return the report. Does not write to S3.

    `on_step` is called as `on_step(step, total)` for EVERY completed step — including
    skipped ones — so a progress bar reaches 100%. The stored evidence is identical
    either way.
    """
    # A bare string keeps the CLI's --url and the router's app_url working: it means
    # "one application, serving everything".
    if isinstance(urls, str):
        urls = {"api": urls, "ui": urls}
    app_url = urls.get("ui") or urls.get("api") or ""

    started_wall = time.monotonic()
    report = Report(run_id=run_id, project_id=project_id, app_url=app_url,
                    ran_by=ran_by, exploratory=exploratory,
                    emulators=list(emulators or []), cases=list(cases))

    # File checks first, and without a browser. A project that serves no HTTP has
    # nothing else it can be asked, so gating these behind Playwright would put the
    # only answerable questions behind a requirement they do not have.
    structural = [c for c in cases if c.kind == "structure"]
    policy_cases = [c for c in cases if c.kind == "policy"]
    stack_cases = [c for c in cases if c.kind == "stack"]
    # A DENYLIST, so any kind not named here is treated as a URL to navigate to. Adding a
    # kind without adding it here sends it to the browser runner, which fails it with a
    # navigation error that says nothing about the real problem.
    web = [c for c in cases if c.kind not in ("structure", "stack", "policy")]

    if (structural or policy_cases) and root is not None:
        rec_early = _Recorder(project_id, run_id, on_step=on_step, total=len(cases))
        if structural:
            _run_structure(rec_early, structural, root)
        if policy_cases:
            _run_policy(rec_early, policy_cases, root)
        if not web and not stack_cases:
            return _finish(report, rec_early, project_id, run_id, started_wall)

    ok, why = _playwright_available()
    if not ok:
        if structural and root is not None:
            # The file checks already ran and mean what they say. Reporting the whole
            # run as `unavailable` would throw away real results because a DIFFERENT
            # kind of case could not run.
            report = _finish(report, rec_early, project_id, run_id, started_wall)
            report.reason = (f"{len(web)} case(s) needed a browser and were not run: "
                             f"{why}")
            return report
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

    if stack_cases and not web:
        # A converted project: a stack to ask questions of, and no graph-derived
        # routes to browse. No browser needed at all.
        rec_only = _Recorder(project_id, run_id, on_step=on_step, total=len(cases))
        if structural and root is not None:
            rec_only.steps.extend(rec_early.steps)
        _run_stack(rec_only, stack_cases, urls)
        return _finish(report, rec_only, project_id, run_id, started_wall)

    rec = _Recorder(project_id, run_id, on_step=on_step, total=len(cases))
    if structural and root is not None:
        # Keep the numbering continuous across both passes.
        rec.steps.extend(rec_early.steps)
        rec.console.extend(rec_early.console)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        # Console errors and failed requests are evidence too — a page that renders
        # while throwing is a pass that should not be trusted silently.
        def _console(m):
            if m.type == "error":
                rec.console.append(f"[console.error] {m.text}")
                rec.pending.append(f"console.error: {m.text[:200]}")
            elif m.type == "warning":
                rec.console.append(f"[console.warning] {m.text}")

        def _failed(r):
            rec.console.append(f"[requestfailed] {r.method} {r.url}")
            rec.pending.append(f"request failed: {r.method} {r.url}")

        page.on("console", _console)
        page.on("requestfailed", _failed)
        # An uncaught exception is the clearest evidence a page is broken, and it
        # never shows up in an HTTP status.
        page.on("pageerror", lambda e: rec.pending.append(f"uncaught: {str(e)[:200]}"))

        for case in web:
            started = time.monotonic()

            if case.case_id == "root-001":
                # The one case a frontend can be planned from: the graph holds no
                # SPA routes, because code analysis extracts server-side route
                # tables and a React app has none.
                root_url = _base_for(urls, case)
                # Whether the root belongs to a UI decides what a 404 there means: a
                # missing home page is a defect in a UI and entirely normal for an API,
                # which usually has no route at "/".
                is_ui = bool(urls.get("ui")) and root_url == urls.get("ui")
                try:
                    resp = page.goto(root_url, timeout=_STEP_TIMEOUT_MS,
                                     wait_until="networkidle")
                    png = page.screenshot(full_page=True)
                    code = resp.status if resp else 0
                    # Chromium logs "Failed to load resource: ... status of 404" for the
                    # main document itself. That restates the status already judged
                    # below, so counting it as a separate problem failed every API whose
                    # root is a plain 404.
                    echo = f"status of {code}"
                    problems = [p for p in rec.take_pending()
                                if not (code >= 400 and echo in p)]

                    if code >= 500:
                        rec.add(f"application loads -> {code}", root_url, "failed",
                                started, error=f"HTTP {code}", png=png,
                                case_id=case.case_id)
                    elif is_ui and code >= 400:
                        rec.add(f"application loads -> {code}", root_url, "failed",
                                started, error=f"the UI's root returned HTTP {code}",
                                png=png, case_id=case.case_id)
                    elif problems:
                        # Loaded, but broken in a way no status code shows — the
                        # failure mode a screenshot alone hides.
                        rec.add(f"application loads -> {code}", root_url, "failed",
                                started,
                                error="loaded with errors:\n" + "\n".join(problems[:8]),
                                png=png, case_id=case.case_id)
                    else:
                        note = "" if code < 400 else f" (no route at / — expected for an API)"
                        rec.add(f"application loads -> {code}{note}", root_url,
                                "passed", started, png=png, case_id=case.case_id)
                except Exception as exc:  # noqa: BLE001
                    rec.add("application loads", root_url, "failed", started,
                            error=str(exc), case_id=case.case_id)
                continue

            # One reason, decided by the planner. The runner used to make this call
            # itself with a slightly different rule than the plan did (`"{" in path`
            # versus the plan's regex), so the preview could promise a case that the
            # runner then declined. The plan is what the user was shown, so the plan
            # decides. `why_unrunnable` is the fallback for a plan built by an older
            # server, and keeps this module correct standalone.
            reason = case.skip_reason or plan.why_unrunnable(case)
            if reason:
                target = (case.verifies_eid if case.kind == "smoke"
                          else _url_for(urls, case))
                rec.add(case.name, target, "skipped", started,
                        error=reason, case_id=case.case_id)
                continue

            url = _url_for(urls, case)

            try:
                resp = page.goto(url, timeout=_STEP_TIMEOUT_MS, wait_until="domcontentloaded")
                png = page.screenshot(full_page=True)
                code = resp.status if resp else 0
                ctype = (resp.header_value("content-type") or "") if resp else ""
                if 200 <= code < 400 and "text/html" in ctype and case.path != "/":
                    # A single-page app answers 200 with index.html for EVERY path, so
                    # an API plan aimed at the frontend would report every case as a
                    # pass. Say what is actually wrong instead.
                    rec.add(f"GET {case.path} -> {code} (HTML)", url, "failed", started,
                            error=("expected an API response, got HTML. This URL looks "
                                   "like the frontend — point the run at the API, or "
                                   "this route no longer exists and the SPA fallback "
                                   "answered."),
                            png=png, case_id=case.case_id)
                elif 200 <= code < 400:
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


        browser.close()

    if stack_cases:
        _run_stack(rec, stack_cases, urls)

    return _finish(report, rec, project_id, run_id, started_wall)

"""The QA run queue.

Before this, a run had no state at all. `report.json` is written LAST and its existence
is the finished signal (`evidence.py`), `Report.status` has no `queued`/`running` member,
and the DynamoDB index row is written only after a run completes. So a run in progress
was invisible: `GET /results/{project}` lists S3 prefixes, and a prefix without a
`report.json` is indistinguishable from one that never existed.

That is fine while a run happens inside the request that asked for it. It stops being
fine the moment the thing executing the run is somewhere else — a self-hosted runner on a
developer machine — because "started" and "finished" become separate events minutes apart.

Deliberately reuses the existing `test-results` table (PK `testRunId`, SK `projectId`)
rather than adding one. `routers/qa.py` already scans it, runs are rare, and a second
table would mean two places to look for the same run.

Lifecycle:

    queued ──claim──> claimed ──heartbeat──> running ──finish──> passed
                         │                      │                failed
                         └──────reap────────────┴──────────────> unavailable
                                                                 abandoned
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from src.database import dynamo_client as db

log = logging.getLogger(__name__)

TABLE = "test-results"

# Written on every row so the queue can be told apart from the TestGenerationAgent rows
# that share this table (routers/qa.py:77 already filters on a type marker).
KIND = "qatest"

#: Marker for the one-row-per-runner liveness records. A DIFFERENT type from KIND so
#: they never appear in a project's run list or get picked up by the reaper.
RUNNER_KIND = "qa-runner"

#: Sort key for runner rows. The table is PK testRunId / SK projectId, and a runner
#: belongs to no project, so it needs a sentinel.
RUNNER_SK = "_runners"

QUEUED = "queued"
CLAIMED = "claimed"
RUNNING = "running"
ABANDONED = "abandoned"

#: States a runner may still be working on. Anything else is terminal.
LIVE = (QUEUED, CLAIMED, RUNNING)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """Same shape as qatest.service.new_run_id, so ids are interchangeable."""
    return uuid.uuid4().hex[:8]


def enqueue(project_id: str, app_url: str = "", ran_by: str = "",
            exploratory: bool = False, run_id: str | None = None) -> dict:
    """Create a queued run and return the row. Returns immediately — nothing executes."""
    row = {
        "testRunId": run_id or new_run_id(),
        "projectId": project_id,
        "type": KIND,
        "status": QUEUED,
        "appUrl": app_url,
        "userId": ran_by,
        "exploratory": bool(exploratory),
        "createdAt": _now(),
        "updatedAt": _now(),
        "runner": "",
        "phase": "",
        "totalPassed": 0,
        "totalFailed": 0,
        "totalSkipped": 0,
    }
    db.put_item(TABLE, row)
    log.info("QA queue: enqueued %s for project %s", row["testRunId"], project_id)
    return row


def list_for_project(project_id: str, limit: int = 200) -> list[dict]:
    """Every queued/live/terminal run this queue knows about, newest first.

    The Results tab merges this with the S3 listing: S3 knows about finished runs
    (including ones executed straight from the CLI, which never touch this queue), and
    this knows about runs that have not finished yet.
    """
    try:
        rows = db.scan_items(TABLE, limit=limit) or []
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: scan failed: %s", exc)
        return []
    mine = [r for r in rows
            if r.get("projectId") == project_id and r.get("type") == KIND]
    return sorted(mine, key=lambda r: str(r.get("createdAt", "")), reverse=True)


def touch_runner(runner: str) -> None:
    """Record that `runner` is alive, right now.

    Called on every poll, including the ones that find nothing to do — and that is the
    entire point. Liveness was originally derived from claimed runs, which deadlocked:
    a runner that had never claimed anything looked offline, so `canRun` stayed false,
    so the button stayed disabled, so nothing was ever queued, so it never claimed.

    Polling IS the liveness signal, because polling is what the runner actually does.
    """
    if not runner:
        return
    try:
        db.put_item(TABLE, {
            "testRunId": f"runner:{runner}",
            "projectId": RUNNER_SK,
            "type": RUNNER_KIND,
            "runner": runner,
            "updatedAt": _now(),
        })
    except Exception as exc:                                  # noqa: BLE001
        # Never fail a poll over this. The worst case is a button that looks disabled.
        log.debug("QA queue: could not record runner %s: %s", runner, exc)


def online_runners(stale_after_s: int = 90) -> list[dict]:
    """Runners that have polled within `stale_after_s`, newest first."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
    try:
        rows = db.scan_items(TABLE, limit=500) or []
    except Exception:                                         # noqa: BLE001
        return []

    out = []
    for row in rows:
        if row.get("type") != RUNNER_KIND:
            continue
        stamp = str(row.get("updatedAt") or "")
        try:
            seen = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= cutoff:
            out.append({"name": row.get("runner", ""), "lastSeen": stamp})
    return sorted(out, key=lambda r: r["lastSeen"], reverse=True)


def claim(runner: str) -> dict | None:
    """Take the oldest queued run, or None.

    The claim is a CONDITIONAL write on `status == "queued"`. A read-then-write would
    race: two runners polling a few hundred milliseconds apart would both see the row as
    queued and both execute the same run, doubling the podman containers on fixed host
    ports and writing two sets of evidence over each other.
    """
    try:
        rows = db.scan_items(TABLE, limit=200) or []
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: scan failed while claiming: %s", exc)
        return None

    waiting = sorted(
        (r for r in rows if r.get("type") == KIND and r.get("status") == QUEUED),
        key=lambda r: str(r.get("createdAt", "")))

    for row in waiting:
        key = {"testRunId": row["testRunId"], "projectId": row["projectId"]}
        won = db.update_item_if(TABLE, key, {
            "status": CLAIMED,
            "runner": runner,
            "claimedAt": _now(),
            "updatedAt": _now(),
        }, expect={"status": QUEUED})
        if won:
            log.info("QA queue: %s claimed %s", runner, row["testRunId"])
            return won
        # Lost the race to another runner — try the next one rather than giving up.
    return None


def heartbeat(run_id: str, project_id: str, phase: str = "",
              runner: str = "", counts: dict | None = None) -> dict | None:
    """Mark progress. Also what keeps `reap` from declaring the run dead.

    `counts` carries pass/fail/skip AS THEY HAPPEN. Without them a running remote run
    can only say "running", and a twelve-case run spends a minute looking identical to
    a stuck one. They are overwritten by `finish` with the report's own totals.
    """
    updates = {"status": RUNNING, "updatedAt": _now()}
    if phase:
        updates["phase"] = phase
    if runner:
        updates["runner"] = runner
    # Applied only when the runner actually knows the plan size. The body model has
    # zero defaults for all four fields, so a heartbeat that carries no real counts
    # would otherwise OVERWRITE good ones with zeros — a progress bar that walks
    # forward and then snaps back to 0 of 0 mid-run.
    if counts and int(counts.get("totalCases") or 0) > 0:
        for key in ("totalPassed", "totalFailed", "totalSkipped", "totalCases"):
            if key in counts:
                updates[key] = int(counts[key] or 0)
    try:
        return db.update_item(TABLE, {"testRunId": run_id, "projectId": project_id},
                              updates)
    except Exception as exc:                                  # noqa: BLE001
        # A dropped heartbeat must never kill a run that is otherwise fine.
        log.warning("QA queue: heartbeat for %s failed: %s", run_id, exc)
        return None


def finish(run_id: str, project_id: str, report: dict) -> dict | None:
    """Record the terminal state from the runner's report."""
    updates = {
        "status": report.get("status") or "failed",
        "phase": "done",
        "updatedAt": _now(),
        "completedAt": report.get("completedAt") or _now(),
        "appUrl": report.get("appUrl", ""),
        "totalPassed": int(report.get("totalPassed") or 0),
        "totalFailed": int(report.get("totalFailed") or 0),
        "totalSkipped": int(report.get("totalSkipped") or 0),
        "reason": (report.get("reason") or "")[:400],
    }
    try:
        return db.update_item(TABLE, {"testRunId": run_id, "projectId": project_id},
                              updates)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: finish for %s failed: %s", run_id, exc)
        return None


def reap(stale_after_s: int = 900) -> int:
    """Mark runs whose runner stopped talking as `abandoned`. Returns how many.

    Without this the Results tab lies. A laptop that sleeps or an agent that is killed
    leaves a row at `claimed`/`running` for ever, and the S3 prefix it left behind has no
    `report.json` — so every reader treats the evidence as nonexistent while the queue
    insists the run is still going.

    `stale_after_s` is generous on purpose: a real run can sit quiet inside a single
    20-second navigation, and a slow emulator pull is minutes.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
    try:
        rows = db.scan_items(TABLE, limit=500) or []
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: scan failed while reaping: %s", exc)
        return 0

    reaped = 0
    for row in rows:
        if row.get("type") != KIND or row.get("status") not in LIVE:
            continue
        stamp = str(row.get("updatedAt") or row.get("createdAt") or "")
        try:
            seen = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= cutoff:
            continue
        key = {"testRunId": row["testRunId"], "projectId": row["projectId"]}
        if db.update_item_if(TABLE, key, {
            "status": ABANDONED,
            "phase": "abandoned",
            "reason": f"the runner stopped reporting for over {stale_after_s}s",
            "updatedAt": _now(),
        }, expect={"status": row["status"]}):
            reaped += 1
            log.warning("QA queue: reaped %s (last seen %s)", row["testRunId"], stamp)
    return reaped

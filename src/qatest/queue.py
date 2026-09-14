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

from boto3.dynamodb.conditions import Attr

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

#: One row holding every runner's last-known state, so reading "who is online and what
#: is running on them" is a GetItem instead of a full table scan. Each runner writes a
#: single top-level attribute named after itself, so concurrent writers never collide.
#: The per-runner rows remain the record of truth; this is a read index and every
#: reader falls back to the scan when it is missing.
RUNNER_INDEX_ID = "runners:_index"

#: How long a runner may go quiet before its reported containers are last-known rather
#: than current. A sleeping laptop must not leave a panel claiming four are running.
RUNNER_STALE_S = 90

#: Row counting polls that failed to authenticate. A rejected poll carries no identity
#: by definition — that is what rejected means — so it cannot be attributed to a runner
#: row. But it can be COUNTED, and the count is the difference between "no runner is
#: connected" and "a runner is connected and its key is being refused". Those look
#: identical in the UI today, and telling them apart took four days of log archaeology.
UNAUTHORIZED_ID = "runners:_unauthorized"

#: How long a rejected poll stays interesting. Longer than the runner staleness window,
#: because the point is to explain an ABSENT runner — the evidence has to outlive it.
UNAUTHORIZED_WINDOW_S = 900

QUEUED = "queued"
CLAIMED = "claimed"
RUNNING = "running"
ABANDONED = "abandoned"

#: States a runner may still be working on. Anything else is terminal.
LIVE = (QUEUED, CLAIMED, RUNNING)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope() -> str:
    """Which deployment this queue row belongs to.

    This table is addressed by name, not by endpoint, so every Aura process pointed at
    the same AWS account shares one queue — and a developer running the API on their
    laptop shares it with the deployed environment. That is not hypothetical: a run
    queued from the dev UI was claimed by a localhost backend, executed against a
    laptop's filesystem, and wrote its report into dev's S3. The reader saw a macOS path
    in a browser pointed at AWS and had no way to explain it.

    Derived from settings that already exist and already differ — `deployment_env` is
    "local" on a laptop and "ecs" in every deployed task — so nothing new has to be
    configured for the isolation to take effect.
    """
    try:
        from src.config_settings import get_settings
        s = get_settings()
        return f"{s.deployment_env}/{s.app_env}"
    except Exception:                                         # noqa: BLE001
        # A scope that cannot be read must not silently become the empty string, which
        # would match nothing and stall the queue in a way that looks like "no runner".
        return "unknown/unknown"


def _runner_key(runner: str) -> dict:
    return {"testRunId": f"runner:{runner}", "projectId": RUNNER_SK}


def record_unauthorized(hint: str = "") -> None:
    """Note that someone tried to poll with a credential this server refused.

    Best-effort and deliberately cheap: it rides the rejection path, which an
    unauthenticated caller controls the rate of, so it must never fail a request and
    must never grow. One row, two attributes, last-write-wins.

    `hint` is the key's last few characters when available — enough to tell two runners
    apart when diagnosing, never enough to reconstruct the credential.
    """
    try:
        db.update_item(TABLE, {"testRunId": UNAUTHORIZED_ID, "projectId": RUNNER_SK},
                       {"type": RUNNER_KIND, "runner": UNAUTHORIZED_ID,
                        "lastUnauthorizedAt": _now(),
                        "lastUnauthorizedHint": str(hint or "")[:8]})
    except Exception as exc:                                  # noqa: BLE001
        log.debug("QA queue: could not record an unauthorized poll: %s", exc)


def unauthorized_recently(window_s: int = UNAUTHORIZED_WINDOW_S) -> dict:
    """Whether a refused poll happened recently, for the UI to explain an absent runner."""
    try:
        row = db.get_item(TABLE, {"testRunId": UNAUTHORIZED_ID,
                                  "projectId": RUNNER_SK}) or {}
    except Exception as exc:                                  # noqa: BLE001
        log.debug("QA queue: could not read the unauthorized marker: %s", exc)
        return {}
    at = str(row.get("lastUnauthorizedAt") or "")
    if not at or _age_seconds(at) > window_s:
        return {}
    return {"at": at, "hint": str(row.get("lastUnauthorizedHint") or "")}


def _age_seconds(stamp: str) -> float:
    """Seconds since an ISO timestamp; +inf when it cannot be read."""
    try:
        seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - seen).total_seconds()
    except Exception:                                         # noqa: BLE001
        return float("inf")


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
        # Only a runner reached through THIS deployment may claim it. See `_scope`.
        "scope": _scope(),
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
        # Filtered for the same reason as `claim` — an in-flight run was invisible to
        # the Results tab once the table passed the limit, which reads as a run that
        # was never queued at all.
        rows = db.scan_items(
            TABLE,
            filter_expr=Attr("projectId").eq(project_id) & Attr("type").eq(KIND),
            limit=limit) or []
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: scan failed: %s", exc)
        return []
    return sorted(rows, key=lambda r: str(r.get("createdAt", "")), reverse=True)


#: Who a runner belongs to and what its operator calls the machine. Derived by the API
#: from the gateway key and the request headers — NEVER from the agent's JSON body, which
#: is why these are kept apart from `_STATE_FIELDS`.
_IDENTITY_FIELDS = ("owner", "ownerId", "machine")


def _identity_updates(identity: dict | None) -> dict:
    """The identity attributes worth writing, empty when there is nothing to say.

    An absent or blank value is omitted rather than written as "": these ride on calls
    that fire every few seconds, and writing an empty machine name over a good one would
    make the label flicker for readers polling at the same time.
    """
    return {k: str(identity.get(k) or "")
            for k in _IDENTITY_FIELDS if (identity or {}).get(k)}


def _run_identity_updates(identity: dict | None) -> dict:
    """The same provenance, spelled for a RUN row rather than a runner row.

    Prefixed, because a run row already has an `owner`-shaped concept of its own — the
    `userId` of whoever queued it — and that is a different person from whoever's laptop
    ended up executing it.
    """
    out = {}
    if (identity or {}).get("machine"):
        out["runnerMachine"] = str(identity["machine"])
    if (identity or {}).get("owner"):
        out["runnerOwner"] = str(identity["owner"])
    return out


def touch_runner(runner: str, identity: dict | None = None) -> None:
    """Record that `runner` is alive, right now.

    Called on every poll, including the ones that find nothing to do — and that is the
    entire point. Liveness was originally derived from claimed runs, which deadlocked:
    a runner that had never claimed anything looked offline, so `canRun` stayed false,
    so the button stayed disabled, so nothing was ever queued, so it never claimed.

    Polling IS the liveness signal, because polling is what the runner actually does.

    The identity is written here rather than in `record_runner_state` because this call
    is the one every runner makes — a protocol-1 agent never reports state at all, and
    would otherwise stay unlabelled forever.
    """
    if not runner:
        return
    try:
        # update_item, NOT put_item: the row also carries the runner's reported state
        # (podman readiness, its Floci containers, any pending command), written by
        # `record_runner_state` every ~15s. put_item REPLACES the item, so a poll five
        # seconds later would erase all of it. UpdateItem creates the row when absent,
        # so this is identical for a runner that has never reported state.
        db.update_item(TABLE, _runner_key(runner), {
            "type": RUNNER_KIND,
            "runner": runner,
            "updatedAt": _now(),
            **_identity_updates(identity),
        })
    except Exception as exc:                                  # noqa: BLE001
        # Never fail a poll over this. The worst case is a button that looks disabled.
        log.debug("QA queue: could not record runner %s: %s", runner, exc)


def online_runners(stale_after_s: int = RUNNER_STALE_S) -> list[dict]:
    """Runners that have polled within `stale_after_s`, newest first.

    Reads the aggregate index, so `/capabilities` — called on every page load — costs a
    GetItem rather than the 500-item scan it used to. `list_runner_state` owns the
    index-then-scan fallback; this stays a thin projection of it so the two can never
    disagree about who is online.
    """
    return [{"name": r["name"], "lastSeen": r["lastSeen"]}
            for r in list_runner_state(stale_after_s) if r["online"] and r["name"]]


def claim(runner: str, identity: dict | None = None) -> dict | None:
    """Take the oldest queued run, or None.

    The claim is a CONDITIONAL write on `status == "queued"`. A read-then-write would
    race: two runners polling a few hundred milliseconds apart would both see the row as
    queued and both execute the same run, doubling the podman containers on fixed host
    ports and writing two sets of evidence over each other.

    The machine and owner are STAMPED on the run row, not looked up later against the
    runner list: a finished or abandoned run has to keep saying where it executed long
    after that machine has gone offline and aged out of the index.

    Scoped to this deployment. A row carrying a different scope — or none, which means it
    predates scoping — is left alone rather than claimed. There is deliberately no
    `scope.not_exists()` fallback: that is precisely the cross-environment claim this
    exists to prevent. Pre-existing queued rows are retired by `reap` within its window
    instead, which is minutes for a queue whose runs last seconds.
    """
    try:
        # FILTERED, not sliced. `test-results` holds every run this deployment has ever
        # made — 196 rows from the old execution agent alone on one dev machine — and an
        # unfiltered scan spends its whole limit on them, so the queued run falls outside
        # the window and the runner polls forever against a queue that looks empty. The
        # limit counts rows that MATCH, so filtering server-side makes the budget mean
        # what it says and reads less.
        rows = db.scan_items(
            TABLE,
            filter_expr=(Attr("type").eq(KIND) & Attr("status").eq(QUEUED)
                         & Attr("scope").eq(_scope())),
            limit=200) or []
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: scan failed while claiming: %s", exc)
        return None

    waiting = sorted(rows, key=lambda r: str(r.get("createdAt", "")))

    for row in waiting:
        key = {"testRunId": row["testRunId"], "projectId": row["projectId"]}
        won = db.update_item_if(TABLE, key, {
            "status": CLAIMED,
            "runner": runner,
            "claimedAt": _now(),
            "updatedAt": _now(),
            **_run_identity_updates(identity),
        }, expect={"status": QUEUED})
        if won:
            log.info("QA queue: %s claimed %s", runner, row["testRunId"])
            return won
        # Lost the race to another runner — try the next one rather than giving up.
    return None


def heartbeat(run_id: str, project_id: str, phase: str = "",
              runner: str = "", counts: dict | None = None,
              identity: dict | None = None) -> dict | None:
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
    # Re-stamped on every beat as well as at claim time. A run already in flight when
    # this shipped was claimed by the old code path and carries no machine, so without
    # this it would stay anonymous for its whole life.
    updates.update(_run_identity_updates(identity))
    # Applied only when the runner actually knows the plan size. The body model has
    # zero defaults for all four fields, so a heartbeat that carries no real counts
    # would otherwise OVERWRITE good ones with zeros — a progress bar that walks
    # forward and then snaps back to 0 of 0 mid-run.
    if counts and int(counts.get("totalCases") or 0) > 0:
        for key in ("totalPassed", "totalFailed", "totalSkipped",
                    "totalUnemulated", "totalCases", "stepIndex"):
            if key in counts:
                updates[key] = int(counts[key] or 0)
    if counts:
        # What the run is doing right now, in words — "aws emulator ready on :4566"
        # beats the bare phase name "emulator".
        if counts.get("phaseDetail"):
            updates["phaseDetail"] = str(counts["phaseDetail"])[:300]
        # The Floci containers serving THIS run. They belong to the run, not the
        # machine, so they ride the heartbeat rather than the runner-state POST: the
        # run row already exists and the heartbeat is already rate-limited.
        # The runner sends its WHOLE console each beat and this stores it verbatim.
        #
        # It was a read-modify-write append, and it lost almost everything: a step
        # event beats immediately, so several heartbeats are in flight at once, each
        # reads the row before the previous one landed, and each write clobbers the
        # last. A 12-case run ended with a single line. The runner already holds the
        # history in memory, so having it send the whole thing removes the read and
        # the race with it — the same last-write-wins shape `emulators` uses.
        incoming = counts.get("events")
        if isinstance(incoming, list) and incoming:
            updates["activity"] = [
                {"at": str(e.get("at") or "")[:32],
                 "phase": str(e.get("phase") or "")[:24],
                 "text": str(e.get("text") or "")[:200]}
                for e in incoming[-ACTIVITY_MAX:] if isinstance(e, dict)]

        emus = counts.get("emulators")
        if isinstance(emus, list):
            updates["emulators"] = [
                {k: (str(v)[:400] if k == "error" else v)
                 for k, v in (e or {}).items()
                 if k in ("cloud", "image", "digest", "port", "container",
                          "started", "stopped", "error")}
                for e in emus[:8]
            ]
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
        "totalUnemulated": int(report.get("totalUnemulated") or 0),
        "reason": (report.get("reason") or "")[:400],
    }
    # Kept so a finished row can still answer "how many were planned". Harmless while
    # a finished run drops out of LIVE and stops being listed, but `/runs/{id}/progress`
    # reads this row directly and would otherwise report a 100%-complete run as 0 of 0.
    planned = int(report.get("planTotal") or 0) or len(report.get("cases") or [])
    if planned:
        updates["totalCases"] = planned
    try:
        return db.update_item(TABLE, {"testRunId": run_id, "projectId": project_id},
                              updates)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: finish for %s failed: %s", run_id, exc)
        return None


CANCELLED = "cancelled"


def cancel(run_id: str, project_id: str, actor: str) -> dict | None:
    """Stop a run from the UI. Returns the row, or None if it was not cancellable.

    A conditional write per live status, for the same reason `claim` uses one: a run
    that finishes between the read and the write must keep its real result rather than
    have it overwritten with "cancelled".

    This does NOT reach the runner — it polls, holds no inbound port, and may be a
    laptop that is asleep. What it does is stop the queue lying: the run leaves LIVE,
    so the Results tab stops showing something that will never finish and a project
    delete is no longer blocked by it. A runner that later wakes and posts a result
    for a cancelled run writes its evidence to S3 as usual; the queue row stays
    cancelled, which is the honest record of what the operator asked for.
    """
    for status in LIVE:
        won = db.update_item_if(
            TABLE, {"testRunId": run_id, "projectId": project_id},
            {"status": CANCELLED, "phase": CANCELLED, "updatedAt": _now(),
             "completedAt": _now(),
             "reason": f"cancelled by {actor}"},
            expect={"status": status})
        if won:
            log.info("QA queue: %s cancelled %s (was %s)", actor, run_id, status)
            return won
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
        # Both kinds: the runs to reap, and the runner rows `_prune_runner_index` needs.
        rows = db.scan_items(
            TABLE, filter_expr=Attr("type").is_in([KIND, RUNNER_KIND]), limit=500) or []
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
            # The emulators this row lists were running when the runner went quiet.
            # Without this the run's panel keeps showing containers that are almost
            # certainly gone — a phantom that reads as "still working".
            "emulatorsStale": True,
            "updatedAt": _now(),
        }, expect={"status": row["status"]}):
            reaped += 1
            log.warning("QA queue: reaped %s (last seen %s)", row["testRunId"], stamp)

    _prune_runner_index(rows)
    return reaped


def _prune_runner_index(rows: list[dict]) -> None:
    """Drop runners from the index that no longer have a row.

    Runs inside `reap`, which is already scheduled, so the index cannot grow for ever
    as developer machines come and go.
    """
    live = {r.get("runner") for r in rows
            if r.get("type") == RUNNER_KIND and r.get("testRunId") != RUNNER_INDEX_ID}
    try:
        index = db.get_item(TABLE, {"testRunId": RUNNER_INDEX_ID,
                                    "projectId": RUNNER_SK}) or {}
        stale = [k for k, v in index.items()
                 if k.startswith("r_") and isinstance(v, dict)
                 and v.get("runner") not in live]
        if not stale:
            return
        kept = {k: v for k, v in index.items() if k not in stale}
        kept.update({"testRunId": RUNNER_INDEX_ID, "projectId": RUNNER_SK,
                     "type": RUNNER_KIND})
        db.put_item(TABLE, kept)
    except Exception as exc:                                  # noqa: BLE001
        log.debug("QA queue: could not prune the runner index: %s", exc)


# ── Runner state: what is running on the developer's machine ────────────────────
#
# The API runs on Fargate and can never see a developer's podman. Everything below is
# reported BY the runner and read back here, so "show Floci running on your machine"
# is a question the server can only answer second-hand — and must say so when the
# report is stale.

#: Attributes a runner reports about itself. Whitelisted rather than stored wholesale
#: so a future agent cannot grow the row without this module agreeing.
_STATE_FIELDS = ("podman", "browser", "podmanVersion", "browserVersion",
                 "os", "busyRunId", "protocol")

#: Findings kept per runner. Every runner's state shares ONE DynamoDB item under a
#: 400 KB limit, so this is a real bound rather than tidiness. Re-capped here as well
#: as on the agent: the agent's own limit is not something to trust.
HEALTH_MAX_FINDINGS = 6
HEALTH_MAX_TEXT = 400

#: A developer's machine runs their employer's containers. Only Aura's own are ever
#: reported, and the agent enforces the same rule on its side.
MANAGED_PREFIX = "aura-qa-"

#: Cap so the 400 KB item limit is unreachable no matter how many containers exist.
_MAX_CONTAINERS = 25


def _clean_containers(containers: list | None) -> list[dict]:
    out: list[dict] = []
    for c in (containers or [])[:_MAX_CONTAINERS]:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        out.append({
            "id": str(c.get("id") or "")[:64],
            "name": name[:128],
            "image": str(c.get("image") or "")[:200],
            "status": str(c.get("status") or "")[:80],
            "ports": str(c.get("ports") or "")[:120],
            "createdAt": str(c.get("createdAt") or "")[:40],
            "cloud": str(c.get("cloud") or "")[:20],
            "managed": name.startswith(MANAGED_PREFIX),
        })
    return out


def _text(value, limit: int = HEALTH_MAX_TEXT) -> str:
    """A string safe to store and render. Written by a runner, read by everyone."""
    out = str(value or "")[:limit]
    return "".join(ch for ch in out if ch == "\n" or ch >= " ")


def _clean_health(health) -> dict | None:
    """What the runner says is wrong with it, bounded and stripped.

    This is the one new thing a runner can put in front of other people's eyes, so it
    is capped and de-controlled here rather than trusted.
    """
    if not isinstance(health, dict):
        return None
    findings = []
    for item in (health.get("findings") or [])[:HEALTH_MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        findings.append({
            "check": _text(item.get("check"), 60),
            "severity": _text(item.get("severity"), 12) or "blocks",
            "title": _text(item.get("title"), 120),
            "detail": _text(item.get("detail")),
            "remedy": [_text(r, 160) for r in (item.get("remedy") or [])[:2]],
        })
    return {"ok": bool(health.get("ok", True)),
            "platform": _text(health.get("platform"), 40),
            "checkedAt": _text(health.get("checkedAt"), 40),
            "findings": findings}


def _clean_setup(setup) -> dict | None:
    """Progress of a setup the USER started on their machine.

    The server never causes one — it can only be told about it — so this is a display
    field and nothing branches on it.
    """
    if not isinstance(setup, dict):
        return None
    log = [{"at": _text(e.get("at"), 40), "text": _text(e.get("text"), 200)}
           for e in (setup.get("log") or [])[-60:] if isinstance(e, dict)]
    return {"active": bool(setup.get("active")),
            "step": _text(setup.get("step"), 120),
            "index": int(setup.get("index") or 0),
            "total": int(setup.get("total") or 0),
            "log": log}


def record_runner_state(runner: str, state: dict,
                        identity: dict | None = None) -> dict:
    """Store what a runner just said about itself. Returns any pending command.

    Writes the per-runner row AND the aggregate index entry. The index is what makes a
    polled panel affordable: `online_runners` and the runners endpoint then read one
    item instead of scanning the table on every tick.
    """
    if not runner:
        return {}
    containers = _clean_containers(state.get("containers"))
    payload = {k: state[k] for k in _STATE_FIELDS if k in state}
    health = _clean_health(state.get("health"))
    if health:
        payload["health"] = health
    setup = _clean_setup(state.get("setup"))
    if setup:
        payload["setup"] = setup
    payload.update({
        "type": RUNNER_KIND,
        "runner": runner,
        "containers": containers,
        "containersAt": _now(),
        "updatedAt": _now(),
        # Applied AFTER the whitelisted body fields, so a runner cannot claim to be
        # owned by someone else by putting `owner` in its own state report.
        **_identity_updates(identity),
    })
    try:
        db.update_item(TABLE, _runner_key(runner), payload)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: could not record state for %s: %s", runner, exc)

    _index_runner(runner, payload)
    return runner_state(runner) or {}


def _index_runner(runner: str, payload: dict) -> None:
    """Mirror one runner into the aggregate index row.

    One attribute per runner, so two runners writing at the same moment touch disjoint
    attributes and cannot clobber each other. A `/` in a runner name is fine — the
    Dynamo client addresses attributes by name, not by path expression.
    """
    try:
        db.update_item(TABLE, {"testRunId": RUNNER_INDEX_ID, "projectId": RUNNER_SK},
                       {"type": RUNNER_KIND,
                        _index_attr(runner): {
                            "runner": runner,
                            "updatedAt": payload.get("updatedAt") or _now(),
                            "podman": bool(payload.get("podman", False)),
                            "browser": bool(payload.get("browser", False)),
                            # Carried through, not just stored on the per-runner row:
                            # readers go through the index, so anything missing here is
                            # invisible in the UI even though it was reported correctly.
                            "os": payload.get("os") or "",
                            "owner": payload.get("owner") or "",
                            "ownerId": payload.get("ownerId") or "",
                            "machine": payload.get("machine") or "",
                            "health": payload.get("health") or {},
                            "setup": payload.get("setup") or {},
                            "podmanVersion": payload.get("podmanVersion") or "",
                            "browserVersion": payload.get("browserVersion") or "",
                            "busyRunId": payload.get("busyRunId") or "",
                            "protocol": int(payload.get("protocol") or 1),
                            "containers": payload.get("containers") or [],
                            "containersAt": payload.get("containersAt") or "",
                        }})
    except Exception as exc:                                  # noqa: BLE001
        # The index is a cache. Losing a write costs a scan, not correctness.
        log.debug("QA queue: runner index write failed for %s: %s", runner, exc)


def _index_attr(runner: str) -> str:
    """Attribute name for a runner inside the index row."""
    return f"r_{runner}"


def runner_state(runner: str) -> dict | None:
    """One runner's row, by key. No scan."""
    try:
        return db.get_item(TABLE, _runner_key(runner))
    except Exception as exc:                                  # noqa: BLE001
        log.debug("QA queue: could not read runner %s: %s", runner, exc)
        return None


def list_runner_state(stale_after_s: int = RUNNER_STALE_S) -> list[dict]:
    """Every known runner with its last-known state, newest first.

    Reads the aggregate index (one GetItem) and falls back to the scan the moment the
    index is absent — first deploy, or a lost row — so this can never be the reason the
    panel is empty.
    """
    rows: list[dict] = []
    try:
        index = db.get_item(TABLE, {"testRunId": RUNNER_INDEX_ID,
                                    "projectId": RUNNER_SK}) or {}
        rows = [v for k, v in index.items()
                if k.startswith("r_") and isinstance(v, dict) and v.get("runner")]
    except Exception as exc:                                  # noqa: BLE001
        log.debug("QA queue: runner index unreadable: %s", exc)

    if not rows:
        try:
            rows = [r for r in db.scan_items(
                        TABLE, filter_expr=Attr("type").eq(RUNNER_KIND), limit=500)
                    if r.get("runner") and r.get("testRunId") != RUNNER_INDEX_ID]
        except Exception as exc:                              # noqa: BLE001
            log.warning("QA queue: could not list runners: %s", exc)
            return []

    out = []
    for row in rows:
        age = _age_seconds(row.get("updatedAt", ""))
        out.append({
            "name": row.get("runner", ""),
            "lastSeen": row.get("updatedAt", ""),
            "online": age <= stale_after_s,
            # Stale means the containers listed are the LAST KNOWN set, not the
            # current one. The UI has to say which it is showing.
            "stale": age > stale_after_s,
            "podman": bool(row.get("podman", False)),
            "browser": bool(row.get("browser", False)),
            "podmanVersion": row.get("podmanVersion", ""),
            "browserVersion": row.get("browserVersion", ""),
            "os": row.get("os", ""),
            # Who this machine belongs to and what they call it. Absent until the
            # runner's first state report after an upgrade, so every reader falls back
            # to `name`.
            "owner": row.get("owner", ""),
            "ownerId": row.get("ownerId", ""),
            "machine": row.get("machine", ""),
            "busyRunId": row.get("busyRunId", ""),
            "protocol": int(row.get("protocol") or 1),
            # protocol 1 agents never report state, so an empty container list from
            # one means "cannot tell", not "nothing running".
            "reportsState": bool(row.get("containersAt")),
            "containers": list(row.get("containers") or []),
            "containersAt": row.get("containersAt", ""),
            "health": dict(row.get("health") or {}),
            "setup": dict(row.get("setup") or {}),
        })
    return sorted(out, key=lambda r: r.get("lastSeen") or "", reverse=True)


def progress(run_id: str, project_id: str) -> dict | None:
    """One run's live counters, by key.

    Exists so a 2-second progress poll is a GetItem. `list_for_project` scans, and a
    polled endpoint backed by a scan is how a table gets hot.
    """
    try:
        row = db.get_item(TABLE, {"testRunId": run_id, "projectId": project_id})
    except Exception as exc:                                  # noqa: BLE001
        log.warning("QA queue: progress read for %s failed: %s", run_id, exc)
        return None
    if not row:
        return None
    total = int(row.get("totalCases") or 0)
    done = (int(row.get("totalPassed") or 0) + int(row.get("totalFailed") or 0)
            + int(row.get("totalSkipped") or 0) + int(row.get("totalUnemulated") or 0))
    return {
        "runId": run_id,
        "projectId": project_id,
        "status": row.get("status", ""),
        "phase": row.get("phase", ""),
        "phaseDetail": row.get("phaseDetail", ""),
        # Why it ended, when it ended badly — "cancelled by alice", or the reaper's
        # "the runner stopped reporting". A status with no reason makes the reader
        # guess.
        "reason": row.get("reason", ""),
        "runner": row.get("runner", ""),
        "runnerMachine": row.get("runnerMachine", ""),
        "runnerOwner": row.get("runnerOwner", ""),
        "totalPassed": int(row.get("totalPassed") or 0),
        "totalFailed": int(row.get("totalFailed") or 0),
        "totalSkipped": int(row.get("totalSkipped") or 0),
        "totalUnemulated": int(row.get("totalUnemulated") or 0),
        "totalCases": total,
        "done": min(done, total) if total else done,
        # None, never 0. A queued run that has not been claimed does not know its plan
        # size yet, and a bar sitting at 0% says something false about a run that has
        # not started.
        "pct": (max(0, min(100, round(done / total * 100))) if total else None),
        "emulators": list(row.get("emulators") or []),
        "emulatorsStale": bool(row.get("emulatorsStale", False)),
        "activity": list(row.get("activity") or []),
        "updatedAt": row.get("updatedAt", ""),
    }


# ── Commands: asking a runner to do something on its next poll ─────────────────
#
# The runner has no inbound port by design, so the server cannot call it. A command is
# left on the runner's row and collected on its next state POST — latency is one poll,
# which is why the UI must say "fetch", never "stream".

#: How much of a run's own console is kept. Enough to see the whole of a normal run,
#: bounded so the row cannot grow without limit.
ACTIVITY_MAX = 80

#: A repeat request inside this window returns the command already in flight rather
#: than queueing a second one.
COMMAND_DEDUPE_S = 30


def request_command(runner: str, kind: str, container: str, tail: int = 200) -> dict:
    """Queue one command for a runner. Returns {commandId, status}."""
    row = runner_state(runner) or {}
    existing = row.get("cmdId")
    if existing and not row.get("cmdResultAt") \
            and _age_seconds(row.get("cmdRequestedAt", "")) < COMMAND_DEDUPE_S:
        return {"commandId": existing, "status": "pending", "deduped": True}

    command_id = f"cmd-{uuid.uuid4().hex[:12]}"
    db.update_item(TABLE, _runner_key(runner), {
        "cmdId": command_id,
        "cmdKind": kind,
        "cmdContainer": container,
        "cmdTail": int(tail),
        "cmdRequestedAt": _now(),
        # Flat attributes rather than a nested map: update_item builds only top-level
        # SETs, and keeping them flat also means the state writer and this writer touch
        # disjoint attributes and cannot clobber each other.
        "cmdResultKey": "",
        "cmdResultAt": "",
        "cmdError": "",
    })
    return {"commandId": command_id, "status": "pending"}


def take_command(runner: str) -> dict | None:
    """Hand a runner its pending command, exactly once.

    The conditional write is what makes it exactly once: two polls racing cannot both
    win, so a command is never dispatched twice.
    """
    row = runner_state(runner) or {}
    command_id = row.get("cmdId")
    if not command_id or row.get("cmdTakenAt"):
        return None
    try:
        db.update_item_if(TABLE, _runner_key(runner),
                          {"cmdTakenAt": _now()}, expect={"cmdId": command_id})
    except Exception:                                         # noqa: BLE001 — lost the race
        return None
    return {"id": command_id, "kind": row.get("cmdKind", "logs"),
            "container": row.get("cmdContainer", ""),
            "tail": int(row.get("cmdTail") or 200)}


def record_command_result(runner: str, command_id: str, key: str = "",
                          error: str = "") -> None:
    db.update_item(TABLE, _runner_key(runner), {
        "cmdResultKey": key, "cmdResultAt": _now(), "cmdError": error[:400]})


def command_result(runner: str, command_id: str) -> dict | None:
    row = runner_state(runner) or {}
    if row.get("cmdId") != command_id:
        return None
    return {"commandId": command_id,
            "container": row.get("cmdContainer", ""),
            "resultKey": row.get("cmdResultKey", ""),
            "resultAt": row.get("cmdResultAt", ""),
            "error": row.get("cmdError", ""),
            "requestedAt": row.get("cmdRequestedAt", "")}

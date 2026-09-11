"""What each role needs to know, and nothing else.

Every role used to land on the same eight tiles counting graph labels. A label
count answers "how many Infrastructure nodes exist", which is nobody's first
question in the morning. This module answers a different question per role:

    developer            what does Aura know about my code, and what went stale
    qa                   can I trust the last run, and what cannot be tested
    ops                  is the platform itself healthy
    project_manager      where is each system, and what is stalled
    product_owner        what do we know about the estate, and where is the risk
    admin                what is this costing, plus everything ops sees
    ontology_maintainer  is the graph consistent with what fed it

Three rules hold across all of them.

**A metric carries its own state.** Every value is `{value, state, basis}` where
state is ok | attention | critical | unmeasured | unavailable. The UI colours
from `state` and from nothing else, which is what makes one red number on an
otherwise plain screen mean something.

**Absent is not zero.** `unmeasured` (nothing has happened yet) and
`unavailable` (we tried to measure and could not) are distinct, and both are
distinct from a real zero. Collapsing them was the old dashboard's worst habit:
a coverage query that failed and a project with no tests both rendered `0%`,
which reads as *nothing works*.

**A failing builder degrades one block, not the page.** Every source is loaded
through `_Data`, which records failures instead of raising. A role whose graph
is down still gets its DynamoDB blocks, with the rest marked `unavailable`.

Layout is deliberately NOT expressed here. This module says which blocks a role
sees and which is the hero; the UI owns every size, colour and spacing decision.
That split is what lets a new role ship as an entry in ROLE_VIEWS plus a row in
`auth-config`, with no UI deploy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import cached_property
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Runs older than this are not "recent" on any screen.
RECENT_RUNS = 8
#: A project untouched for this long is stalled. Chosen to be longer than a
#: weekend and shorter than a sprint.
STALL_DAYS = 6

#: How many project cards DevMate's hero shows. A HARD CAP, not a "usually
#: small" assumption: a developer with a hundred projects works on two or three
#: this week, and the expensive per-project work below is bounded by this
#: number rather than by the size of the estate.
HERO_PROJECTS = 6

# Pipelines that constitute each delivery stage. A stage is reached because a
# run actually succeeded, never because someone said so — there is no project
# plan in this product to read from, so the run history IS the plan.
_ANALYSIS_PIPELINES = {"git", "mcp", "api", "file-upload"}
_MAPPING_PIPELINES = {"dev-mate", "correlation", "self-learning"}
_TEST_PIPELINES = {"qa-mind"}
_MIGRATION_PIPELINES = {"migration"}


# ── Wire constructors ────────────────────────────────────────────────────────

def metric(label: str, value: Any, *, unit: str = "", state: str = "ok",
           basis: str = "", reason: str = "", href: str = "",
           spark: list[int | float] | None = None) -> dict:
    """One metric. `value=None` forces an absent state — a caller cannot
    accidentally publish a real-looking zero for something unmeasured.

    `spark` is a daily series the UI draws behind the number. It is only ever
    real history: a sparkline is a claim that we know how this moved over time,
    and drawing a decorative one on a metric with no history is the same lie as
    rendering an unmeasured value as zero. A series of fewer than three points,
    or one that is entirely zero, is dropped rather than drawn flat.
    """
    if value is None and state == "ok":
        state = "unmeasured"
    out: dict[str, Any] = {"label": label, "value": value, "state": state}
    for key, val in (("unit", unit), ("basis", basis),
                     ("reason", reason), ("href", href)):
        if val:
            out[key] = val
    if spark and len(spark) >= 3 and any(spark):
        out["spark"] = [round(float(n), 4) for n in spark]
    return out


def daily_counts(rows: list[dict], stamp_key: str, days: int = 14,
                 weigh: Callable[[dict], float] | None = None) -> list[float]:
    """Bucket rows into one value per day, oldest first.

    Days with nothing in them are zero rather than missing, so the shape of the
    line is honest about the gaps — a series that silently skipped empty days
    would draw steady activity where there was none.
    """
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    buckets = {today - timedelta(days=n): 0.0 for n in range(days)}
    for row in rows:
        parsed = _parse(row.get(stamp_key)
                        or str(row.get("sortKey", "")).split("#")[0])
        if parsed is None:
            continue
        key = parsed.date()
        if key in buckets:
            buckets[key] += weigh(row) if weigh else 1.0
    return [buckets[today - timedelta(days=n)] for n in range(days - 1, -1, -1)]


def unavailable(label: str, why: str, *, href: str = "") -> dict:
    """A metric we tried to compute and could not. Never rendered as 0."""
    return metric(label, None, state="unavailable",
                  basis="could not be measured", reason=why, href=href)


def trend(label: str, series: list[int | float], *, unit: str = "",
          money: bool = False) -> dict | None:
    """History for the hero metric, drawn as a chart beneath the headline.

    Same rule as a sparkline, for the same reason: this is a claim that we know
    how the hero moved over time. Returned as None — and therefore omitted —
    when the series is too short to have a shape or is entirely zero, rather
    than drawing a flat line that reads as "steady" when the truth is "nothing
    happened".
    """
    if not series or len(series) < 3 or not any(series):
        return None
    return {"label": label, "unit": unit, "money": money,
            "series": [round(float(n), 4) for n in series]}


def attention(severity: str, title: str, *, detail: str = "",
              when: str = "", href: str = "") -> dict:
    out = {"severity": severity, "title": title}
    for key, val in (("detail", detail), ("when", when), ("href", href)):
        if val:
            out[key] = val
    return out


def cell(text: str, *, state: str = "", mono: bool = False) -> dict:
    out: dict[str, Any] = {"text": text}
    if state:
        out["state"] = state
    if mono:
        out["mono"] = True
    return out


# ── Time ─────────────────────────────────────────────────────────────────────

def _negate(stamp: str) -> str:
    """Sort key that reverses a lexicographic ISO timestamp.

    Python cannot negate a string, and mixing `reverse=True` with an ascending
    first key in one `sorted()` call would flip both. Complementing each
    character keeps the tuple sort single-pass and readable.
    """
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c
                   for c in (stamp or ""))


def _parse(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _age_days(stamp: Any) -> float | None:
    parsed = _parse(stamp)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400


def _ago(stamp: Any, absent: str = "never") -> str:
    days = _age_days(stamp)
    if days is None:
        return absent
    seconds = days * 86400
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(days)}d ago"


def _duration(ms: Any) -> str:
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return "—"
    if value < 1000:
        return f"{int(value)}ms"
    if value < 60_000:
        return f"{value / 1000:.1f}s"
    return f"{int(value // 60000)}m {int((value % 60000) // 1000)}s"


def _pct(part: int, whole: int) -> int | None:
    """None, never 0, when there is nothing to divide by."""
    return round(part / whole * 100) if whole else None


# ── Sources ──────────────────────────────────────────────────────────────────

class _Data:
    """Every source a view might read, loaded once per request.

    A source that fails is recorded in `failed` and yields an empty default, so
    a builder can ask `data.ok("runs")` and emit `unavailable` for just that
    metric rather than taking the whole dashboard down. `cached_property`
    guarantees at most one read per source per request — the bound this module
    is held to, since several views want the same three tables.
    """

    def __init__(self, user: dict):
        self.user = user
        self.username: str = user.get("username", "")
        self.user_id: str = user.get("userId", "")
        self.role: str = user.get("role", "")
        self.failed: dict[str, str] = {}

    def ok(self, source: str) -> bool:
        return source not in self.failed

    def why(self, source: str) -> str:
        return self.failed.get(source, "unknown error")

    def _load(self, source: str, fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except Exception as exc:                                  # noqa: BLE001
            log.warning("role_metrics: source %r failed: %s", source, exc)
            self.failed[source] = f"{type(exc).__name__}: {exc}"[:200]
            return default

    # ── DynamoDB ─────────────────────────────────────────────────────────────

    @cached_property
    def projects(self) -> list[dict]:
        def read() -> list[dict]:
            from src.database import dynamo_client as db
            return db.scan_items("projects", limit=500)
        return self._load("projects", read, [])

    @cached_property
    def runs(self) -> list[dict]:
        """Pipeline runs, newest first. Queried through the feed GSI rather than
        scanned — a scan silently capped the history once it passed 200 rows."""
        def read() -> list[dict]:
            from src.services.ontology_version_service import list_versions
            return list_versions(limit=300)
        rows = self._load("runs", read, [])
        rows.sort(key=lambda r: str(r.get("startedAt") or ""), reverse=True)
        return rows

    @cached_property
    def jobs(self) -> list[dict]:
        """Scheduled-job state. `:config` rows are settings, not run records."""
        def read() -> list[dict]:
            from src.database import dynamo_client as db
            rows = db.scan_items("scheduler-state", limit=100)
            return [r for r in rows if not str(r.get("jobId", "")).endswith(":config")]
        return self._load("jobs", read, [])

    @cached_property
    def outbox(self) -> dict[str, int]:
        def read() -> dict[str, int]:
            from src.graph import outbox
            return outbox.depth()
        return self._load("outbox", read, {})

    @cached_property
    def outbox_oldest(self) -> str:
        """ISO stamp of the oldest queued write. `outboxId` is
        `<iso>#<rand>`, so the minimum id is the oldest row — no extra read."""
        def read() -> str:
            from src.database import dynamo_client as db
            from src.graph import backends as reg
            oldest = ""
            for name in reg.configured_names():
                rows = db.query_items("graph-outbox", "backend", name, limit=1)
                for row in rows:
                    stamp = str(row.get("outboxId", "")).split("#")[0]
                    if stamp and (not oldest or stamp < oldest):
                        oldest = stamp
            return oldest
        return self._load("outbox_oldest", read, "")

    @cached_property
    def test_runs(self) -> list[dict]:
        """Completed QA runs, newest first. Runner sentinel rows
        (`projectId == "_runners"`) belong to no project and are excluded."""
        def read() -> list[dict]:
            from src.database import dynamo_client as db
            rows = db.scan_items("test-results", limit=500)
            return [r for r in rows
                    if r.get("projectId") != "_runners"
                    and not str(r.get("testRunId", "")).startswith("runner:")]
        rows = self._load("test_runs", read, [])
        rows.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
        return rows

    #: Statuses a run ends in. Anything else is still in flight.
    TERMINAL = ("passed", "failed", "unavailable")

    @cached_property
    def finished_runs(self) -> list[dict]:
        return [r for r in self.test_runs if r.get("status") in self.TERMINAL]

    @cached_property
    def latest_coverage(self) -> dict:
        """Coverage for the most recent finished run.

        Read from the S3 report rather than the queue row, because coverage is
        not stored in DynamoDB — `queue.finish` records only the pass/fail
        tallies. One GET, for one run, and only when a finished run exists."""
        def read() -> dict:
            latest = self.finished_runs[0] if self.finished_runs else None
            if latest is None:
                return {}
            from src.qatest import evidence
            report = evidence.read_report(str(latest.get("projectId") or ""),
                                          str(latest.get("testRunId") or ""))
            return (report or {}).get("coverage") or {}
        return self._load("coverage", read, {})

    @cached_property
    def runners(self) -> list[dict]:
        """Runners that have polled recently. `online_runners` reads the
        aggregate index row, so this is a GetItem rather than a 500-row scan."""
        def read() -> list[dict]:
            from src.qatest import queue
            return list(queue.online_runners() or [])
        return self._load("runners", read, [])

    @cached_property
    def token_usage(self) -> list[dict]:
        def read() -> list[dict]:
            from src.database import dynamo_client as db
            return db.scan_items("token-usage", limit=1000)
        return self._load("token_usage", read, [])

    # ── Graph ────────────────────────────────────────────────────────────────

    @cached_property
    def graph_labels(self) -> dict[str, int]:
        """Every label and its count, in ONE query. Views derive their own
        numbers from this dict rather than issuing a query each."""
        def read() -> dict[str, int]:
            from src.graph import neo4j_client as neo4j
            if not neo4j.is_available():
                raise RuntimeError("graph engine is not reachable")
            with neo4j.session() as session:
                rows = session.run(
                    "MATCH (n) UNWIND labels(n) AS lbl "
                    "RETURN lbl AS label, count(*) AS cnt"
                )
                return {r["label"]: int(r["cnt"]) for r in rows}
        return self._load("graph", read, {})

    @cached_property
    def graph_by_project(self) -> dict[str, int]:
        """Node count per projectId, in ONE query."""
        def read() -> dict[str, int]:
            from src.graph import neo4j_client as neo4j
            if not neo4j.is_available():
                raise RuntimeError("graph engine is not reachable")
            with neo4j.session() as session:
                rows = session.run(
                    "MATCH (n) WHERE n.projectId IS NOT NULL "
                    "RETURN n.projectId AS pid, count(*) AS cnt"
                )
                return {str(r["pid"]): int(r["cnt"]) for r in rows}
        return self._load("graph", read, {})

    @cached_property
    def critical_findings(self) -> int | None:
        def read() -> int:
            from src.graph import neo4j_client as neo4j
            if not neo4j.is_available():
                raise RuntimeError("graph engine is not reachable")
            with neo4j.session() as session:
                row = session.run(
                    "MATCH (f:SecurityFinding|Vulnerability) "
                    "WHERE toLower(coalesce(f.severity,'')) = 'critical' "
                    "RETURN count(f) AS cnt"
                ).single()
                return int(row["cnt"]) if row else 0
        return self._load("graph", read, None)

    @cached_property
    def engines(self) -> dict[str, bool]:
        def read() -> dict[str, bool]:
            from src.graph import backends as reg
            out: dict[str, bool] = {}
            for name in reg.configured_names():
                try:
                    target = reg.get_backend(name)
                    out[name] = bool(target and target.is_available())
                except Exception:                                 # noqa: BLE001
                    out[name] = False
            return out
        return self._load("engines", read, {})

    # ── Derived ──────────────────────────────────────────────────────────────

    @cached_property
    def my_projects(self) -> list[dict]:
        """Projects this user owns. Admins see every project — they are the
        role that has to clean up after everyone else."""
        if self.role in ("admin", "super_admin"):
            return self.projects
        mine = [p for p in self.projects if p.get("userId") == self.user_id]
        # A project created before userId was recorded falls back to username,
        # which is what the projects list itself does.
        if not mine and self.username:
            mine = [p for p in self.projects if p.get("username") == self.username]
        return mine

    @cached_property
    def runs_by_project(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for run in self.runs:
            pid = str(run.get("projectId") or "")
            if pid:
                out.setdefault(pid, []).append(run)
        return out

    # ── DevMate ──────────────────────────────────────────────────────────────

    @cached_property
    def chat_sessions(self) -> list[dict]:
        """This user's DevMate conversations, newest first."""
        def read() -> list[dict]:
            from boto3.dynamodb.conditions import Attr
            from src.database import dynamo_client as db
            rows = db.scan_items("chat-sessions",
                                 filter_expr=Attr("userId").eq(self.user_id),
                                 limit=500) if self.user_id else []
            return rows
        rows = self._load("chat_sessions", read, [])
        rows.sort(key=lambda r: str(r.get("updatedAt") or ""), reverse=True)
        return rows

    @cached_property
    def proposals(self) -> list[dict]:
        """Decided DevMate proposals — what the agent suggested and whether it
        was taken. Only exists from the moment apply/discard started recording."""
        def read() -> list[dict]:
            from src.database import dynamo_client as db
            return db.scan_items("devmate-proposals", limit=500)
        return self._load("proposals", read, [])

    @cached_property
    def devmate_runs(self) -> list[dict]:
        """Conversation turns, as pipeline runs. Derived from `runs`, so this
        costs nothing extra."""
        return [r for r in self.runs
                if str(r.get("pipeline") or "") == "dev-mate"]

    @cached_property
    def devmate_tokens(self) -> list[dict]:
        """Token rows this advisor produced. Rows written before tagging began
        carry no `source` and are invisible here — deliberately, because
        attributing them would be a guess."""
        return [r for r in self.token_usage
                if str(r.get("source") or "") == "dev-mate"
                and (not self.user_id or r.get("userId") == self.user_id)]

    def hero_projects(self, limit: int = HERO_PROJECTS) -> list[dict]:
        """The working set, ranked: waiting-on-you first, then recency.

        `pending_count` is the cheap filesystem probe, NOT `list_pending` —
        ranking needs to know whether something is waiting, not what it says.
        The expensive call is made only for the handful this returns.
        """
        from src.services.advisor import tools as advisor_tools

        def rank(project: dict) -> tuple:
            pid = str(project.get("projectId") or "")
            try:
                waiting = advisor_tools.pending_count(pid)
            except Exception:                                 # noqa: BLE001
                waiting = 0
            touched = str(project.get("updatedAt") or project.get("createdAt") or "")
            # Three keys, all ascending so one `sorted()` does it:
            #   1. anything waiting on a decision outranks everything else
            #   2. a project WITH a timestamp outranks one without — an empty
            #      string sorts before every real date, so without this a
            #      project that has never been touched leads the hero
            #   3. most recent first, via the complement
            return (0 if waiting else 1, 0 if touched else 1, _negate(touched))

        return sorted(self.my_projects, key=rank)[:limit]

    @cached_property
    def known_project_ids(self) -> set[str]:
        return {str(p.get("projectId") or "") for p in self.projects} - {""}

    @cached_property
    def unattributed_runs(self) -> list[dict]:
        """Runs that cannot be tied to any project we know about.

        Not a hypothetical: in a live environment 95 of 185 runs carried no
        projectId at all and 89 carried the single character "p". A project with
        no run of its own might therefore have been analysed by one of these —
        we simply cannot tell. Saying "never analysed" in that state is a claim
        the data does not support.
        """
        return [r for r in self.runs
                if str(r.get("projectId") or "") not in self.known_project_ids]

    @cached_property
    def unattributed_graph_nodes(self) -> int:
        """Graph nodes whose projectId names no project we know."""
        return sum(count for pid, count in self.graph_by_project.items()
                   if pid not in self.known_project_ids)

    #: Evidence states. Deliberately three, not two.
    ANALYSED, NOT_ANALYSED, UNKNOWN = "analysed", "none", "unknown"

    def evidence_for(self, project: dict) -> str:
        """Whether this project was analysed — or whether we cannot tell."""
        pid = str(project.get("projectId") or "")
        if (self.last_run(pid, _ANALYSIS_PIPELINES, success_only=True) is not None
                or self.graph_by_project.get(pid, 0) > 0):
            return self.ANALYSED
        if self.unattributed_runs or self.unattributed_graph_nodes:
            return self.UNKNOWN
        return self.NOT_ANALYSED

    def last_run(self, project_id: str, pipelines: set[str] | None = None,
                 *, success_only: bool = False) -> dict | None:
        for run in self.runs_by_project.get(project_id, []):
            if pipelines and str(run.get("pipeline") or "") not in pipelines:
                continue
            if success_only and run.get("status") != "success":
                continue
            return run
        return None

    def stage_marks(self, project: dict) -> list[str]:
        """registered / analysed / mapped / tested / migrating, from evidence."""
        pid = str(project.get("projectId") or "")
        marks = ["done"]                       # the row exists, so it is registered

        # A project with no run of its own, in an environment where runs exist
        # that name no project, is UNKNOWN — not "not started". The two look the
        # same from here and mean opposite things to a reader.
        blank = "unknown" if self.unattributed_runs else "none"

        for pipelines in (_ANALYSIS_PIPELINES, _MAPPING_PIPELINES,
                          _TEST_PIPELINES, _MIGRATION_PIPELINES):
            latest = self.last_run(pid, pipelines)
            if latest is None:
                marks.append(blank)
            elif latest.get("status") == "success":
                marks.append("done")
            elif latest.get("status") == "failed":
                marks.append("failed")
            else:
                marks.append("partial")        # in_progress

        # A project with graph nodes is mapped, even when the run record that
        # produced them has aged out of the window we read.
        if marks[2] in ("none", "unknown") and self.graph_by_project.get(pid, 0) > 0:
            marks[2] = "done"
        return marks

    def is_analysed(self, project: dict) -> bool:
        """Definitely analysed. UNKNOWN counts as not-proven, so callers that
        need the difference must use `evidence_for`."""
        return self.evidence_for(project) == self.ANALYSED

    def is_tested(self, project: dict) -> bool:
        pid = str(project.get("projectId") or "")
        return self.last_run(pid, _TEST_PIPELINES, success_only=True) is not None


# ── Shared blocks ────────────────────────────────────────────────────────────

def _platform_attention(data: _Data) -> list[dict]:
    """Problems with the platform itself. Ops sees these as its whole rail;
    admin inherits them because admin is the superset role."""
    items: list[dict] = []

    if data.ok("outbox"):
        for backend, depth in sorted(data.outbox.items()):
            if depth <= 0:
                continue
            when = _ago(data.outbox_oldest, "") if data.ok("outbox_oldest") else ""
            items.append(attention(
                "critical",
                f"{backend} is {depth:,} writes behind",
                detail="The graph engines have diverged; queued writes will replay "
                       "against a store that may have moved on.",
                when=f"oldest {when}" if when else "",
                href="/settings",
            ))

    if data.ok("engines"):
        for name, reachable in sorted(data.engines.items()):
            if not reachable:
                items.append(attention("critical", f"{name} is unreachable",
                                       href="/settings"))

    if data.ok("jobs"):
        silent = [j for j in data.jobs if not j.get("lastRunAt")]
        for job in silent:
            items.append(attention(
                "attention",
                f"{job.get('jobId', 'a scheduled job')} has never recorded a run",
                detail="Work this job is responsible for is not being done.",
                href="/scheduler",
            ))

    if data.ok("runs") and data.unattributed_runs:
        share = _pct(len(data.unattributed_runs), len(data.runs))
        items.append(attention(
            "attention",
            f"{len(data.unattributed_runs)} of {len(data.runs)} pipeline runs "
            f"name no known project ({share}%)",
            detail="Their work cannot be attributed, so per-project coverage and "
                   "delivery stages understate what has been done.",
            href="/ontology"))

    if data.ok("projects"):
        stuck = [p for p in data.projects if p.get("status") == "deletion_failed"]
        if stuck:
            names = ", ".join(str(p.get("name") or p.get("projectId")) for p in stuck[:3])
            items.append(attention(
                "attention",
                f"{len(stuck)} project{'s' if len(stuck) > 1 else ''} stuck "
                f"in deletion_failed",
                detail=names, href="/dev-chat",
            ))

    return items


def _recent_runs_block(data: _Data, title: str = "Recent pipeline runs") -> dict:
    rows = []
    for run in data.runs[:RECENT_RUNS]:
        status = str(run.get("status") or "unknown")
        state = ("critical" if status == "failed"
                 else "attention" if status == "in_progress" else "")
        rows.append({
            "href": f"/ontology?run={run.get('versionId', '')}",
            "cells": [
                cell(str(run.get("pipeline") or "unattributed")),
                cell(_project_name(data, str(run.get("projectId") or "")) or "—"),
                cell(status, state=state),
                cell(_duration(run.get("durationMs"))),
                cell(_ago(run.get("startedAt"))),
            ],
        })
    return {
        "kind": "list", "title": title,
        "columns": [
            {"key": "pipeline", "label": "Pipeline"},
            {"key": "project", "label": "Project"},
            {"key": "status", "label": "Status"},
            {"key": "duration", "label": "Duration", "align": "right"},
            {"key": "when", "label": "When", "align": "right"},
        ],
        "rows": rows,
        "empty": "No pipeline has run yet. Analyse a project to populate this.",
    }


def _project_name(data: _Data, project_id: str) -> str:
    if not project_id:
        return ""
    for project in data.projects:
        if project.get("projectId") == project_id:
            return str(project.get("name") or project_id[:8])
    return project_id[:8]


# ── Role views ───────────────────────────────────────────────────────────────

def _developer(data: _Data) -> dict:
    projects = data.my_projects
    analysed = [p for p in projects if data.is_analysed(p)]
    never = [p for p in projects if data.evidence_for(p) == data.NOT_ANALYSED]
    unknown = [p for p in projects if data.evidence_for(p) == data.UNKNOWN]
    failed = [r for r in data.runs
              if r.get("status") == "failed"
              and str(r.get("pipeline") or "") in _ANALYSIS_PIPELINES]

    detail_parts = []
    if never:
        detail_parts.append(f"{len(never)} never analysed")
    if unknown:
        detail_parts.append(f"{len(unknown)} with no attributable history")
    if failed:
        detail_parts.append(f"{len(failed)} analysis run"
                            f"{'s' if len(failed) > 1 else ''} failed")

    if not data.ok("projects"):
        headline = {"text": "Your projects could not be loaded",
                    "detail": data.why("projects"), "state": "unavailable"}
    elif not projects:
        headline = {"text": "No projects yet",
                    "detail": "Register a repository in DevMate and Aura will map it.",
                    "state": "unmeasured",
                    "action": {"label": "Open DevMate", "href": "/dev-chat"}}
    elif len(unknown) == len(projects):
        # "0 of 3 analysed" is a confident claim, and the next line would admit
        # we cannot tell. Say the second thing only.
        headline = {
            "text": f"None of your {len(projects)} projects can be traced to a run",
            "detail": "Pipeline runs are not recording which project they belong "
                      "to, so Aura cannot say what has been analysed.",
            "state": "unavailable",
        }
    else:
        headline = {
            "text": f"{len(analysed)} of {len(projects)} projects analysed",
            "detail": " · ".join(detail_parts) or "Everything is up to date.",
            "state": "attention" if (never or failed or unknown) else "ok",
        }

    # Attention: failures first, then the silent gaps.
    items: list[dict] = []
    for run in failed[:3]:
        errors = run.get("errors") or []
        items.append(attention(
            "critical",
            f"{_project_name(data, str(run.get('projectId') or '')) or 'A project'}"
            f" — analysis failed",
            detail=str(errors[0])[:180] if errors else "",
            when=_ago(run.get("startedAt")), href="/reverse-engineering",
        ))
    for project in data.my_projects:
        if project.get("status") == "deletion_failed":
            pid = str(project.get("projectId") or "")
            nodes = data.graph_by_project.get(pid, 0)
            items.append(attention(
                "attention",
                f"{project.get('name')} — deletion failed"
                + (f", {nodes:,} nodes still in the graph" if nodes else ""),
                href="/dev-chat"))
    for project in never[:3]:
        items.append(attention(
            "attention", f"{project.get('name')} — never analysed",
            when=_ago(project.get("createdAt")), href="/reverse-engineering"))

    dev_trend = trend("Analysis runs per day",
                      daily_counts([r for r in data.runs
                                    if str(r.get("pipeline") or "")
                                    in _ANALYSIS_PIPELINES],
                                   "startedAt", 14))
    if dev_trend:
        headline["trend"] = dev_trend

    labels = data.graph_labels
    if data.ok("graph"):
        mapped = {"kind": "metrics", "title": "What Aura mapped from your code",
                  "items": [
                      metric("Services", labels.get("Service", 0), href="/ontology"),
                      metric("Repositories", labels.get("Repository", 0), href="/ontology"),
                      metric("Dependencies", labels.get("Dependency", 0), href="/ontology"),
                      metric("API endpoints", labels.get("API", 0), href="/ontology"),
                  ]}
    else:
        mapped = {"kind": "metrics", "title": "What Aura mapped from your code",
                  "items": [unavailable(name, data.why("graph")) for name in
                            ("Services", "Repositories", "Dependencies", "API endpoints")]}

    rows = []
    for project in projects:
        pid = str(project.get("projectId") or "")
        latest = data.last_run(pid, _ANALYSIS_PIPELINES)
        nodes = data.graph_by_project.get(pid, 0)
        if latest is None and data.evidence_for(project) == data.UNKNOWN:
            # Runs exist that name no project. "never" would be a claim we
            # cannot support, and it is the claim a developer would act on.
            analysed_cell = cell("unknown", state="unmeasured")
        elif latest is None:
            analysed_cell = cell("never", state="unmeasured")
        elif latest.get("status") == "failed":
            analysed_cell = cell("failed", state="critical")
        else:
            analysed_cell = cell(_ago(latest.get("startedAt")))
        rows.append({
            "href": f"/reverse-engineering?project={pid}",
            "cells": [
                cell(str(project.get("name") or pid[:8])),
                analysed_cell,
                cell(f"{nodes:,} nodes" if nodes else "—",
                     state="" if nodes else "unmeasured"),
                cell(_duration(latest.get("durationMs")) if latest else "—"),
            ],
        })

    return {
        "headline": headline,
        "attention": items,
        "blocks": [
            mapped,
            {"kind": "list", "title": "Your projects",
             "columns": [
                 {"key": "project", "label": "Project"},
                 {"key": "analysed", "label": "Analysed"},
                 {"key": "found", "label": "Found", "align": "right"},
                 {"key": "duration", "label": "Last run", "align": "right"},
             ],
             "rows": rows,
             "empty": "No projects yet. Register a repository in DevMate."},
        ],
    }


def _qa(data: _Data) -> dict:
    runs = data.test_runs
    finished = data.finished_runs
    projects = data.projects
    online = data.runners if data.ok("runners") else []

    latest = finished[0] if finished else None
    coverage = data.latest_coverage if latest else {}
    node_pct = coverage.get("nodePct")

    # Execution and untestability come off the queue row, which always has them —
    # no S3 read required, so these survive an evidence-bucket outage.
    planned = int((latest or {}).get("totalCases") or 0)
    ran = (int((latest or {}).get("totalPassed") or 0)
           + int((latest or {}).get("totalFailed") or 0))
    untestable_n = (int((latest or {}).get("totalUnemulated") or 0)
                    + int((latest or {}).get("totalSkipped") or 0))
    untestable = _pct(untestable_n, planned)
    executed_pct = _pct(ran, planned)

    if not data.ok("test_runs"):
        headline = {"text": "Test runs could not be loaded",
                    "detail": data.why("test_runs"), "state": "unavailable"}
    elif not finished:
        headline = {
            "text": "No project has ever been tested",
            "detail": f"{len(projects)} project{'s' if len(projects) != 1 else ''} · "
                      f"0 runs completed · {len(online)} runner"
                      f"{'s' if len(online) != 1 else ''} online",
            "state": "attention",
            "action": {"label": "Open QualityMind", "href": "/qa"},
        }
    elif node_pct is None:
        headline = {
            "text": "The last run verified nothing",
            "detail": "No API or Service node was confirmed by a passing case. "
                      "Check whether the application started.",
            "state": "attention",
            "action": {"label": "Open QualityMind", "href": "/qa"},
        }
    else:
        headline = {
            "text": f"{node_pct}% graph coverage",
            "detail": f"{coverage.get('nodeCovered', 0)} of "
                      f"{coverage.get('nodeTotal', 0)} nodes verified by a passing case",
            "state": "ok" if node_pct >= 50 else "attention",
            # Drawn as an arc beside the number. The arc IS the number — it is
            # not a target, a projection, or a second metric in disguise.
            "gauge": node_pct,
        }

    items: list[dict] = []

    queued = [r for r in runs if r.get("status") == "queued"]
    if queued and not online:
        oldest = min((str(r.get("createdAt") or "") for r in queued), default="")
        items.append(attention(
            "critical",
            f"{len(queued)} run{'s' if len(queued) > 1 else ''} queued, "
            f"no runner has claimed them",
            detail="Start the self-hosted runner, or the queue will not drain.",
            when=_ago(oldest, ""), href="/qa"))

    stuck = [r for r in runs if r.get("status") in ("claimed", "running")
             and (_age_days(r.get("updatedAt") or r.get("createdAt")) or 0) > 0.25]
    if stuck:
        items.append(attention(
            "attention",
            f"{len(stuck)} run{'s' if len(stuck) > 1 else ''} have been running "
            f"for more than 6 hours",
            detail="They will not finish. Stop them from QualityMind.", href="/qa"))

    if data.ok("jobs"):
        reaper = next((j for j in data.jobs if "reap" in str(j.get("jobId", ""))), None)
        if reaper is not None and not reaper.get("lastRunAt"):
            items.append(attention(
                "attention", "The QA reaper has never recorded a run",
                detail="Runs that die mid-flight will stay marked running forever.",
                href="/scheduler"))

    if latest is not None and untestable is not None and untestable >= 50:
        items.append(attention(
            "attention",
            f"{_project_name(data, str(latest.get('projectId') or ''))} — "
            f"{untestable_n} of {planned} cases could not be executed",
            detail="Nothing was verified. Check the emulators and the app server.",
            href=f"/qa"))

    rows = []
    for run in runs[:RECENT_RUNS]:
        status = str(run.get("status") or "unknown")
        passed = int(run.get("totalPassed") or 0)
        total = int(run.get("totalCases") or 0)
        if status == "failed":
            result, state = f"failed · {passed} of {total}", "critical"
        elif status == "cancelled":
            result, state = "cancelled", "attention"
        elif status == "unavailable":
            result, state = f"unavailable · 0 of {total}", "attention"
        elif status in ("queued", "claimed", "running"):
            result, state = status, "attention"
        else:
            result, state = f"{passed} of {total} passed", ""
        rows.append({
            "href": "/qa",
            "cells": [
                cell(str(run.get("testRunId", ""))[:8], mono=True),
                cell(_project_name(data, str(run.get("projectId") or "")) or "—"),
                cell(result, state=state),
                cell(_ago(run.get("createdAt"))),
            ],
        })

    qa_trend = trend("Test runs per day", daily_counts(runs, "createdAt", 14))
    if qa_trend:
        headline["trend"] = qa_trend

    def _untestable_state() -> str:
        if untestable is None:
            return "unmeasured"
        if untestable >= 50:
            return "critical"
        return "attention" if untestable >= 20 else "ok"

    return {
        "headline": headline,
        "attention": items,
        "blocks": [
            {"kind": "metrics", "title": "The last run", "items": [
                metric("Graph coverage", node_pct, unit="%",
                       state="ok" if node_pct is not None else
                             ("unavailable" if not data.ok("coverage") else "unmeasured"),
                       reason=data.why("coverage") if not data.ok("coverage") else "",
                       basis=(f"{coverage.get('nodeCovered', 0)} of "
                              f"{coverage.get('nodeTotal', 0)} verified")
                             if node_pct is not None else "no run has completed",
                       href="/qa"),
                # The metric this screen was missing. A run reporting "0 failed"
                # and a run where nothing could execute looked identical before,
                # and the second is the state most projects are actually in.
                metric("Untestable", untestable, unit="%",
                       state=_untestable_state(),
                       basis=(f"{untestable_n} of {planned} cases never ran"
                              if untestable is not None else "no run has completed"),
                       href="/qa"),
                metric("Plan executed", executed_pct, unit="%",
                       state="ok" if executed_pct is not None else "unmeasured",
                       basis=(f"{ran} of {planned} cases"
                              if planned else "no run has completed"),
                       href="/qa"),
                metric("Runs", len(runs) or None, basis="recorded",
                       href="/qa", spark=daily_counts(runs, "createdAt")),
                metric("Runners", len(online) if data.ok("runners") else None,
                       state="ok" if data.ok("runners") else "unavailable",
                       reason=data.why("runners") if not data.ok("runners") else "",
                       basis="online" if online else "none online", href="/qa"),
            ]},
            {"kind": "list", "title": "Recent runs",
             "columns": [
                 {"key": "run", "label": "Run"},
                 {"key": "project", "label": "Project"},
                 {"key": "result", "label": "Result"},
                 {"key": "when", "label": "When", "align": "right"},
             ],
             "rows": rows,
             "empty": "No test run has been recorded. Start one from QualityMind."},
        ],
    }


def _ops(data: _Data) -> dict:
    items = _platform_attention(data)
    worst = next((i for i in items if i["severity"] == "critical"), None)

    day_old = [r for r in data.runs if (_age_days(r.get("startedAt")) or 99) <= 1]
    failed_today = [r for r in day_old if r.get("status") == "failed"]
    reachable = sum(1 for ok in data.engines.values() if ok)
    total_engines = len(data.engines)
    outbox_total = sum(data.outbox.values()) if data.ok("outbox") else None

    if worst is not None:
        headline = {"text": worst["title"], "detail": worst.get("detail", ""),
                    "state": "critical"}
        if worst.get("href"):
            headline["action"] = {"label": "Investigate", "href": worst["href"]}
    elif items:
        headline = {"text": f"{len(items)} things need attention",
                    "detail": "Nothing is broken, but these will not fix themselves.",
                    "state": "attention"}
    else:
        headline = {"text": "All systems nominal",
                    "detail": f"{len(day_old)} pipeline runs in the last 24 hours, "
                              f"none failed.", "state": "ok"}

    # Throughput sits under every one of those headlines: it is the context for
    # "is this normal", which a count of problems alone cannot give.
    headline["trend"] = trend("Pipeline runs per day",
                              daily_counts(data.runs, "startedAt", 14))

    job_rows = []
    for job in sorted(data.jobs, key=lambda j: str(j.get("jobId", ""))):
        last = job.get("lastRunAt")
        job_rows.append({
            "cells": [
                cell(str(job.get("jobId", "—"))),
                cell(str(job.get("interval") or job.get("cronExpression") or "—")),
                cell(_ago(last), state="" if last else "critical"),
                cell(str(job.get("lastStatus") or "—"),
                     state="critical" if job.get("lastStatus") == "error" else ""),
            ],
        })

    return {
        "headline": headline,
        "attention": items,
        "blocks": [
            {"kind": "metrics", "title": "Platform", "items": [
                metric("Pipeline runs", len(day_old),
                       basis=f"{len(data.runs)} all time", href="/ontology",
                       spark=daily_counts(data.runs, "startedAt"))
                if data.ok("runs") else unavailable("Pipeline runs", data.why("runs")),
                metric("Failure rate", _pct(len(failed_today), len(day_old)), unit="%",
                       spark=daily_counts([r for r in data.runs
                                           if r.get("status") == "failed"],
                                          "startedAt"),
                       state=("critical" if len(failed_today) > len(day_old) / 4
                              else "attention" if failed_today else "ok")
                             if day_old else "unmeasured",
                       basis=(f"{len(failed_today)} of {len(day_old)} in 24h"
                              if day_old else "no runs in the last 24 hours")),
                metric("Engines", f"{reachable} of {total_engines}" if total_engines else None,
                       state=("ok" if reachable == total_engines else "critical")
                             if total_engines else "unavailable",
                       reason=data.why("engines") if not data.ok("engines") else "",
                       basis=", ".join(n for n, ok in sorted(data.engines.items())
                                       if not ok) + " unreachable"
                             if reachable < total_engines else "all reachable",
                       href="/settings"),
                metric("Outbox", outbox_total,
                       state=("critical" if outbox_total else "ok")
                             if outbox_total is not None else "unavailable",
                       reason=data.why("outbox") if not data.ok("outbox") else "",
                       basis="writes pending" if outbox_total else "nothing queued",
                       href="/settings"),
            ]},
            {"kind": "list", "title": "Scheduled jobs",
             "columns": [
                 {"key": "job", "label": "Job"},
                 {"key": "interval", "label": "Interval"},
                 {"key": "last", "label": "Last run"},
                 {"key": "result", "label": "Last result"},
             ],
             "rows": job_rows,
             "empty": "No scheduled jobs are registered."},
            _recent_runs_block(data),
        ],
    }


def _project_manager(data: _Data) -> dict:
    projects = data.projects
    stalled = []
    for project in projects:
        pid = str(project.get("projectId") or "")
        latest = data.runs_by_project.get(pid, [])
        stamp = (latest[0].get("startedAt") if latest
                 else project.get("updatedAt") or project.get("createdAt"))
        age = _age_days(stamp)
        if age is not None and age >= STALL_DAYS:
            stalled.append((project, age, bool(latest)))

    if not data.ok("projects"):
        headline = {"text": "The portfolio could not be loaded",
                    "detail": data.why("projects"), "state": "unavailable"}
    elif not projects:
        headline = {"text": "No systems registered", "state": "unmeasured",
                    "detail": "Nothing has been brought into Aura yet."}
    elif stalled:
        headline = {"text": f"{len(stalled)} of {len(projects)} systems are stalled",
                    "detail": f"Nothing has moved on them for {STALL_DAYS} days or more.",
                    "state": "attention"}
    else:
        headline = {"text": f"All {len(projects)} systems are moving",
                    "detail": f"Every system has had activity in the last "
                              f"{STALL_DAYS} days.", "state": "ok"}

    pm_trend = trend("Activity per day", daily_counts(data.runs, "startedAt", 14))
    if pm_trend:
        headline["trend"] = pm_trend

    items = []
    for project, age, ever_ran in sorted(stalled, key=lambda s: -s[1]):
        pid = str(project.get("projectId") or "")
        if not ever_ran and data.evidence_for(project) == data.UNKNOWN:
            why = (f"registered {int(age)}d ago, no attributable history "
                   f"— runs are not recording a project id")
        elif not ever_ran:
            why = f"registered {int(age)}d ago, never analysed"
        elif not data.is_tested(project):
            why = f"analysed {int(age)}d ago, no test run since"
        else:
            why = f"no activity for {int(age)}d"
        items.append(attention("attention", f"{project.get('name')} — {why}",
                               href=f"/reverse-engineering?project={pid}"))

    if data.unattributed_runs:
        items.insert(0, attention(
            "attention",
            f"{len(data.unattributed_runs)} pipeline runs name no project",
            detail="Their work cannot be credited to anything on this board, so "
                   "stages below may understate what has actually been done.",
            href="/ontology"))

    stages = ["Registered", "Analysed", "Mapped", "Tested", "Migrating"]
    board_rows = [{
        "label": str(p.get("name") or str(p.get("projectId"))[:8]),
        "href": f"/reverse-engineering?project={p.get('projectId')}",
        "marks": data.stage_marks(p),
    } for p in projects]

    furthest = 0
    for row in board_rows:
        reached = sum(1 for m in row["marks"] if m == "done")
        furthest = max(furthest, reached)

    # `and r.get("durationMs")` excluded every run that took 0ms — which live
    # runs genuinely do — so the average silently dropped its fastest samples
    # and reported "unmeasured" when they were the only ones.
    analysis_runs = [r for r in data.runs
                     if str(r.get("pipeline") or "") in _ANALYSIS_PIPELINES
                     and r.get("durationMs") is not None]
    avg_ms = (sum(float(r["durationMs"]) for r in analysis_runs) / len(analysis_runs)
              if analysis_runs else None)

    week = [r for r in data.runs if (_age_days(r.get("startedAt")) or 99) <= 7]

    return {
        "headline": headline,
        "attention": items,
        "blocks": [
            {"kind": "pipeline", "title": "Delivery pipeline",
             "stages": stages, "rows": board_rows,
             "legend": ("Stage reached is derived from pipeline runs that actually "
                        "succeeded, not self-reported."
                        + (" A dash means no run names this project, so its "
                           "progress cannot be determined."
                           if data.unattributed_runs else ""))},
            {"kind": "metrics", "title": "Portfolio", "items": [
                metric("In pipeline", len(projects), basis="systems"),
                metric("Stalled", len(stalled),
                       state="attention" if stalled else "ok",
                       basis=f"no activity for {STALL_DAYS}d or more"),
                metric("Furthest stage",
                       stages[furthest - 1] if furthest else None,
                       basis="reached by at least one system"
                             if furthest else "nothing has progressed"),
                metric("Avg time to analyse",
                       _duration(avg_ms) if avg_ms is not None else None,
                       basis=f"across {len(analysis_runs)} runs"
                             if analysis_runs else "no analysis has completed"),
            ]},
            {"kind": "note", "title": "Activity this week",
             "text": f"{len(week)} pipeline run{'s' if len(week) != 1 else ''} · "
                     f"{sum(1 for r in week if r.get('status') == 'failed')} failed · "
                     f"{sum(1 for r in week if str(r.get('pipeline')) in _MIGRATION_PIPELINES)}"
                     f" migrations started."},
        ],
    }


def _product_owner(data: _Data) -> dict:
    projects = data.projects
    analysed = [p for p in projects if data.is_analysed(p)]
    unknown = [p for p in projects if data.evidence_for(p) == data.UNKNOWN]
    tested = [p for p in projects if data.is_tested(p)]
    critical = data.critical_findings

    # When some projects cannot be attributed, a percentage over ALL of them is
    # not a measurement — it is a guess reported to two significant figures.
    # 0% understood on an estate with 1,666 mapped nodes is worse than no number.
    measurable = len(projects) - len(unknown)
    understood = _pct(len(analysed), measurable) if measurable else None
    verified = _pct(len(tested), measurable) if measurable else None
    gap = (f"{len(unknown)} of {len(projects)} systems have no attributable "
           f"history" if unknown else "")

    if not data.ok("projects"):
        headline = {"text": "The portfolio could not be loaded",
                    "detail": data.why("projects"), "state": "unavailable"}
    elif not projects:
        headline = {"text": "No systems under management", "state": "unmeasured",
                    "detail": "Nothing has been brought into Aura yet."}
    elif understood is None:
        headline = {
            "text": "The estate cannot be assessed yet",
            "detail": f"{gap}. Pipeline runs are not recording which project they "
                      f"belong to, so nothing here can be attributed.",
            "state": "unavailable",
        }
    else:
        headline = {
            "gauge": understood,
            "text": f"{understood}% of the assessable estate is understood",
            "detail": f"{len(analysed)} of {measurable} systems analysed · "
                      f"{len(tested)} verified by tests"
                      + (f" · {gap}" if gap else ""),
            "state": "ok" if understood >= 80 else "attention",
        }

    items = []
    if critical:
        items.append(attention(
            "critical", f"{critical:,} critical security findings",
            detail="None have been triaged in Aura.", href="/observability"))
    if unknown:
        items.append(attention(
            "attention",
            f"{len(unknown)} system{'s' if len(unknown) > 1 else ''} cannot be "
            f"assessed",
            detail="Pipeline runs are not recording which project they belong to, "
                   "so their history cannot be attributed.",
            href="/ontology"))
    if measurable and not tested:
        items.append(attention(
            "attention", "No assessable system has a passing test run",
            detail="Coverage cannot be reported until QualityMind runs succeed.",
            href="/qa"))

    rows = []
    for project in projects:
        pid = str(project.get("projectId") or "")
        is_analysed = data.is_analysed(project)
        evidence = data.evidence_for(project)
        understood_cell = (
            cell("yes") if evidence == data.ANALYSED
            else cell("unknown", state="unmeasured") if evidence == data.UNKNOWN
            else cell("no", state="attention"))
        rows.append({"cells": [
            cell(str(project.get("name") or pid[:8])),
            understood_cell,
            cell("yes" if data.is_tested(project) else "no",
                 state="" if data.is_tested(project) else "attention"),
            cell(_ago(project.get("updatedAt") or project.get("createdAt"))),
        ]})

    spend = _spend_total(data)

    return {
        "headline": headline,
        "attention": items,
        "blocks": [
            {"kind": "metrics", "title": "Portfolio", "items": [
                metric("Systems", len(projects), basis="under management"),
                metric("Understood", understood, unit="%",
                       state=("ok" if understood >= 80 else "attention")
                             if understood is not None else "unmeasured",
                       basis=(f"{len(analysed)} of {measurable} assessable"
                              if understood is not None
                              else "no system has attributable history")),
                metric("Verified", verified, unit="%",
                       state=("ok" if verified >= 50 else "attention")
                             if verified is not None else "unmeasured",
                       basis=(f"{len(tested)} tested" if verified is not None
                              else "no system has attributable history"),
                       href="/qa"),
                metric("Critical findings", critical,
                       state=("critical" if critical else "ok")
                             if critical is not None else "unavailable",
                       reason=data.why("graph") if critical is None else "",
                       basis="open, current count"),
            ]},
            {"kind": "list", "title": "Portfolio",
             "columns": [
                 {"key": "system", "label": "System"},
                 {"key": "understood", "label": "Understood"},
                 {"key": "verified", "label": "Verified"},
                 {"key": "touched", "label": "Last touched", "align": "right"},
             ],
             "rows": rows,
             "empty": "No systems registered yet."},
            {"kind": "metrics", "title": "Platform spend (30d)", "items": [
                metric("Spend", f"${spend['cost']:,.2f}" if data.ok("token_usage") else None,
                       state="ok" if data.ok("token_usage") else "unavailable",
                       reason=data.why("token_usage"),
                       basis=f"{spend['users']} users · {spend['calls']:,} calls",
                       spark=daily_counts(data.token_usage, "timestamp", 30,
                                          weigh=_row_cost)),
            ]},
            # Said out loud rather than implied by a sparkline we cannot draw.
            {"kind": "note", "title": "About these numbers",
             "text": "Every figure here is a current count. Aura does not yet keep a "
                     "history of finding counts, so trend over time is not available."},
        ],
    }


def _row_cost(row: dict) -> float:
    try:
        return float(row.get("cost") or row.get("totalCost") or 0)
    except (TypeError, ValueError):
        return 0.0


def _spend_total(data: _Data) -> dict:
    """30-day spend from token-usage. Costs are pre-computed per row."""
    cost, calls, users = 0.0, 0, set()
    for row in data.token_usage:
        stamp = row.get("timestamp") or str(row.get("sortKey", "")).split("#")[0]
        age = _age_days(stamp)
        if age is not None and age > 30:
            continue
        calls += 1
        users.add(row.get("userId"))
        try:
            cost += float(row.get("cost") or row.get("totalCost") or 0)
        except (TypeError, ValueError):
            pass
    return {"cost": cost, "calls": calls, "users": len(users)}


def _admin(data: _Data) -> dict:
    spend = _spend_total(data)
    items = _platform_attention(data)
    by_user: dict[str, dict] = {}
    for row in data.token_usage:
        age = _age_days(row.get("timestamp")
                        or str(row.get("sortKey", "")).split("#")[0])
        if age is not None and age > 30:
            continue
        entry = by_user.setdefault(str(row.get("username") or row.get("userId") or "—"),
                                   {"cost": 0.0, "calls": 0})
        entry["calls"] += 1
        try:
            entry["cost"] += float(row.get("cost") or row.get("totalCost") or 0)
        except (TypeError, ValueError):
            pass

    if not data.ok("token_usage"):
        headline = {"text": "Spend could not be loaded",
                    "detail": data.why("token_usage"), "state": "unavailable"}
    else:
        headline = {
            "text": f"${spend['cost']:,.2f} spent in the last 30 days",
            "detail": f"{spend['users']} active user"
                      f"{'s' if spend['users'] != 1 else ''} · "
                      f"{spend['calls']:,} calls",
            "state": "ok",
            "trend": trend("Daily spend",
                           daily_counts(data.token_usage, "timestamp", 30,
                                        weigh=_row_cost),
                           money=True),
        }

    user_rows = [{"cells": [
        cell(name),
        cell(f"${entry['cost']:,.2f}", ),
        cell(f"{entry['calls']:,}"),
    ]} for name, entry in sorted(by_user.items(), key=lambda kv: -kv[1]["cost"])]

    return {
        "headline": headline,
        "attention": items,
        "blocks": [
            {"kind": "metrics", "title": "Platform", "items": [
                metric("Users", spend["users"] or None, basis="active in 30 days",
                       href="/access"),
                metric("Projects", len(data.projects) if data.ok("projects") else None,
                       state="ok" if data.ok("projects") else "unavailable",
                       reason=data.why("projects"), basis="registered"),
                metric("Calls", spend["calls"] or None, basis="in 30 days",
                       spark=daily_counts(data.token_usage, "timestamp", 30)),
                metric("Spend per user",
                       f"${spend['cost'] / spend['users']:,.2f}" if spend["users"] else None,
                       basis="30-day average"),
                metric("Outbox", sum(data.outbox.values()) if data.ok("outbox") else None,
                       state=("critical" if sum(data.outbox.values()) else "ok")
                             if data.ok("outbox") else "unavailable",
                       reason=data.why("outbox"), basis="writes pending",
                       href="/settings"),
            ]},
            {"kind": "list", "title": "Spend by user",
             "columns": [
                 {"key": "user", "label": "User"},
                 {"key": "cost", "label": "Cost", "align": "right"},
                 {"key": "calls", "label": "Calls", "align": "right"},
             ],
             "rows": user_rows,
             "empty": "No gateway usage recorded in the last 30 days."},
            _recent_runs_block(data),
        ],
    }


def _ontology_maintainer(data: _Data) -> dict:
    labels = data.graph_labels
    total_nodes = sum(labels.values()) if data.ok("graph") else None
    outbox_total = sum(data.outbox.values()) if data.ok("outbox") else None
    items = _platform_attention(data)

    if not data.ok("graph"):
        headline = {"text": "The graph is unreachable",
                    "detail": data.why("graph"), "state": "critical"}
    elif outbox_total:
        headline = {"text": f"{outbox_total:,} writes have not reached every engine",
                    "detail": "The graph is not the same on both backends.",
                    "state": "critical",
                    "action": {"label": "Inspect the outbox", "href": "/settings"}}
    else:
        headline = {"text": f"{total_nodes:,} nodes across {len(labels)} labels",
                    "detail": "Every configured engine is in step.", "state": "ok"}

    label_rows = [{"cells": [cell(name), cell(f"{count:,}")]}
                  for name, count in sorted(labels.items(), key=lambda kv: -kv[1])]

    failed_ingestion = [r for r in data.runs if r.get("status") == "failed"]

    return {
        "headline": headline,
        "attention": items,
        "blocks": [
            {"kind": "metrics", "title": "Graph", "items": [
                metric("Nodes", total_nodes,
                       state="ok" if data.ok("graph") else "unavailable",
                       reason=data.why("graph"), href="/ontology"),
                metric("Labels in use", len(labels) or None,
                       state="ok" if data.ok("graph") else "unavailable",
                       reason=data.why("graph")),
                metric("Outbox", outbox_total,
                       state=("critical" if outbox_total else "ok")
                             if outbox_total is not None else "unavailable",
                       reason=data.why("outbox"), basis="writes pending"),
                metric("Failed ingestions", len(failed_ingestion),
                       state="attention" if failed_ingestion else "ok",
                       basis=f"of {len(data.runs)} runs", href="/ontology"),
            ]},
            # These label counts are genuinely useful to this role and to no
            # other — which is why they moved here instead of being deleted.
            {"kind": "list", "title": "Label inventory",
             "columns": [
                 {"key": "label", "label": "Label"},
                 {"key": "count", "label": "Nodes", "align": "right"},
             ],
             "rows": label_rows,
             "empty": "The graph holds no nodes."},
            _recent_runs_block(data, "Recent ingestion runs"),
        ],
    }


# ── Registry ─────────────────────────────────────────────────────────────────

ROLE_VIEWS: dict[str, Callable[[_Data], dict]] = {
    "user_dev": _developer,
    "user_qa": _qa,
    "user_ops": _ops,
    "project_manager": _project_manager,
    "product_owner": _product_owner,
    "admin": _admin,
    "super_admin": _admin,
    "ontology_maintainer": _ontology_maintainer,
}

# ── DevMate ──────────────────────────────────────────────────────────────────
#
# A second view over the same machinery. It is NOT a role view: every role that
# reaches DevMate holds `dev_workspace`, and there are only two variants
# (developer and admin), so the split is on cost visibility rather than on role.
#
# The page's hero is the project grid, not a metric — so `hero` sits beside
# `blocks` rather than inside them. A developer opens DevMate to choose a
# project and start talking; the numbers are context for that choice.


def _devmate_cards(data: _Data) -> tuple[list[dict], int]:
    """The hero's project cards, and the true total behind them."""
    from src.services.advisor import tools as advisor_tools

    projects = data.my_projects
    cards = []
    for project in data.hero_projects():
        pid = str(project.get("projectId") or "")
        # The expensive call, made only for the six that survived ranking.
        try:
            pending = advisor_tools.list_pending(pid)
        except Exception:                                     # noqa: BLE001
            pending = []

        if pending:
            status, state = (f"{len(pending)} change"
                             f"{'s' if len(pending) > 1 else ''} awaiting you"), "attention"
            detail = " · ".join(f"{c['path']} +{c['additions']} \u2212{c['deletions']}"
                                for c in pending[:3])
        elif data.evidence_for(project) == data.ANALYSED:
            nodes = data.graph_by_project.get(pid, 0)
            status, state = "analysed", "ok"
            detail = f"{nodes:,} nodes" if nodes else ""
        elif not project.get("repoCount"):
            status, state, detail = "no repo linked", "unmeasured", ""
        else:
            status, state, detail = "not analysed", "unmeasured", ""

        cards.append({
            "projectId": pid,
            "name": str(project.get("name") or pid[:8]),
            "status": status,
            "state": state,
            "detail": detail,
            "when": _ago(project.get("updatedAt") or project.get("createdAt")),
            "pending": len(pending),
        })
    return cards, len(projects)


def _devmate_blocks(data: _Data, cards: list[dict], show_cost: bool) -> dict:
    projects = data.my_projects
    waiting = sum(c["pending"] for c in cards)
    sessions = data.chat_sessions
    today = [s for s in sessions if (_age_days(s.get("updatedAt")) or 99) < 1]
    analysed = [p for p in projects if data.is_analysed(p)]

    tokens_today = sum(
        int(r.get("inputTokens") or 0) + int(r.get("outputTokens") or 0)
        for r in data.devmate_tokens
        if (_age_days(r.get("timestamp")) or 99) < 1
    )
    decided = data.proposals
    applied = [p for p in decided if p.get("decision") == "applied"]

    items = [
        metric("Projects", len(projects) or None, basis=f"{len(analysed)} analysed"),
        metric("Awaiting you", waiting,
               state="attention" if waiting else "ok",
               # Said plainly: this is the hero set's total, not a sweep of the
               # whole estate, and at 100 projects that difference matters.
               basis=(f"across your {len(cards)} most active"
                      if len(projects) > len(cards) else "staged changes")),
        metric("Tokens today", tokens_today or None,
               basis="since tagging began" if tokens_today else "no turns today",
               spark=daily_counts(
                   data.devmate_tokens, "timestamp", 14,
                   weigh=lambda r: float(int(r.get("inputTokens") or 0)
                                         + int(r.get("outputTokens") or 0)))),
        metric("Sessions", len(sessions) or None,
               state="ok" if data.ok("chat_sessions") else "unavailable",
               reason=data.why("chat_sessions") if not data.ok("chat_sessions") else "",
               basis=f"{len(today)} today" if sessions else "no conversation recorded"),
        metric("Advice applied", _pct(len(applied), len(decided)), unit="%",
               state="ok" if decided else "unmeasured",
               basis=(f"{len(applied)} of {len(decided)} decided"
                      if decided else "no proposal has been decided yet")),
    ]
    if show_cost:
        spend = _spend_total(data)
        items.append(metric("Spend", f"${spend['cost']:,.2f}", basis="last 30 days",
                            spark=daily_counts(data.token_usage, "timestamp", 30,
                                               weigh=_row_cost)))

    blocks: list[dict] = [{"kind": "metrics", "title": "Workspace", "items": items}]

    # The full catalogue is NOT sent. `ProjectsPanel` already renders it on the
    # client, with a name/environment search filter, and it fetches its own
    # rows — so shipping a hundred more rows here would duplicate that list,
    # lose its search, and grow the payload for nothing. The UI shows it when
    # `hero.total > hero.shown`; the hero's own cards are all this needs to send.

    blocks.append({
        "kind": "list", "title": "Recent sessions",
        "columns": [{"key": "name", "label": "Conversation"},
                    {"key": "project", "label": "Project"},
                    {"key": "when", "label": "When", "align": "right"}],
        "rows": [{"href": f"/dev-chat?session={s.get('sessionId', '')}", "cells": [
            cell(str(s.get("sessionName") or "Untitled")),
            cell(str(s.get("projectName") or "\u2014")),
            cell(_ago(s.get("updatedAt"))),
        ]} for s in sessions[:RECENT_RUNS]],
        "empty": "No conversation has been saved yet.",
    })
    return blocks


def build_devmate_view(user: dict) -> dict:
    """The DevMate landing payload. Same envelope as the dashboard, plus `hero`."""
    from src.services.auth_service import ROLE_LABELS

    role = str(user.get("role") or "")
    show_cost = role in ("admin", "super_admin")
    data = _Data(user)

    try:
        cards, total = _devmate_cards(data)
        waiting = sum(c["pending"] for c in cards)

        if not data.ok("projects"):
            headline = {"text": "Your projects could not be loaded",
                        "detail": data.why("projects"), "state": "unavailable"}
        elif total == 0:
            headline = {"text": "Create your first project",
                        "detail": "Point DevMate at a repository and it will map it, "
                                  "then you can ask it questions.",
                        "state": "unmeasured"}
        else:
            analysed = sum(1 for p in data.my_projects if data.is_analysed(p))
            shown = (f"{len(cards)} of {total} projects"
                     if total > len(cards) else
                     f"{total} project{'s' if total != 1 else ''}")
            headline = {
                "text": "Pick up where you left off",
                "detail": f"{shown} \u00b7 {analysed} analysed"
                          + (f" \u00b7 {waiting} change"
                             f"{'s' if waiting > 1 else ''} waiting on you"
                             if waiting else ""),
                "state": "attention" if waiting else "ok",
            }

        attention = []
        for card in cards:
            if card["pending"]:
                attention.append(attention_item(card))

        blocks = _devmate_blocks(data, cards, show_cost)
    except Exception as exc:                                  # noqa: BLE001
        log.exception("devmate view failed")
        return {"role": role, "roleLabel": ROLE_LABELS.get(role, role or "User"),
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "headline": {"text": "DevMate could not be assembled",
                             "detail": f"{type(exc).__name__}: {exc}"[:200],
                             "state": "unavailable"},
                "hero": {"cards": [], "total": 0}, "attention": [], "blocks": []}

    payload = {
        "role": role,
        "roleLabel": ROLE_LABELS.get(role, role or "User"),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "headline": headline,
        # Beside `blocks`, not inside: the grid is the page's primary control,
        # not a metric, and the UI renders it directly.
        "hero": {"cards": cards, "total": total, "shown": len(cards)},
        "attention": attention,
        "blocks": blocks,
    }
    if data.failed:
        payload["degraded"] = sorted(data.failed)
    return payload


def attention_item(card: dict) -> dict:
    return attention(
        "attention",
        f"{card['name']} \u2014 {card['status']}",
        detail=card["detail"],
        when=card["when"],
        href=f"/dev-chat?project={card['projectId']}",
    )


def _assert_every_role_has_a_view() -> None:
    """Fail at import if a built-in role has no view.

    The failure mode this prevents is silent: a role added to ROLE_PERMISSIONS
    without a view here would fall through to DEFAULT_VIEW and show its holder
    a developer's dashboard, which looks like a working feature rather than an
    omission. Only built-in roles are checked — a directory user can carry any
    role name their organisation invented, and DEFAULT_VIEW exists for them.
    """
    from src.services.auth_service import ROLE_PERMISSIONS
    missing = sorted(set(ROLE_PERMISSIONS) - set(ROLE_VIEWS))
    if missing:
        raise RuntimeError(
            "role_metrics: no dashboard view for built-in role(s) "
            + ", ".join(missing)
            + " — add one to ROLE_VIEWS, or the role lands on the wrong screen."
        )


#: The view an unrecognised role gets. A directory user can carry any role name
#: their organisation invented, and they must still land on something.
DEFAULT_VIEW = _developer


def build_view(user: dict) -> dict:
    """The whole dashboard payload for one user."""
    from src.services.auth_service import ROLE_LABELS

    role = str(user.get("role") or "")
    builder = ROLE_VIEWS.get(role, DEFAULT_VIEW)
    data = _Data(user)

    try:
        view = builder(data)
    except Exception as exc:                                      # noqa: BLE001
        # A builder bug must not return a 500 — the dashboard is the landing
        # page, and a blank landing page looks like the product is down.
        log.exception("role view %r failed", role)
        view = {
            "headline": {"text": "Your dashboard could not be assembled",
                         "detail": f"{type(exc).__name__}: {exc}"[:200],
                         "state": "unavailable"},
            "attention": [], "blocks": [],
        }

    payload = {
        "role": role,
        "roleLabel": ROLE_LABELS.get(role, role or "User"),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "headline": view.get("headline") or {"text": "", "state": "ok"},
        "attention": view.get("attention") or [],
        "blocks": view.get("blocks") or [],
    }
    if data.failed:
        # Reported, not hidden. A dashboard quietly missing a section is worse
        # than one that says which source it could not read.
        payload["degraded"] = sorted(data.failed)
    return payload


_assert_every_role_has_a_view()

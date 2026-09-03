"""Ambient provenance: who wrote a node or edge, when, from where, in which run.

Every node and edge in the graph is written by one of eight pipelines — Git, MCP,
API, file upload, Dev Mate, QA Mind, self-learning, or a manual edit — and until now
almost none of them recorded anything beyond a bare `source` string. Clicking a node
in Onto Verse could not answer "who put this here, and when".

The obvious fix is to add `actor` / `run_id` parameters to the write helpers and
thread them through every caller. That is ~79 call sites across 26 modules, and it
fails the moment someone adds the 80th without remembering. Worse, several of those
callers are three or four frames below the request handler that actually knows who
the user is, so the parameter would have to be carried through code that has no
other reason to know about it.

So the context travels out-of-band, in a ContextVar, and is applied at the single
place every write already funnels through — the upsert helpers in `neo4j_client`.
A caller opens a `trace_run(...)` block; everything written inside it, however deep,
is attributed. Nothing else changes.

Two consequences worth stating plainly:

  * Provenance is part of the same `SET` payload as the data. It is not a follow-up
    write that can fail on its own, and because `backends._FanOutSession` mirrors by
    replaying the identical statement and parameters, Memgraph receives byte-identical
    provenance with no extra code — including through the outbox when it is down.

  * There is no `None` context. Code running outside any `trace_run` writes
    UNATTRIBUTED, which is a recorded value, not a missing key. "We do not know who
    wrote this" is a fact the UI can show and an operator can go fix; a null is just
    a hole.

⚠️ ContextVars do NOT cross a `threading.Thread` boundary. Several routers hand
ingestion to a thread or an executor (`routers/service_loader.py`,
`routers/projects.py`, `load_via_mcp`). Those hand-offs must go through
`spawn_traced` / `traced_callable`, or the work lands unattributed — which is silent,
and the single most likely way this module gets quietly defeated.
"""
from __future__ import annotations

import contextvars
import functools
import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Generator, Iterable

log = logging.getLogger(__name__)


# ── Vocabulary ───────────────────────────────────────────────────────────────
# These are the values the UI groups, colours and filters by, so they are a
# contract, not free text. `pipeline` answers "which product surface produced
# this"; `source` stays the finer-grained system name already in use
# (`code-analysis`, `mcp:shop`, `servicenow_cmdb`, …) and is not replaced.

PIPELINE_GIT = "git"
PIPELINE_MCP = "mcp"
PIPELINE_API = "api"
PIPELINE_FILE = "file-upload"
PIPELINE_DEV_MATE = "dev-mate"
PIPELINE_QA_MIND = "qa-mind"
PIPELINE_SELF_LEARNING = "self-learning"
PIPELINE_MANUAL = "manual"
PIPELINE_CORRELATION = "correlation"
PIPELINE_SEED = "seed"
PIPELINE_UNKNOWN = "unattributed"

PIPELINES = (
    PIPELINE_GIT, PIPELINE_MCP, PIPELINE_API, PIPELINE_FILE, PIPELINE_DEV_MATE,
    PIPELINE_QA_MIND, PIPELINE_SELF_LEARNING, PIPELINE_MANUAL,
    PIPELINE_CORRELATION, PIPELINE_SEED, PIPELINE_UNKNOWN,
)

# How the write was initiated. Deliberately separate from `pipeline`: the same Git
# ingestion is a different thing when a person clicked it than when cron ran it, and
# the request that motivated this work called that distinction out explicitly.
TRIGGER_MANUAL = "manual"        # a signed-in human asked for it
TRIGGER_SCHEDULED = "scheduled"  # APScheduler / cron
TRIGGER_AUTOMATIC = "automatic"  # a system event cascaded into a write
TRIGGER_SYSTEM = "system"        # startup seed, migration, backfill
TRIGGER_UNKNOWN = "unknown"

TRIGGERS = (TRIGGER_MANUAL, TRIGGER_SCHEDULED, TRIGGER_AUTOMATIC,
            TRIGGER_SYSTEM, TRIGGER_UNKNOWN)

# Written onto anything whose origin predates this module, by the backfill script.
ATTRIBUTION_TRACED = "traced"
ATTRIBUTION_PRE_TRACE = "pre-trace"
ATTRIBUTION_NONE = "none"

# Stats keys a run accumulates. Fixed so the UI can render tiles without guessing.
_STAT_KEYS = ("nodesAdded", "nodesUpdated", "nodesUnchanged", "nodesRetired",
              "relsAdded", "relsUpdated", "relsArchived")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── The context ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TraceContext:
    """Who/what/when/where for every write made inside a `trace_run` block.

    Frozen so a nested block cannot mutate its parent's attribution — a nested run
    gets its own copy via `replace()`. `stats` and `errors` are the deliberate
    exceptions: they are mutable containers shared with the run record, guarded by
    `_lock`, because the whole point of a run is to accumulate what it did.
    """

    runId: str
    pipeline: str = PIPELINE_UNKNOWN
    trigger: str = TRIGGER_UNKNOWN
    # Finer-grained than `pipeline`, and already meaningful in this codebase:
    # "code-analysis", "qa-test", "mcp:shop", "servicenow_cmdb", "file_upload".
    source: str = ""
    # Human-readable pointer at the thing the data came from — a repo URL and
    # branch, an uploaded filename, an API endpoint, an MCP server name. This is
    # what the UI shows under the source glyph, and it is what makes provenance
    # actionable rather than decorative.
    sourceDetail: str = ""
    sourceRecordId: str = ""
    actor: str = ""
    actorId: str = ""
    # The module that performed the write, for when a pipeline has several writers.
    writtenBy: str = ""
    sessionId: str = ""
    projectId: str = ""
    connectorIds: tuple[str, ...] = ()
    notes: str = ""
    parentRunId: str = ""
    versionNumber: str = ""
    # True only for the sentinel, so callers can special-case "no context" without
    # comparing against a magic runId.
    unattributed: bool = False

    stats: dict[str, int] = field(default_factory=dict, compare=False, repr=False)
    errors: list[str] = field(default_factory=list, compare=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    def bump(self, key: str, amount: int = 1) -> None:
        if amount == 0:
            return
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def fail(self, message: str) -> None:
        """Record a non-fatal error against the run.

        Ingestion deliberately survives one bad record, so these would otherwise be
        swallowed and a half-failed run would look identical to a clean one — which
        is exactly what the Data Loader UI shows today.
        """
        with self._lock:
            if len(self.errors) < 100:
                self.errors.append(str(message)[:500])

    def record_stats(self, **counts: int) -> None:
        """Set run counters from a caller that already has its own totals.

        Ingestion services return their own stats dicts, so for those the run does
        not need to count writes a second time — it just adopts what they report.
        """
        for key, value in counts.items():
            if value:
                self.bump(key, int(value))

    def snapshot_stats(self) -> dict[str, int]:
        with self._lock:
            return {key: self.stats.get(key, 0) for key in _STAT_KEYS}


UNATTRIBUTED = TraceContext(
    runId="",
    pipeline=PIPELINE_UNKNOWN,
    trigger=TRIGGER_UNKNOWN,
    actor="",
    unattributed=True,
)

_context: contextvars.ContextVar[TraceContext] = contextvars.ContextVar(
    "aura_trace_context", default=UNATTRIBUTED,
)


def current() -> TraceContext:
    """The active context, or UNATTRIBUTED. Never None."""
    return _context.get()


def is_active() -> bool:
    return not current().unattributed


# ── Stamping ─────────────────────────────────────────────────────────────────

def stamp(props: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge the active context into `props` — the half applied on every write.

    **A value the caller supplied always wins.** Provenance fills gaps; it never
    overwrites data. Two reasons, both load-bearing:

      * Existing `source` values are more specific than anything the context knows,
        and queries depend on them: `code_graph._archive_stale` selects by
        `source = 'code-analysis'` and `wipe.py` picks demo data by source. Letting
        the context overwrite those silently changes which nodes those own.

      * Names collide. `qatest.graph_writeback` writes a TestRun whose own `runId`
        is the test run — nothing to do with the ingestion run. Clobbering it would
        corrupt real data to record bookkeeping, which is never a good trade.

    Only the fields provenance genuinely owns are written unconditionally:
    `updatedAt`, `lastSeenAt`, `lastSeenRunId`, `attribution`.
    """
    ctx = current()
    out = dict(props or {})
    now = _now()

    # Owned outright — these describe this write, and a caller has no business
    # setting them.
    out["updatedAt"] = now
    out["lastSeenAt"] = now
    out["attribution"] = ATTRIBUTION_NONE if ctx.unattributed else ATTRIBUTION_TRACED
    if ctx.runId:
        out["lastSeenRunId"] = ctx.runId

    # Filled only where the caller left a gap. Empty values are skipped entirely:
    # an empty string in the graph is indistinguishable from a real one, and the UI
    # would then have to special-case it everywhere.
    for key, value in (
        ("source", ctx.source),
        ("pipeline", ctx.pipeline),
        ("trigger", ctx.trigger),
        ("sourceDetail", ctx.sourceDetail),
        ("sourceRecordId", ctx.sourceRecordId),
        ("actor", ctx.actor),
        ("actorId", ctx.actorId),
        ("writtenBy", ctx.writtenBy),
        # Kept as an alias of the run id: `GET /nodes/{id}/version`, the
        # ("Service","versionId") index and the existing Provenance tab all read
        # `versionId`, and they should keep working without a migration.
        ("versionId", ctx.runId),
    ):
        if value and not out.get(key):
            out[key] = value
    return out


def first_seen_props() -> dict[str, Any]:
    """The ON CREATE half — origin facts that must never be overwritten.

    Separated from `stamp` because `SET n += $props` cannot express "only if new",
    and a node's origin is the single most useful thing provenance records. If a
    nightly job overwrote `firstSeenAt`, every node in the graph would report that
    it was created last night.
    """
    ctx = current()
    out: dict[str, Any] = {"firstSeenAt": _now()}
    if ctx.runId:
        out["firstSeenRunId"] = ctx.runId
    if ctx.actor:
        out["createdBy"] = ctx.actor
    if ctx.pipeline:
        out["createdVia"] = ctx.pipeline
    return out


def edge_provenance(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Provenance for a relationship, in the shape `upsert_relationship` expects.

    Mirrors `ontology/schema.py::PROVENANCE_PROPS`, which already declares
    `source`/`discoveredBy`/`confidence`/`firstSeen`/`lastSeen`/`evidence`/`factType`
    for edges. `extra` (a caller's confidence, evidence, factType) wins over the
    context, since only the caller knows how it derived the edge.
    """
    ctx = current()
    out: dict[str, Any] = {}
    if ctx.source:
        out["source"] = ctx.source
    if ctx.writtenBy:
        out["discoveredBy"] = ctx.writtenBy
    out.update({k: v for k, v in (extra or {}).items() if v is not None})
    return out


# ── Runs ─────────────────────────────────────────────────────────────────────

@contextmanager
def trace_run(
    pipeline: str,
    *,
    trigger: str = TRIGGER_UNKNOWN,
    actor: str = "",
    actorId: str = "",
    source: str = "",
    sourceDetail: str = "",
    sourceRecordId: str = "",
    writtenBy: str = "",
    sessionId: str = "",
    projectId: str = "",
    connectorIds: Iterable[str] = (),
    notes: str = "",
    fileInfo: dict[str, Any] | None = None,
    record: bool = True,
) -> Generator[TraceContext, None, None]:
    """Attribute every graph write made inside this block to one run.

    Nests. An inner block inherits any field the caller left unset and records the
    outer run as its parent, so `analyse` → `sync_project` → an analyzer agent reads
    as one tree rather than three unrelated runs.

    `record=False` suppresses the DynamoDB run record while still attributing the
    writes — for hot paths that write a handful of nodes and would otherwise put a
    row in the ledger for every request.
    """
    parent = current()
    run_id = str(uuid.uuid4())

    def inherit(value: str, fallback: str) -> str:
        return value or (fallback if not parent.unattributed else "")

    ctx = TraceContext(
        runId=run_id,
        pipeline=pipeline or inherit("", parent.pipeline) or PIPELINE_UNKNOWN,
        trigger=trigger if trigger != TRIGGER_UNKNOWN else (
            parent.trigger if not parent.unattributed else TRIGGER_UNKNOWN),
        source=inherit(source, parent.source),
        sourceDetail=inherit(sourceDetail, parent.sourceDetail),
        sourceRecordId=inherit(sourceRecordId, parent.sourceRecordId),
        actor=inherit(actor, parent.actor),
        actorId=inherit(actorId, parent.actorId),
        writtenBy=writtenBy or "",
        sessionId=inherit(sessionId, parent.sessionId),
        projectId=inherit(projectId, parent.projectId),
        connectorIds=tuple(connectorIds) or parent.connectorIds,
        notes=notes,
        parentRunId="" if parent.unattributed else parent.runId,
    )

    if record:
        ctx = replace(ctx, versionNumber=_open_run_record(ctx, fileInfo))

    token = _context.set(ctx)
    started = datetime.now(timezone.utc)
    status = "success"
    try:
        yield ctx
    except BaseException as exc:
        status = "failed"
        ctx.fail(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _context.reset(token)
        if record:
            duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            _close_run_record(ctx, status, duration_ms)


def _open_run_record(ctx: TraceContext, file_info: dict[str, Any] | None = None) -> str:
    """Create the run row up front, so an interrupted run is still visible.

    Best-effort throughout: a ledger that is unreachable must not take ingestion
    down with it. The graph write is the product; the run record is bookkeeping.
    """
    try:
        from src.services import ontology_version_service as versions
        record = versions.create_version_record(
            load_method=ctx.pipeline,
            actor=ctx.actor or "system",
            sources=list(ctx.connectorIds) or ([ctx.source] if ctx.source else []),
            notes=ctx.notes,
            file_info=file_info,
            run_id=ctx.runId,
            trigger=ctx.trigger,
            pipeline=ctx.pipeline,
            source_detail=ctx.sourceDetail,
            project_id=ctx.projectId,
            written_by=ctx.writtenBy,
            parent_run_id=ctx.parentRunId,
        )
        return str(record.get("versionNumber", ""))
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails ingestion
        log.warning("could not open run record for %s: %s", ctx.pipeline, exc)
        return ""


def _close_run_record(ctx: TraceContext, status: str, duration_ms: int) -> None:
    try:
        from src.services import ontology_version_service as versions
        versions.finish_version_record(
            ctx.runId, status, ctx.snapshot_stats(),
            duration_ms=duration_ms,
            errors=list(ctx.errors),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not close run record %s: %s", ctx.runId, exc)


@contextmanager
def refine(**fields: Any) -> Generator[TraceContext, None, None]:
    """Override descriptive fields on the current context, keeping the same run.

    For a run that reads several sources — an MCP load spanning three servers, say.
    Each node should record which server it came from, but they are all one run, so
    opening a nested `trace_run` would give each server its own `runId` and break
    "show me everything this run wrote".

    Shares the parent's `stats` and `errors` objects, so counting still accrues to
    the one run.
    """
    parent = current()
    ctx = replace(parent, **fields)
    # replace() copies the field values, which for stats/errors would give this
    # block its own containers and silently drop whatever it counted.
    object.__setattr__(ctx, "stats", parent.stats)
    object.__setattr__(ctx, "errors", parent.errors)
    object.__setattr__(ctx, "_lock", parent._lock)
    token = _context.set(ctx)
    try:
        yield ctx
    finally:
        _context.reset(token)


@contextmanager
def ensure_run(pipeline: str, **kwargs: Any) -> Generator[TraceContext, None, None]:
    """Open a run only if one is not already active; otherwise pass the parent through.

    For functions that sit in the middle — `observability.runbooks.save_runbook` is
    called both from inside the learning loop and directly from an upload route.
    Plain `trace_run` would work, but it would open a child run record for every
    runbook the loop touches, burying the one run an operator actually cares about
    under a pile of single-write children.
    """
    active = current()
    if not active.unattributed:
        yield active
        return
    with trace_run(pipeline, **kwargs) as ctx:
        yield ctx


# ── Crossing a thread boundary ───────────────────────────────────────────────

def traced_callable(fn: Callable, *args: Any, **kwargs: Any) -> Callable[[], Any]:
    """Bind `fn` to a copy of the CURRENT context, for running elsewhere.

    `threading.Thread` starts with a fresh, empty context — unlike an asyncio task,
    which copies. Several routers hand ingestion straight to a thread, so without
    this every one of those writes lands unattributed, and does so silently.

    Use for both `Thread(target=...)` and `run_in_executor`.
    """
    ctx = contextvars.copy_context()
    return functools.partial(ctx.run, functools.partial(fn, *args, **kwargs))


def spawn_traced(fn: Callable, *args: Any, **kwargs: Any) -> threading.Thread:
    """Start a daemon thread running `fn` inside the current trace context."""
    thread = threading.Thread(target=traced_callable(fn, *args, **kwargs), daemon=True)
    thread.start()
    return thread


# ── The traced write ─────────────────────────────────────────────────────────
#
# `graph/code_graph.py` and `qatest/graph_writeback.py` each grew the same
# diff-then-audit routine independently, and every other ingestion path grew none —
# which is why a node loaded from MCP or an uploaded file shows a lineage ribbon but
# an empty timeline: nothing ever recorded that it was created.
#
# Lifting it here gives every writer the same discipline, and keeps the
# diff-only rule in one place instead of two-and-counting.

# Bookkeeping the differ must ignore, or every write looks like a change and the
# "only record real changes" rule collapses into "record everything".
_VOLATILE = frozenset({
    "createdAt", "updatedAt", "lastSeenAt", "lastSeenRunId", "versionId",
    "attribution", "pipeline", "trigger", "actor", "actorId", "writtenBy",
    "sourceDetail", "sourceRecordId", "firstSeenAt", "firstSeenRunId",
    "createdBy", "createdVia", "versionedAt", "lastSeen", "firstSeen",
})


def changed_fields(before: dict, after: dict, managed: Iterable[str]) -> dict:
    """Managed fields whose value actually differs. `{field: (old, new)}`."""
    diff = {}
    for key in managed:
        if key in _VOLATILE:
            continue
        old, new = before.get(key), after.get(key)
        if old != new:
            diff[key] = (old, new)
    return diff


def traced_upsert(
    label: str,
    external_id: str,
    props: dict[str, Any],
    *,
    name: str | None = None,
    session_id: str = "",
    managed: Iterable[str] | None = None,
    actor: str = "",
    graph: Any = None,
    store: Any = None,
) -> tuple[str, bool, dict]:
    """Upsert a node and record history only if a managed value actually changed.

    Returns `(element_id, created, diff)`. Raises nothing on the history write:
    losing an audit row is bad, but failing an ingestion because the changelog
    table is unreachable would be worse — the graph write has already committed.

    `graph` and `store` exist so a caller that holds its own module reference can
    pass it through. `code_graph` does, and its tests substitute an in-memory
    double there; importing the real client here would reach around that seam and
    make the fake silently inert.
    """
    neo4j = graph
    if neo4j is None:
        from src.graph import neo4j_client as neo4j  # noqa: PLC0415

    before, after, element_id, created = neo4j.upsert_node_returning_id(
        label, external_id, props)
    if not element_id:
        return "", False, {}

    keys = list(managed) if managed is not None else list(props)
    diff = changed_fields(before, after, keys)
    note_node(created=created, changed=bool(diff))

    if created or diff:
        record_change(
            change_type="CREATE" if created else "UPDATE",
            entity_kind="node",
            element_id=element_id,
            external_id=external_id,
            label=label,
            name=name or props.get("name") or external_id,
            before=None if created else {k: v[0] for k, v in diff.items()},
            after=after if created else {k: v[1] for k, v in diff.items()},
            session_id=session_id,
            actor=actor,
            # The row's source should match the node's own, which is more specific
            # than the run's — `code-analysis`, not the generic `dev-mate`.
            source=str(props.get("source") or ""),
            graph=neo4j,
            store=store,
        )
    return element_id, created, diff


def record_change(
    *,
    change_type: str,
    entity_kind: str,
    element_id: str,
    external_id: str,
    label: str,
    name: str,
    before: Any,
    after: Any,
    session_id: str = "",
    actor: str = "",
    source: str = "",
    graph: Any = None,
    store: Any = None,
) -> None:
    """Dual-write history: an :AuditLog node plus an ontology-changelog row.

    Both best-effort, and both kept: the AuditLog node makes history traversable
    inside the graph, while the DynamoDB row is the copy that survives
    `graph/wipe.py --scope all`, which deletes AuditLog nodes along with everything
    else.
    """
    neo4j = graph
    if neo4j is None:
        from src.graph import neo4j_client as neo4j  # noqa: PLC0415
    dynamo = store
    if dynamo is None:
        from src.database import dynamo_client as dynamo  # noqa: PLC0415

    ctx = current()
    # An explicit actor wins over the ambient one, for the same reason a caller's
    # own `source` does in `stamp`: `sync_project(actor=...)` is told who asked for
    # the analysis, and that is more specific than whatever opened the run.
    actor = actor or ctx.actor or "system"
    try:
        neo4j.write_audit_log(actor=actor, action=f"{change_type}:{label}",
                              target_id=element_id, before=before, after=after)
    except Exception as exc:  # noqa: BLE001
        log.debug("audit node failed for %s: %s", external_id, exc)
    try:
        dynamo.write_changelog(dynamo.build_changelog_entry(
            entity_id=element_id, entity_type=entity_kind.title(),
            entity_label=label, entity_name=name,
            change_type=change_type, actor=actor,
            before=before, after=after,
            session_id=session_id or ctx.sessionId or ctx.runId,
            source=source or ctx.source or "unknown",
            external_id=external_id,
            entity_kind=entity_kind,
        ))
    except Exception as exc:  # noqa: BLE001
        log.debug("changelog failed for %s: %s", external_id, exc)


def traced_link(
    from_label: str, from_eid: str, to_label: str, to_eid: str, rel_type: str,
    *,
    fact_type: str = "known",
    confidence: float | None = None,
    evidence: list | None = None,
    props: dict | None = None,
) -> bool:
    """Upsert a relationship with provenance, counting it against the run."""
    from src.graph import neo4j_client as neo4j
    try:
        ok = neo4j.upsert_relationship(
            from_label, from_eid, to_label, to_eid, rel_type,
            props=props,
            provenance_props={"factType": fact_type, "confidence": confidence,
                              "evidence": evidence},
        )
    except Exception as exc:  # noqa: BLE001 — one bad edge must not abort a load
        log.debug("link %s->%s failed: %s", from_eid, to_eid, exc)
        note_error(f"link {from_eid}->{to_eid} [{rel_type}]: {exc}")
        return False
    if ok:
        note_rel()
    return ok


# ── Accounting helpers ───────────────────────────────────────────────────────

def note_node(created: bool, changed: bool = False) -> None:
    ctx = current()
    if created:
        ctx.bump("nodesAdded")
    elif changed:
        ctx.bump("nodesUpdated")
    else:
        ctx.bump("nodesUnchanged")


def note_rel(created: bool = True) -> None:
    current().bump("relsAdded" if created else "relsUpdated")


def note_error(message: str) -> None:
    current().fail(message)

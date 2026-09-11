"""Delete everything a project owns.

The endpoint this replaces removed one DynamoDB row and orphaned the rest — the graph
nodes, the S3 evidence, the working copy, every test run. This does the whole job, and
tells the user what it is about to do first.

**The registry is data, not control flow.** `SCOPES` below is the single declaration of
what a project owns, in the same shape as `dynamo_client.TABLE_SCHEMAS`. The preview
and the delete iterate the same tuple, so they cannot disagree about scope — which is
the failure that turns a confirmation dialog into a lie.

**Order matters more than atomicity.** There is no cross-store transaction and pretending
otherwise would be worse than admitting it, so the rule is: *delete the thing that makes
the project discoverable last.* A crash leaves a project that is still listed, still
openable and still re-deletable. Idempotence comes from that ordering, not from a
rollback we cannot perform.

**What is deliberately kept**: `ontology-changelog`, because the record of this deletion
has to outlive it; and `gateway-audit-log`, the LLM spend ledger, which expires on its
own 90-day TTL. Both are listed in the preview so the omission is visible rather than
silent.
"""
from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

class _LookupFailed(RuntimeError):
    """A store could not be read. Distinct from "it had nothing"."""


#: Runner liveness rows in `test-results` belong to no project.
RUNNER_SENTINEL = "_runners"


@dataclass(frozen=True)
class Scope:
    """One store a project writes to, and how to find its rows."""
    table: str
    strategy: str                    # query | gsi | cascade | scan
    key_attrs: tuple[str, ...]       # the BASE table key, for delete_item
    pk: str = ""
    index: str = ""
    parent: str = ""                 # cascade: the scope supplying the ids
    parent_attr: str = ""            # cascade: which attribute of the parent row
    child_pk: str = ""               # cascade: the child's pk name
    exclude: Callable[[dict], bool] | None = None
    keep: bool = False
    why: str = ""


def _is_runner_row(row: dict) -> bool:
    return (row.get("projectId") == RUNNER_SENTINEL
            or str(row.get("testRunId", "")).startswith("runner:"))


#: Ordered: cascades before their parents, and `projects` last — see the module note.
SCOPES: tuple[Scope, ...] = (
    # ── cascades, before the rows that index them ──
    Scope("ai-spans", "cascade", ("traceId", "spanSortKey"),
          parent="ai-traces", parent_attr="traceId", child_pk="traceId"),
    Scope("observability-evidence", "cascade", ("investigationId", "evidenceId"),
          parent="observability-investigations", parent_attr="investigationId",
          child_pk="investigationId"),
    Scope("observability-outcomes", "cascade", ("investigationId", "recordedAt"),
          parent="observability-investigations", parent_attr="investigationId",
          child_pk="investigationId"),

    # ── keyed or indexed by projectId ──
    Scope("ai-traces", "query", ("projectId", "sortKey"), pk="projectId"),
    Scope("services", "query", ("projectId", "serviceId"), pk="projectId"),
    Scope("token-usage", "gsi", ("userId", "sortKey"),
          pk="projectId", index="projectId-timestamp-index"),
    Scope("migration-sessions", "gsi", ("sessionId", "projectId"),
          pk="projectId", index="projectId-createdAt-index"),
    Scope("observability-investigations", "gsi", ("investigationId", "createdAt"),
          pk="projectId", index="projectId-createdAt-index"),

    # ── projectId is an attribute only ──
    Scope("connectors", "scan", ("connectorId", "projectId")),
    Scope("test-results", "scan", ("testRunId", "projectId"), exclude=_is_runner_row,
          why="runner liveness rows belong to no project"),
    Scope("sops", "scan", ("sopId", "projectId")),
    Scope("chat-sessions", "scan", ("sessionId", "userId")),
    Scope("agents", "scan", ("agentRunId", "orchestratorId")),
    Scope("activity", "scan", ("userId", "timestamp")),
    Scope("ontology-versions", "scan", ("versionId",)),

    # ── kept ──
    Scope("ontology-changelog", "scan", ("changeId",), keep=True,
          why="the record of this deletion has to outlive it"),
    Scope("gateway-audit-log", "scan", ("requestId", "timestamp"), keep=True,
          why="the LLM spend ledger; it expires on its own 90-day TTL"),

    # ── the commit point ──
    Scope("projects", "query", ("projectId", "userId"), pk="projectId"),
)

#: (bucket suffix, prefix template). Every prefix ends in "/" so `p1/` cannot match
#: `p10/` — a bug that would delete a different project's evidence.
S3_PREFIXES: tuple[tuple[str, str], ...] = (
    ("analysis", "{pid}/"),
    ("analysis", "generated/{pid}/"),
    ("analysis", "aiobs/{pid}/"),
    ("uploads", "folders/{pid}/"),
    ("test-artifacts", "{pid}/"),
    ("exports", "{pid}/"),
    ("exports", "rca/{pid}/"),
    ("exports", "migrations/{pid}/"),
    ("exports", "observability/{pid}/"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reject(project_id: str) -> str:
    """Why this id must never be used as a delete scope. Empty means fine."""
    if not project_id or not project_id.strip():
        return "a blank project id matches everything"
    if "/" in project_id:
        return "a project id containing '/' would widen every S3 prefix"
    if project_id == RUNNER_SENTINEL:
        return f"{RUNNER_SENTINEL!r} is the runner sentinel, not a project"
    return ""


# ── Finding rows ──────────────────────────────────────────────────────────────

def _rows_for(scope: Scope, project_id: str, found: dict[str, list[dict]]) -> list[dict]:
    """The rows this scope owns. `found` carries earlier scopes' rows for cascades."""
    from src.database import dynamo_client as db

    try:
        if scope.strategy == "query":
            rows = db.query_items(scope.table, scope.pk, project_id, limit=2000) or []
        elif scope.strategy == "gsi":
            rows = db.query_items(scope.table, scope.pk, project_id,
                                  index_name=scope.index, limit=2000) or []
        elif scope.strategy == "cascade":
            parents = found.get(scope.parent) or []
            ids = {p.get(scope.parent_attr) for p in parents if p.get(scope.parent_attr)}
            rows = []
            for value in ids:
                rows.extend(db.query_items(scope.table, scope.child_pk, value,
                                           limit=2000) or [])
        else:                                                 # scan
            rows = [r for r in (db.scan_items(scope.table, limit=2000) or [])
                    if r.get("projectId") == project_id]
    except Exception as exc:                                  # noqa: BLE001
        # A lookup that FAILED is not a table with no rows, and conflating them is how
        # a delete quietly leaves data behind while the dialog says there was none.
        # Seen for real: a table whose GSI had not been created yet reported zero.
        log.warning("could not list %s for %s: %s", scope.table, project_id, exc)
        raise _LookupFailed(f"{scope.table}: {exc}") from exc

    if scope.exclude:
        rows = [r for r in rows if not scope.exclude(r)]
    return rows


def inventory(project_id: str, run_id: str = "") -> dict:
    """Everything that would be deleted, per store. Reads only.

    `run_id` is the deletion's own provenance run: its `ontology-versions` row must not
    be swept, or `_close_run_record` resurrects it as a stub (a bare SET upserts), and
    the stub has no `startedAt` so it never appears in the feed — present but invisible.
    """
    from src.graph import project_purge
    from src.storage import s3_client

    found: dict[str, list[dict]] = {}
    tables: list[dict] = []
    # Discovery order is NOT delete order. Deleting goes cascades-first so a child is
    # never orphaned behind a deleted index row; FINDING has to go parents-first, or a
    # cascade looks for ids its parent has not produced yet. They were the same loop,
    # and every cascaded table silently inventoried as empty — then survived the delete.
    unreadable: dict[str, str] = {}
    for scope in sorted(SCOPES, key=lambda sc: sc.strategy == "cascade"):
        try:
            rows = _rows_for(scope, project_id, found)
        except _LookupFailed as exc:
            unreadable[scope.table] = str(exc)
            rows = []
        if scope.table == "ontology-versions" and run_id:
            rows = [r for r in rows if r.get("versionId") != run_id]
        found[scope.table] = rows
    for scope in SCOPES:
        entry = {"table": scope.table, "rows": len(found.get(scope.table) or []),
                 "kept": scope.keep, "why": scope.why, "method": scope.strategy,
                 "counted": scope.table not in unreadable}
        if scope.table in unreadable:
            entry["rows"] = None
            entry["reason"] = unreadable[scope.table]
        tables.append(entry)

    objects, total_bytes, prefixes = 0, 0, []
    for bucket, template in S3_PREFIXES:
        prefix = template.format(pid=project_id)
        count = size = 0
        for obj in s3_client.iter_objects(bucket, prefix):
            count += 1
            size += int(obj.get("size") or 0)
        if count:
            prefixes.append({"bucket": bucket, "prefix": prefix,
                             "objects": count, "bytes": size})
        objects += count
        total_bytes += size

    workspace = _workspace_stats(project_id)
    graph = project_purge.inventory(project_id)

    return {
        "projectId": project_id,
        "generatedAt": _now(),
        "dynamodb": {"tables": tables,
                     "totalRows": sum(t["rows"] or 0 for t in tables if not t["kept"]),
                     # False means at least one store could not be read, so the total
                     # is a floor rather than an answer.
                     "exact": not unreadable,
                     "unreadable": unreadable},
        "s3": {"objects": objects, "bytes": total_bytes, "prefixes": prefixes},
        "workspace": workspace,
        "graph": graph,
        "_rows": found,
    }


def _workspace_stats(project_id: str) -> dict:
    from pathlib import Path

    from src.routers import git_ops

    try:
        path = git_ops._clone_path(project_id)
    except Exception:                                         # noqa: BLE001
        return {"exists": False, "path": "", "files": 0, "bytes": 0}
    if not Path(path).is_dir():
        return {"exists": False, "path": str(path), "files": 0, "bytes": 0}

    files = size = 0
    for entry in Path(path).rglob("*"):
        if entry.is_file():
            files += 1
            try:
                size += entry.stat().st_size
            except OSError:
                pass
    return {"exists": True, "path": str(path), "files": files, "bytes": size}


def excluded_notes(project_id: str, inv: dict) -> list[dict]:
    """What is NOT deleted, and why. The part of the dialog a user must read."""
    from src.database import dynamo_client as db
    from src.graph import project_purge

    notes: list[dict] = [
        {"what": "Shared graph nodes",
         "detail": "Kafka topic DataFlow nodes, shared Service nodes and Runbooks are "
                   "pointed at by other projects. Their links to this project go; the "
                   "nodes stay.",
         "count": sum(sum(e.get("excluded", {}).values())
                      for e in inv["graph"]["engines"].values() if e.get("ok"))},
    ]
    for scope in SCOPES:
        if scope.keep:
            notes.append({"what": scope.table, "detail": scope.why, "count": None})

    try:
        children = [r for r in (db.scan_items("projects", limit=500) or [])
                    if r.get("migratedFrom") == project_id]
    except Exception:                                         # noqa: BLE001
        children = []
    for child in children:
        notes.append({
            "what": child.get("name") or child.get("projectId"),
            "detail": "an independent project created by a migration from this one. It "
                      "is kept; its link back will dangle.",
            "count": None})

    notes.append({
        "what": "Runner working copies",
        "detail": "A self-hosted QA runner keeps its own copy of the code on the "
                  "developer's machine. The API has no channel to it — delete it there.",
        "count": None})
    return notes


# ── Deleting ──────────────────────────────────────────────────────────────────

@dataclass
class Report:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    dynamodb: dict = field(default_factory=dict)
    s3: dict = field(default_factory=dict)
    workspace: dict = field(default_factory=dict)
    graph: dict = field(default_factory=dict)
    outbox: int = 0

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message[:300])

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "dynamodb": self.dynamodb,
                "s3": self.s3, "workspace": self.workspace, "graph": self.graph,
                "outbox": self.outbox}


def delete_project(project_id: str, actor: str, run_id: str = "") -> Report:
    """Delete everything, in the order that makes a crash survivable.

    Graph first (likeliest to fail, and nothing is lost yet when it does), then S3, the
    workspace, the satellite tables, and the `projects` row last. The caller must not
    delete that row if this reports `ok: False` — it is the handle to what is left.
    """
    from src.database import dynamo_client as db
    from src.graph import project_purge
    from src.storage import s3_client

    report = Report()
    reason = _reject(project_id)
    if reason:
        report.fail(reason)
        return report

    inv = inventory(project_id, run_id=run_id)
    unreadable = inv["dynamodb"].get("unreadable") or {}
    if unreadable:
        # Deleting what we could see and calling it done is how data survives a delete
        # that reported success.
        for table, why in unreadable.items():
            report.fail(f"could not read {table}: {why}")
        return report

    # 1. Graph.
    graph = project_purge.purge_project(project_id)
    report.graph = graph
    if not graph.get("ok"):
        for result in graph.get("results", []):
            if not result.get("ok"):
                report.fail(f"graph[{result.get('backend')}]: {result.get('error')}")
        if report.ok:
            # `ok: False` with no per-engine detail still means it failed. Relying on
            # the loop to mark it left the report claiming success and the caller went
            # on to delete everything else.
            report.fail(graph.get("error") or "the graph purge did not succeed")
        return report                      # nothing else is touched
    report.outbox = project_purge.drain_outbox_for(project_id)

    # 2. S3.
    deleted_objects = 0
    for bucket, template in S3_PREFIXES:
        prefix = template.format(pid=project_id)
        try:
            out = s3_client.delete_prefix(bucket, prefix)
            deleted_objects += out["deleted"]
            for err in out["errors"]:
                report.fail(f"s3[{bucket}/{prefix}]: {err}")
        except Exception as exc:                              # noqa: BLE001
            report.fail(f"s3[{bucket}/{prefix}]: {exc}")
    report.s3 = {"deleted": deleted_objects}
    if not report.ok:
        return report

    # 3. Workspace.
    workspace = inv["workspace"]
    if workspace.get("exists"):
        try:
            shutil.rmtree(workspace["path"], ignore_errors=False)
            report.workspace = {"deleted": True, "path": workspace["path"]}
        except Exception as exc:                              # noqa: BLE001
            report.fail(f"workspace: {exc}")
            return report
    else:
        report.workspace = {"deleted": False, "path": workspace.get("path", "")}

    # 4. DynamoDB, in registry order — cascades first, `projects` last.
    per_table: dict[str, int] = {}
    for scope in SCOPES:
        if scope.keep:
            continue
        rows = inv["_rows"].get(scope.table) or []
        removed = 0
        for row in rows:
            key = {attr: row.get(attr) for attr in scope.key_attrs}
            if any(v is None for v in key.values()):
                continue
            try:
                db.delete_item(scope.table, key)
                removed += 1
            except Exception as exc:                          # noqa: BLE001
                report.fail(f"{scope.table}: {exc}")
        per_table[scope.table] = removed
        # The projects row is the commit point: if anything before it failed, stop
        # rather than destroy the only handle to a half-deleted project.
        if scope.table != "projects" and not report.ok:
            report.dynamodb = per_table
            return report
    report.dynamodb = per_table
    return report


# ── Audit that outlives what it describes ─────────────────────────────────────

def record(project_id: str, project: dict, actor: str, change_type: str,
           before: dict | None, after: dict | None) -> None:
    """Write one changelog row.

    `ontology-changelog` and NOT the in-graph `:AuditLog`: `write_audit_log` attaches
    the audit node via `AUDITED_BY` to the node being deleted, so the edge dies with it
    and the record is left orphaned and unfindable. `wipe._record` skips the graph half
    for the same reason.
    """
    from src.database import dynamo_client as db

    try:
        db.write_changelog(db.build_changelog_entry(
            entity_id=f"project:{project_id}",
            external_id=f"project:{project_id}",
            entity_type="Node",
            entity_label="Project",
            entity_name=project.get("name") or project_id,
            change_type=change_type,
            actor=actor,
            before=before,
            after=after,
            session_id=f"delete:{project_id}",
            source="api",
            notes="project deletion",
        ))
    except Exception as exc:                                  # noqa: BLE001
        # A lost audit row must not abort a delete that is already under way.
        log.error("could not record %s for %s: %s", change_type, project_id, exc)

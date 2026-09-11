"""Delete one project's nodes and edges, and nothing else's.

The second hard delete in a layer whose stated contract is *never delete*
(`code_graph.py:14`; `retire_node` and `archive_relationship` are both soft). The first
is `wipe.py`, which is global. This one is scoped, and the scoping is the whole problem:

**Some nodes are shared between projects.** `dataflow:topic:{slug}`
(`repo_ingestion_service.py:341`) carries no project in its externalId, and `upsert()`
overwrites its `projectId` with whichever project ingested it last. Deleting "the
project's" DataFlow nodes by that property therefore deletes a node that another
project's Services point at. Bare `service:{name}` nodes from the observability path
and `Runbook` have the same shape. They are excluded by label and by externalId prefix,
and `DETACH DELETE` removes the edges into them while leaving them standing — which is
exactly the outcome wanted.

**Match on the `projectId` PROPERTY, never an externalId prefix.** There are two Project
conventions — `project:{id}` (`code_graph.py:215`) and bare `{id}`
(`repo_ingestion_service.py:275`) — and neither covers Repository, Dependency, API,
Service, TestRun or TestCase.

**`$pid` is a parameter, not interpolated.** `wipe.match_clause` builds its Cypher by
interpolation because every value in it is a module constant. Here the value arrives
from a URL path, so interpolating it is an injection. Anyone copying wipe.py's style
into this file would introduce one.

**Per-backend sessions, like `wipe.py`, not `neo4j_client.run_query`.** `run_query`
fans out and returns only the primary's result, so a failed mirror is swallowed and
queued in the outbox — and an outbox row containing `DETACH DELETE` is a delayed charge
that fires against a store which may since have been re-seeded. Deleting is the one
operation where "eventually, probably" is not good enough. It also lets each engine be
counted and verified on its own.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: Labels that are never project-scoped, whatever `projectId` happens to be on them.
#: Enterprise-level facts and shared knowledge: deleting them with a project would take
#: away things other projects depend on and nobody asked to remove.
SHARED_LABELS = (
    "Runbook", "Organization", "Team", "Enterprise", "BusinessUnit",
    "Infrastructure", "Incident", "Alert", "ChangeRequest",
    "SecurityFinding", "Vulnerability", "AttackPath",
    "IAMRole", "IAMPolicy", "MCPServer", "AuditLog",
)

#: externalId prefixes that are shared even though the node carries a `projectId`.
#: `dataflow:topic:` is the dangerous one — see the module docstring.
SHARED_EID_PREFIXES = ("dataflow:topic:",)

#: Nodes per DETACH DELETE. Smaller than wipe.py's 5000 because this runs against a
#: live system, and because a smaller batch makes progress durable: a crash mid-loop
#: leaves less to redo.
BATCH = 2000

#: Above this, refuse and tell the operator. There is no index on `projectId`
#: (`neo4j_client.NODE_INDEXES`), so the predicate is a full scan; a project with a
#: pathological node count should not be deleted inside a request.
MAX_NODES = 50_000

#: The scope predicate. Every value in it is a parameter.
_SCOPE = ("n.projectId = $pid "
          "AND NOT any(l IN labels(n) WHERE l IN $shared) "
          "AND NOT any(p IN $prefixes WHERE coalesce(n.externalId, '') STARTS WITH p)")

#: Matched by `projectId` but deliberately spared — what the preview explains.
_EXCLUDED = ("n.projectId = $pid "
             "AND (any(l IN labels(n) WHERE l IN $shared) "
             "     OR any(p IN $prefixes WHERE coalesce(n.externalId, '') STARTS WITH p))")


def _params(project_id: str) -> dict:
    return {"pid": project_id, "shared": list(SHARED_LABELS),
            "prefixes": list(SHARED_EID_PREFIXES)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_by_label(backend, project_id: str) -> dict[str, int]:
    """What this project owns on one engine, per label."""
    with backend.session() as s:
        rows = s.run(f"MATCH (n) WHERE {_SCOPE} "
                     "RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC",
                     _params(project_id))
        return {(r["label"] or "?"): int(r["c"]) for r in rows}


def count_excluded(backend, project_id: str) -> dict[str, int]:
    """Nodes carrying this projectId that will be SPARED, per label.

    The preview shows this beside the deletion counts, because "41 nodes will go" is
    only half an answer if two more match and are being kept for a reason.
    """
    with backend.session() as s:
        rows = s.run(f"MATCH (n) WHERE {_EXCLUDED} "
                     "RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC",
                     _params(project_id))
        return {(r["label"] or "?"): int(r["c"]) for r in rows}


def count_crossing_edges(backend, project_id: str) -> int:
    """Edges from this project's nodes to something outside it.

    These die with the DETACH DELETE while the far node survives. Worth showing: it is
    the number that tells a reader their shared infrastructure will lose its links to
    this project and nothing more.
    """
    with backend.session() as s:
        row = s.run(f"MATCH (n)-[r]-(m) WHERE {_SCOPE} "
                    "AND coalesce(m.projectId, '') <> $pid "
                    "RETURN count(DISTINCT r) AS c", _params(project_id)).single()
    return int(row["c"]) if row else 0


def _total(backend, project_id: str) -> int:
    with backend.session() as s:
        row = s.run(f"MATCH (n) WHERE {_SCOPE} RETURN count(n) AS c",
                    _params(project_id)).single()
    return int(row["c"]) if row else 0


def purge_backend(backend, project_id: str) -> dict:
    """Delete one project from one engine. Before/after counts, never a bare success."""
    result: dict = {"backend": backend.name, "projectId": project_id}
    try:
        before = _total(backend, project_id)
        if before > MAX_NODES:
            result.update({"ok": False, "before": before, "deleted": 0,
                           "error": (f"{before} nodes match, above the {MAX_NODES} "
                                     "limit for a synchronous delete")})
            return result

        # LIMIT + loop rather than `CALL … IN TRANSACTIONS`: the batched form exists
        # only in the Neo4j dialect and is not one of Memgraph's rewrites, so it is not
        # portable. The loop is, and it leaves less to redo if it is interrupted.
        params = {**_params(project_id), "batch": BATCH}
        statement = (f"MATCH (n) WHERE {_SCOPE} WITH n LIMIT $batch "
                     "DETACH DELETE n RETURN count(*) AS deleted")
        deleted = 0
        with backend.session() as s:
            while True:
                row = s.run(statement, params).single()
                n = int(row["deleted"]) if row else 0
                deleted += n
                if n == 0:
                    break

        after = _total(backend, project_id)
        result.update({"before": before, "after": after, "deleted": deleted,
                       "ok": after == 0})
        if after:
            result["error"] = (f"{after} node(s) still match after the delete — "
                               "re-run, or check the engine logs")
    except Exception as exc:  # noqa: BLE001 — one engine failing must not hide the rest
        log.warning("project purge failed on %s: %s", backend.name, exc)
        result.update({"ok": False, "error": str(exc)[:300]})
    return result


def _targets():
    """Every configured write target, resolved. (name, backend_or_None, reason)."""
    from src.graph import backends, graph_config

    config = graph_config.get_config(refresh=True)
    names = list(config.write_targets) or backends.configured_names()
    for name in names:
        backend = backends.get_backend(name)
        if backend is None:
            yield name, None, "not configured"
        elif not backend.is_available():
            yield name, None, "not reachable"
        else:
            yield name, backend, ""


def preflight() -> list[str]:
    """Reasons this must not start. Empty means go.

    Refusing beats half-deleting. Clearing one engine of a dual-write pair leaves the
    mirror populated, and the data reappears the moment somebody switches the read
    source — which looks exactly like corruption and is very hard to diagnose.
    """
    problems = [f"graph engine {name!r} is {reason}"
                for name, backend, reason in _targets() if backend is None]

    from src.graph import outbox
    try:
        # depth() returns {backend: count}, not a number.
        pending = outbox.depth()
    except Exception:                                         # noqa: BLE001
        pending = {}
    for name, count in pending.items():
        if count:
            # A queued write for this project, replayed after the delete, resurrects it.
            problems.append(f"{name} has {count} write(s) pending replay — drain the "
                            f"outbox first or the delete will be undone")
    return problems


def inventory(project_id: str) -> dict:
    """What a purge would remove and spare, per engine. Reads only."""
    engines: dict[str, dict] = {}
    for name, backend, reason in _targets():
        if backend is None:
            engines[name] = {"ok": False, "error": reason}
            continue
        try:
            engines[name] = {
                "ok": True,
                "byLabel": count_by_label(backend, project_id),
                "excluded": count_excluded(backend, project_id),
                "crossingEdges": count_crossing_edges(backend, project_id),
            }
        except Exception as exc:                              # noqa: BLE001
            engines[name] = {"ok": False, "error": str(exc)[:300]}
    total = max((sum(e.get("byLabel", {}).values())
                 for e in engines.values() if e.get("ok")), default=0)
    return {"engines": engines, "nodes": total}


def purge_project(project_id: str) -> dict:
    """Delete a project from every configured write target."""
    if not project_id or not project_id.strip():
        return {"ok": False, "error": "a blank project id matches nothing safely",
                "results": []}

    results = []
    for name, backend, reason in _targets():
        if backend is None:
            results.append({"backend": name, "ok": False, "error": reason})
            continue
        results.append(purge_backend(backend, project_id))

    return {"projectId": project_id, "at": _now(), "results": results,
            "totalDeleted": sum(r.get("deleted", 0) for r in results),
            "ok": bool(results) and all(r.get("ok") for r in results)}


def drain_outbox_for(project_id: str, limit: int = 500) -> int:
    """Drop queued writes that mention this project. Returns how many.

    Belt and braces for anything enqueued WHILE the delete ran: a replay of one of
    those would put the project back.
    """
    import json

    from src.database import dynamo_client as db
    from src.graph import backends

    dropped = 0
    for name in backends.configured_names():
        try:
            rows = db.query_items("graph-outbox", "backend", name, limit=limit) or []
        except Exception:                                     # noqa: BLE001
            continue
        for row in rows:
            blob = json.dumps(row.get("params") or {}, default=str)
            if project_id not in blob:
                continue
            try:
                db.delete_item("graph-outbox", {"backend": row["backend"],
                                                "outboxId": row["outboxId"]})
                dropped += 1
            except Exception:                                 # noqa: BLE001
                pass
    return dropped

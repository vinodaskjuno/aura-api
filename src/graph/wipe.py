"""Delete graph data. The most destructive operation in the product.

Two scopes, and the difference matters more than it looks:

  demo   Nodes written by the seed script and the mock connectors. Recoverable by
         re-running src/scripts/seed_neo4j.py. Leaves analysed projects
         (source="code-analysis") and the :AuditLog history alone, because those
         carry no `source` property or a different one.

  all    Every node. NOT recoverable without an EFS snapshot, and it destroys the
         in-graph audit trail along with everything else.

Scoped to the graph deliberately. DynamoDB is *not* wipeable from the UI: the
`aura-` table prefix is shared across dev, staging and prod in one AWS account, so a
DynamoDB wipe has no environment boundary to rely on. Neo4j and Memgraph are ECS
services inside a single cluster, so a graph wipe cannot reach another environment.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Sources written by the seed script and the mock-MCP connectors.
#
# `servicenow_change` was missing from the equivalent list in reset-dev.sh, so a
# "synthetic" reset left those nodes behind. This is the single definition; the
# script should read it rather than keep a copy that drifts again.
DEMO_SOURCES = (
    "seed",
    "git",
    "servicenow",
    "servicenow_cmdb",
    "servicenow_change",
    "wiz",
)

SCOPE_DEMO = "demo"
SCOPE_ALL = "all"
SCOPES = (SCOPE_DEMO, SCOPE_ALL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def match_clause(scope: str) -> str:
    """The MATCH that selects what a scope deletes.

    Built from constants only — never from request data — because
    `bulk_delete_statement` interpolates it into Cypher directly.
    """
    if scope == SCOPE_ALL:
        return "MATCH (n)"
    quoted = ", ".join(f"'{s}'" for s in DEMO_SOURCES)
    return f"MATCH (n) WHERE n.source IN [{quoted}]"


def _count(backend, match: str) -> int:
    with backend.session() as s:
        row = s.run(f"{match} RETURN count(n) AS c").single()
    return int(row["c"]) if row else 0


def wipe_backend(backend, scope: str) -> dict:
    """Wipe one engine. Returns before/after counts rather than a bare success,
    so the operator can see what actually happened."""
    match = match_clause(scope)
    result: dict = {"backend": backend.name, "scope": scope}
    try:
        before = _count(backend, match)
        with backend.session() as s:
            s.run(backend.dialect.bulk_delete_statement(match)).consume()
        after = _count(backend, match)
        result.update({"before": before, "after": after,
                       "deleted": max(0, before - after), "ok": after == 0})
        if after:
            result["error"] = (f"{after} node(s) still match after the delete — "
                               "re-run, or check the engine logs")
    except Exception as exc:  # noqa: BLE001 — one engine failing must not hide the rest
        log.warning("wipe failed on %s: %s", backend.name, exc)
        result.update({"ok": False, "error": str(exc)[:300]})
    return result


def wipe_graph(scope: str, actor: str) -> dict:
    """Wipe every configured write target.

    All write targets, not just the read source: clearing one engine of a
    dual-write pair leaves the mirror populated, and the "deleted" data reappears
    the moment somebody switches the read source — which looks exactly like data
    corruption and is very hard to diagnose.
    """
    if scope not in SCOPES:
        return {"error": f"unknown scope {scope!r}", "results": []}

    from src.graph import backends, graph_config

    config = graph_config.get_config(refresh=True)
    names = list(config.write_targets) or backends.configured_names()

    results = []
    for name in names:
        backend = backends.get_backend(name)
        if backend is None:
            results.append({"backend": name, "ok": False, "error": "not configured"})
            continue
        if not backend.is_available():
            results.append({"backend": name, "ok": False, "error": "not reachable"})
            continue
        results.append(wipe_backend(backend, scope))

    report = {
        "scope": scope,
        "actor": actor,
        "at": _now(),
        "results": results,
        "totalDeleted": sum(r.get("deleted", 0) for r in results),
        "ok": bool(results) and all(r.get("ok") for r in results),
    }
    _record(report)
    return report


def _record(report: dict) -> None:
    """Write the wipe to the DynamoDB changelog.

    Deliberately not the in-graph :AuditLog — a full wipe deletes that. The record
    of a deletion has to outlive what it deleted.
    """
    try:
        from src.database import dynamo_client as db
        db.write_changelog(db.build_changelog_entry(
            entity_id=f"graph-wipe:{report['at']}",
            external_id=f"graph-wipe:{report['at']}",
            entity_type="Graph",
            entity_label="GraphWipe",
            entity_name=f"{report['scope']} wipe",
            change_type="WIPE_GRAPH",
            actor=report["actor"],
            before={"engines": [r.get("backend") for r in report["results"]],
                    "counts": {r.get("backend"): r.get("before") for r in report["results"]}},
            after={"deleted": report["totalDeleted"], "ok": report["ok"]},
            source="ui",
            notes=f"Graph wipe ({report['scope']}) by {report['actor']}",
        ))
    except Exception as exc:  # noqa: BLE001 — a lost audit row must not fail the wipe
        log.error("could not record graph wipe in the changelog: %s", exc)

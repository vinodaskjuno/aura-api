"""Project the result of code analysis into Neo4j, with audit history.

Creating a project wrote DynamoDB and S3 but never reached the graph, so Onto Verse
and DevMate's "Load Context" had nothing to show for a project you had just created.
This is the missing writer.

Three rules shape everything here:

1. **Upsert, never duplicate.** Every node has a deterministic `externalId`, so
   re-analysing a project converges on the same nodes rather than growing the graph.
2. **Audit only real changes.** Re-analysing an unchanged repo writes no history.
   Auditing every run would bury the real edits and grow `ontology-changelog`
   without bound.
3. **Never delete.** A dependency that disappears from a manifest has its
   relationship archived (`active: false`), matching the contract the rest of the
   graph layer already keeps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.database import dynamo_client as dynamo
from src.graph import neo4j_client as neo4j
from src.services.code_parsers import RepoFacts, parse_repository

log = logging.getLogger(__name__)

SOURCE = "code-analysis"

# Caps keep one oversized repo from flooding the graph. Exceeding them is reported
# rather than silently truncated — a graph that quietly omits half a repo is worse
# than one that says it did.
MAX_DEPENDENCIES = 300
MAX_ROUTES = 200
MAX_SERVICES = 100


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    archived: int = 0
    repositories: int = 0
    truncated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": self.created, "updated": self.updated,
            "unchanged": self.unchanged, "archived": self.archived,
            "repositories": self.repositories,
            "truncated": self.truncated, "errors": self.errors,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    """Stable, readable id fragment. Colons are the separator, so they cannot survive.

    `@` and `/` are kept because npm scoped packages (@scope/pkg) and Go module
    paths are common enough that mangling them makes every id unreadable.
    """
    return "".join(c if c.isalnum() or c in "-._/@" else "_" for c in (text or "").strip())


# ── Upsert with audit ────────────────────────────────────────────────────────

# Fields written by other producers (ingestion, the maintainer UI) must survive a
# code-analysis run untouched, so the diff considers only what this module owns.
def _upsert(
    report: SyncReport, label: str, external_id: str, props: dict,
    actor: str, project_id: str,
) -> str | None:
    """Upsert one node and record history only if something actually changed.

    The diff-then-audit routine this used to carry now lives in
    `graph/provenance.traced_upsert`. It had been copied into
    `qatest/graph_writeback` and written nowhere else, so the paths that most
    needed it — MCP and file upload — had no history at all, and a node loaded
    from either showed a lineage ribbon above an empty timeline.
    """
    from src.graph import provenance

    props = {**props, "source": SOURCE, "status": "active"}
    try:
        element_id, created, diff = provenance.traced_upsert(
            label, external_id, props,
            name=props.get("name", external_id),
            session_id=f"analyse:{project_id}",
            actor=actor, graph=neo4j, store=dynamo,
        )
    except Exception as exc:  # noqa: BLE001 — one bad node must not abort the sync
        report.errors.append(f"{label} {external_id}: {exc}")
        log.warning("upsert failed %s %s: %s", label, external_id, exc)
        return None
    if not element_id:
        report.errors.append(f"{label} {external_id}: upsert returned no node")
        return None

    if created:
        report.created += 1
    elif diff:
        report.updated += 1
    else:
        report.unchanged += 1
    return element_id


def _link(from_label: str, from_eid: str, to_label: str, to_eid: str,
          rel: str, fact_type: str = "known") -> None:
    try:
        neo4j.upsert_relationship(
            from_label, from_eid, to_label, to_eid, rel,
            provenance_props={"source": SOURCE, "discoveredBy": "code_analysis",
                        "factType": fact_type},
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("link %s->%s failed: %s", from_eid, to_eid, exc)


# ── Archival ─────────────────────────────────────────────────────────────────

def _archive_stale(
    report: SyncReport, repo_eid: str, rel: str, live_eids: set[str],
    actor: str, project_id: str,
) -> None:
    """Deactivate relationships whose target is no longer produced by analysis.

    Scoped to one repository and one relationship type so a second repo in the same
    project cannot archive the first repo's edges.
    """
    cypher = """
    MATCH (r:Repository {externalId: $repo})-[rel]->(t)
    WHERE type(rel) = $rel_type AND coalesce(rel.active, true) = true
      AND NOT t.externalId IN $live
    SET rel.active = false, rel.archivedAt = $ts
    RETURN t.externalId AS eid, elementId(t) AS id,
           coalesce(t.name, t.externalId) AS name, labels(t)[0] AS label
    """
    try:
        rows = neo4j.run_query(cypher, {
            "repo": repo_eid, "rel_type": rel,
            "live": sorted(live_eids), "ts": _now(),
        })
    except Exception as exc:  # noqa: BLE001
        log.debug("archive scan failed for %s/%s: %s", repo_eid, rel, exc)
        return
    from src.graph import provenance
    for row in rows or []:
        report.archived += 1
        provenance.record_change(
            change_type="RELATIONSHIP_ARCHIVE", entity_kind="edge",
            element_id=row.get("id") or "", external_id=row.get("eid") or "",
            label=row.get("label") or "Node", name=row.get("name") or "",
            before={"active": True},
            after={"active": False, "relationship": rel},
            session_id=f"analyse:{project_id}",
            actor=actor, graph=neo4j, store=dynamo,
        )


# ── Entry point ──────────────────────────────────────────────────────────────

def _repo_roots(connectors: list[dict]) -> list[tuple[str, Path]]:
    """(label, path) for every connector that points at a readable directory.

    MCP connectors are skipped: they are not file-based, which is the same reason
    code_analysis_agent skips them.
    """
    roots: list[tuple[str, Path]] = []
    for conn in connectors:
        if conn.get("sourceType") == "mcp":
            continue
        raw = (conn.get("localPath") or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_dir():
            log.info("connector path is not a directory, skipping: %r", raw)
            continue
        label = path.name or conn.get("repoType") or "repo"
        roots.append((label, path))
    return roots


def sync_project(
    project: dict, connectors: list[dict], actor: str,
    facts_by_label: dict[str, RepoFacts] | None = None,
) -> SyncReport:
    """Project a project's analysed code into Neo4j. Safe to call repeatedly.

    `facts_by_label` lets a caller (or a test) supply pre-parsed results instead of
    re-walking the filesystem.
    """
    report = SyncReport()
    project_id = str(project.get("projectId") or "").strip()
    project_name = (project.get("name") or project_id or "").strip()
    if not project_id or not project_name:
        report.errors.append("project is missing projectId or name")
        return report

    if not neo4j.is_available():
        # Analysis still produced its DynamoDB/S3 output; the graph simply lags.
        report.errors.append("neo4j unavailable — graph not updated")
        return report

    project_eid = f"project:{project_id}"
    _upsert(report, "Project", project_eid, {
        # get_project_subgraph matches on lower(name), so this is what makes
        # DevMate's Load Context and the wizard's KG step find the project.
        "name": project_name,
        "projectId": project_id,
        "description": project.get("description") or "",
        "environment": project.get("environment") or "",
    }, actor, project_id)

    if facts_by_label is not None:
        parsed = list(facts_by_label.items())
    else:
        parsed = [(label, parse_repository(path))
                  for label, path in _repo_roots(connectors)]

    if not parsed:
        report.errors.append("no readable local repository for this project")
        return report

    for label, facts in parsed:
        _sync_repo(report, project_id, project_eid, label, facts, actor)
        report.repositories += 1

    return report


def _sync_repo(
    report: SyncReport, project_id: str, project_eid: str,
    label: str, facts: RepoFacts, actor: str,
) -> None:
    repo_eid = f"repo:{project_id}:{_slug(label)}"
    _upsert(report, "Repository", repo_eid, {
        "name": label,
        "projectId": project_id,
        # Neo4j properties must be primitives or flat lists — a dict would be
        # rejected, so the language histogram is flattened to "Python:12" pairs.
        "languages": [f"{k}:{v}" for k, v in sorted(facts.languages.items())],
        "techStack": facts.tech_stack,
        "fileCount": facts.file_count,
    }, actor, project_id)
    _link("Project", project_eid, "Repository", repo_eid, "HAS_REPOSITORY")

    def _cap(items: list, limit: int, what: str) -> list:
        if len(items) > limit:
            report.truncated.append(f"{label}: {len(items)} {what}, kept {limit}")
            return items[:limit]
        return items

    dep_eids: set[str] = set()
    for dep in _cap(facts.dependencies, MAX_DEPENDENCIES, "dependencies"):
        eid = f"dep:{project_id}:{dep.ecosystem}:{_slug(dep.name)}"
        dep_eids.add(eid)
        _upsert(report, "Dependency", eid, {
            "name": dep.name, "ecosystem": dep.ecosystem,
            "version": dep.version, "scope": dep.scope,
            "projectId": project_id,
        }, actor, project_id)
        _link("Repository", repo_eid, "Dependency", eid, "DEPENDS_ON")

    api_eids: set[str] = set()
    for route in _cap(facts.routes, MAX_ROUTES, "routes"):
        eid = f"api:{project_id}:{route.method}:{_slug(route.path)}"
        api_eids.add(eid)
        _upsert(report, "API", eid, {
            "name": f"{route.method} {route.path}",
            "method": route.method, "path": route.path,
            "framework": route.framework, "sourceFile": route.file,
            "projectId": project_id,
        }, actor, project_id)
        # Routes come from regex, not a resolved router table — the edge says so.
        _link("Repository", repo_eid, "API", eid, "EXPOSES", fact_type="inferred")

    svc_eids: set[str] = set()
    for name in _cap(sorted(set(facts.services)), MAX_SERVICES, "services"):
        eid = f"service:{project_id}:{_slug(name)}"
        svc_eids.add(eid)
        _upsert(report, "Service", eid, {
            "name": name, "projectId": project_id,
        }, actor, project_id)
        _link("Repository", repo_eid, "Service", eid, "IMPLEMENTS", fact_type="inferred")

    _archive_stale(report, repo_eid, "DEPENDS_ON", dep_eids, actor, project_id)
    _archive_stale(report, repo_eid, "EXPOSES", api_eids, actor, project_id)
    _archive_stale(report, repo_eid, "IMPLEMENTS", svc_eids, actor, project_id)

"""Neo4j client for the enterprise ontology universe.

Wraps the neo4j driver with helpers for MERGE-based upserts,
relationship management, and audit logging.  Falls back gracefully
when Neo4j is not running so the rest of the app stays up.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

# Aliased: `provenance` is also the historical name of the keyword argument on
# upsert_relationship, and shadowing the module there was the whole reason that
# parameter is now called provenance_props.
from src.graph import provenance as prov_ctx

log = logging.getLogger(__name__)

# Connection handling and Cypher translation now live in src/graph/backends.py, so
# this module works against whichever engine a deployment configures. The queries
# below are still written in the Neo4j 5 dialect — that is the reference dialect,
# and the active backend translates on the way out.
#
# The name of this module is now a misnomer, but 45 modules import it. Keeping the
# import surface intact is what makes "the same backend code works as-is" true at
# the call-site level; `src/graph/graph_client.py` is the neutral alias for new code.


def active_backend():
    """The backend reads go to — the configured read source.

    Writes may additionally be mirrored to other engines; see backends.routed_session.
    """
    from src.graph import backends, graph_config
    try:
        return backends.get_backend(graph_config.get_config().read_source or None)
    except Exception:  # noqa: BLE001 — config trouble must not take the graph down
        return backends.get_backend()


def dialect():
    """Capabilities of the active engine — branch on these, never on engine name."""
    from src.graph.dialects import DIALECTS
    backend = active_backend()
    return backend.dialect if backend else DIALECTS["neo4j"]


def _get_driver():
    backend = active_backend()
    return backend.driver() if backend else None


def is_available() -> bool:
    backend = active_backend()
    return bool(backend and backend.is_available())


@contextmanager
def session() -> Generator:
    """Reads hit the configured source; writes fan out to every write target.

    Routing lives at this boundary on purpose. There are 102 `.run(...)` call sites
    across the codebase and none of them need to know whether a deployment runs one
    engine or two.
    """
    from src.graph import backends
    with backends.routed_session() as s:
        yield s


def close():
    from src.graph import backends
    backends.reset()


# ── Schema bootstrap ──────────────────────────────────────────────────────────

# Labels that get a unique-externalId constraint
_CONSTRAINED_LABELS = [
    # Legacy
    "Organization", "Project", "Service", "Repository", "Infrastructure",
    "Database", "SecurityFinding", "Incident", "Team",
    # Enterprise
    "Enterprise", "BusinessUnit", "BusinessDomain", "Product",
    # Business
    "BusinessProcess", "BusinessRule", "BusinessApplication", "Requirement", "Policy", "SOP",
    # Documents
    "Document", "WikiArticle", "ADR", "TechnicalSpec", "Runbook",
    # Ticket
    "Ticket",
    # Application
    "Application", "API", "Module", "Class", "Function", "Feature",
    # Code
    "CodeFile", "Dependency", "Configuration", "FeatureFlag",
    # Pipeline
    "BuildPipeline", "BuildArtifact", "Deployment", "DeploymentEnvironment",
    # Data
    "Table", "Column", "DataElement", "DataFlow",
    # Infra
    "Server", "VM", "Container", "KubernetesCluster", "CloudResource", "Network",
    # Security
    "Vulnerability", "AttackPath", "IAMRole", "IAMPolicy",
    # Identity
    "User", "Role", "ServiceAccount",
    # Operations
    "Alert", "ChangeRequest",
    # AI/ML
    "AIModel", "PromptRepository", "RAGKnowledgeBase",
    "VectorDatabase", "AgentDefinition", "MCPServer",
]


def _build_constraints_ddl() -> list[str]:
    # Constraint syntax is not portable — Neo4j 5 uses REQUIRE with a named,
    # IF NOT EXISTS constraint; Memgraph uses ASSERT and has neither. The dialect
    # owns the difference.
    d = dialect()
    return [d.constraint_ddl(label) for label in _CONSTRAINED_LABELS]


# Indexes are declared as (label, property) rather than as DDL strings, because the
# two engines disagree on the SHAPE of the statement, not merely its spelling:
# Memgraph has no named indexes and no IF NOT EXISTS. Each dialect renders its own.
NODE_INDEXES: list[tuple[str, str]] = [
    ("Service", "name"),
    ("Application", "name"),
    ("Repository", "name"),
    ("Infrastructure", "hostname"),
    ("Infrastructure", "ip"),
    ("AuditLog", "timestamp"),
    # Provenance/version queries
    ("Service", "versionId"),
    # ── Observability / SRE agents ───────────────────────────────────────────
    ("Incident", "startedAt"),
    ("Incident", "serviceName"),
    ("Alert", "timestamp"),
    ("Runbook", "origin"),
    ("Runbook", "status"),
]

# Provenance lookups — "everything this run wrote", "everything from Git", "what has
# gone stale". Applied to the labels that carry real volume rather than to all 60
# constrained labels: an index costs write throughput on every ingestion, and the
# long tail holds tens of nodes each, where a scan is already faster than an index.
#
# Cross-label questions ("how much of the whole graph is unattributed") stay full
# scans by nature — no per-label index can serve them — which is why the coverage
# figure is computed for a summary endpoint and not on the hot path.
_PROVENANCE_INDEXED_LABELS = [
    "Service", "Repository", "Application", "API", "Module", "Class", "Function",
    "CodeFile", "Dependency", "Infrastructure", "Container", "CloudResource",
    "Project", "Incident", "Runbook", "TestCase",
]
NODE_INDEXES += [
    (label, prop)
    for label in _PROVENANCE_INDEXED_LABELS
    for prop in ("lastSeenRunId", "pipeline", "lastSeenAt")
]

# Property-on-edge indexes. Memgraph cannot express these and returns None, in
# which case the statement is skipped rather than approximated.
EDGE_INDEXES: list[tuple[str, str]] = [
    ("DEPENDS_ON", "confidence"),
]


def _build_indexes_ddl() -> list[str]:
    d = dialect()
    ddl = [d.node_index_ddl(label, prop) for label, prop in NODE_INDEXES]
    for rel, prop in EDGE_INDEXES:
        stmt = d.edge_index_ddl(rel, prop)
        if stmt:
            ddl.append(stmt)
    return ddl

# Full-text indexes back runbook matching and case retrieval. Deliberately not a
# vector store: no embedding library exists in requirements.txt, and FTS scoring is
# deterministic, explainable, and directly scoreable by the eval harness.
FULLTEXT_DDL = [
    "CREATE FULLTEXT INDEX runbook_fts IF NOT EXISTS "
    "FOR (n:Runbook) ON EACH [n.title, n.bodySnippet, n.tags, n.services]",
    "CREATE FULLTEXT INDEX incident_fts IF NOT EXISTS "
    "FOR (n:Incident) ON EACH [n.title, n.rootCauseStatement, n.errorSignatures, n.serviceName]",
]


def ensure_schema():
    if not is_available():
        log.info("Graph schema bootstrap skipped — no backend available")
        return
    d = dialect()
    constraints = _build_constraints_ddl()
    indexes = _build_indexes_ddl()
    # Full-text indexes are Neo4j-only. Skipping them on an engine without the
    # feature is not a degradation: search_runbooks takes its portable retrieval
    # path when dialect.supports_fulltext is False.
    fulltext = FULLTEXT_DDL if d.supports_fulltext else []
    with session() as s:
        for ddl in constraints + indexes + fulltext:
            try:
                s.run(ddl)
            except Exception:
                pass
    log.info("Graph schema ready on %s (%d constraints, %d indexes, %d fulltext)",
             d.name, len(constraints), len(indexes), len(fulltext))


# ── Generic MERGE upsert ──────────────────────────────────────────────────────

def upsert_node(label: str, external_id: str, props: dict[str, Any]) -> dict:
    """MERGE on externalId — creates or updates.  Never deletes.

    Provenance (who/when/which source/which run) is merged in from the ambient
    trace context, so every one of the ~79 call sites is attributed without being
    edited. See `graph/provenance.py`.
    """
    props_clean = {k: v for k, v in props.items() if v is not None}
    props_clean = prov_ctx.stamp(props_clean)
    first = prov_ctx.first_seen_props()
    # createdAt belongs with the other origin facts: SET n += $props runs on every
    # write, so leaving it there re-dated the node on each ingestion.
    first.setdefault("createdAt", props_clean["updatedAt"])
    if "createdAt" in props_clean:
        first["createdAt"] = props_clean.pop("createdAt")

    cypher = f"""
    MERGE (n:{label} {{externalId: $eid}})
    ON CREATE SET n += $first
    SET n += $props
    RETURN n
    """
    with session() as s:
        result = s.run(cypher, eid=external_id, props=props_clean, first=first)
        record = result.single()
        return dict(record["n"]) if record else {}


def upsert_node_returning_id(
    label: str, external_id: str, props: dict[str, Any],
) -> tuple[dict, dict, str, bool]:
    """MERGE on externalId, returning (before, after, elementId, created).

    `upsert_node` returns properties only, but the audit trail is keyed by
    elementId — `get_node_by_id` and the /nodes/{id}/changelog endpoint both match
    on it — so a caller that needs to record history cannot use that function.

    `before` and `created` come from the same transaction as the write, so a
    concurrent upsert cannot make this report a create as an update.
    """
    props_clean = {k: v for k, v in props.items() if v is not None}
    props_clean = prov_ctx.stamp(props_clean)
    first = prov_ctx.first_seen_props()
    first.setdefault("createdAt", props_clean["updatedAt"])
    if "createdAt" in props_clean:
        first["createdAt"] = props_clean.pop("createdAt")

    cypher = f"""
    OPTIONAL MATCH (existing:{label} {{externalId: $eid}})
    WITH existing IS NULL AS created,
         CASE WHEN existing IS NULL THEN {{}} ELSE properties(existing) END AS before
    MERGE (n:{label} {{externalId: $eid}})
    ON CREATE SET n += $first
    SET n += $props
    RETURN before, properties(n) AS after, elementId(n) AS id, created
    """
    with session() as s:
        record = s.run(cypher, eid=external_id, props=props_clean, first=first).single()
        if not record:
            return {}, {}, "", False
        return (dict(record["before"]), dict(record["after"]),
                record["id"], bool(record["created"]))


def upsert_node_with_version(
    label: str,
    external_id: str,
    props: dict[str, Any],
    version_id: str | None = None,
) -> dict:
    """MERGE node and stamp versionId + versionedAt when provided."""
    if version_id:
        props = {**props, "versionId": version_id, "versionedAt": datetime.now(timezone.utc).isoformat()}
    return upsert_node(label, external_id, props)


def upsert_relationship(
    from_label: str, from_eid: str,
    to_label: str, to_eid: str,
    rel_type: str,
    props: dict[str, Any] | None = None,
    provenance_props: dict[str, Any] | None = None,
    **legacy: Any,
) -> bool:
    """MERGE relationship — creates if absent, updates props if present.

    Optional provenance dict accepts: source, sourceRecordId, discoveredBy,
    confidence (float), evidence (list), factType (known|inferred|hypothesis).
    Run attribution is added from the ambient trace context on top of it.

    The parameter is `provenance_props` rather than `provenance` because this module
    now imports the `provenance` module; `**legacy` keeps the old keyword accepted so
    no caller breaks on the rename.
    """
    if "provenance" in legacy:
        provenance_props = legacy.pop("provenance")
    if legacy:
        raise TypeError(f"upsert_relationship got unexpected arguments: {sorted(legacy)}")
    props = {k: v for k, v in (props or {}).items() if v is not None}
    now = datetime.now(timezone.utc).isoformat()
    # The caller's provenance wins over the ambient context — only the caller knows
    # how it derived the edge (confidence, evidence, factType). The context fills in
    # the parts it cannot know: which run, which actor, which trigger.
    prov = {k: v for k, v in (provenance_props or {}).items() if v is not None}
    prov = {**prov_ctx.edge_provenance(), **prov}
    if "evidence" in prov and isinstance(prov["evidence"], list):
        import json as _json
        prov["evidence"] = _json.dumps(prov["evidence"])
    prov["lastSeen"] = now
    props.update(prov)
    props["active"] = True
    props = prov_ctx.stamp(props)

    first = prov_ctx.first_seen_props()
    first["firstSeen"] = prov.get("firstSeen", now)

    cypher = f"""
    MATCH (a:{from_label} {{externalId: $from_eid}})
    MATCH (b:{to_label} {{externalId: $to_eid}})
    MERGE (a)-[r:{rel_type}]->(b)
    ON CREATE SET r += $first
    SET r += $props
    RETURN r
    """
    with session() as s:
        result = s.run(cypher, from_eid=from_eid, to_eid=to_eid, props=props, first=first)
        return result.single() is not None


def link_nodes_by_eid(
    from_eid: str,
    to_eid: str,
    rel_type: str,
    props: dict[str, Any] | None = None,
    provenance_props: dict[str, Any] | None = None,
) -> bool:
    """MERGE a relationship between two nodes identified by externalId, without requiring labels.

    Carries the same provenance as `upsert_relationship`. It had none until now,
    which is why edges written by `repo_ingestion_service` and `mcp_client/ingest` —
    both of which use this rather than the labelled form — had no lineage at all.
    """
    p = {k: v for k, v in (props or {}).items() if v is not None}
    now = datetime.now(timezone.utc).isoformat()
    prov = {**prov_ctx.edge_provenance(),
            **{k: v for k, v in (provenance_props or {}).items() if v is not None}}
    if "evidence" in prov and isinstance(prov["evidence"], list):
        import json as _json
        prov["evidence"] = _json.dumps(prov["evidence"])
    prov["lastSeen"] = now
    p.update(prov)
    p["active"] = True
    p = prov_ctx.stamp(p)
    first = prov_ctx.first_seen_props()
    first["firstSeen"] = prov.get("firstSeen", now)
    cypher = f"""
    MATCH (a {{externalId: $from_eid}})
    MATCH (b {{externalId: $to_eid}})
    MERGE (a)-[r:{rel_type}]->(b)
    ON CREATE SET r += $first
    SET r += $props
    RETURN r
    """
    try:
        with session() as s:
            result = s.run(cypher, from_eid=from_eid, to_eid=to_eid, props=p, first=first)
            return result.single() is not None
    except Exception as exc:
        log.debug("link_nodes_by_eid %s→%s [%s]: %s", from_eid, to_eid, rel_type, exc)
        return False


def archive_relationship(rel_id: str) -> bool:
    """Soft-delete a relationship by internal Neo4j ID."""
    cypher = """
    MATCH ()-[r]->()
    WHERE elementId(r) = $rid
    SET r.active = false, r += $props
    RETURN r
    """
    props = prov_ctx.stamp({"archivedAt": datetime.now(timezone.utc).isoformat()})
    with session() as s:
        result = s.run(cypher, rid=rel_id, props=props)
        return result.single() is not None


def create_node(label: str, name: str, props: dict[str, Any], actor: str) -> dict:
    """Create a new node with a generated externalId.  Returns the new node dict."""
    import uuid as _uuid
    external_id = f"manual:{_uuid.uuid4()}"
    merged = {
        "name": name,
        "source": "manual",
        "status": "active",
        **{k: v for k, v in props.items() if v is not None},
    }
    node = upsert_node(label, external_id, merged)
    element_id = node.get("id", external_id)
    try:
        write_audit_log(actor, "CREATE_NODE", str(element_id), None, {"label": label, "name": name, **merged})
    except Exception:
        pass
    return {**node, "elementId": element_id, "externalId": external_id, "label": label}


def retire_node(label: str, external_id: str) -> bool:
    cypher = f"""
    MATCH (n:{label} {{externalId: $eid}})
    SET n.status = 'retired', n += $props
    RETURN n
    """
    props = prov_ctx.stamp({"retiredAt": datetime.now(timezone.utc).isoformat()})
    with session() as s:
        result = s.run(cypher, eid=external_id, props=props)
        return result.single() is not None


# ── Query helpers ─────────────────────────────────────────────────────────────

def _label_expr(labels, var: str) -> str:
    """Build a Neo4j 5 label expression (e.g. ``n:Service|API``) from a label list.

    Every entry is validated against the canonical ``schema.ALL_LABELS`` before it
    reaches Cypher, because callers pass values straight from query strings.
    Raises ValueError when nothing survives validation.
    """
    from src.ontology.schema import ALL_LABELS
    safe = [l for l in labels if l in ALL_LABELS]
    if not safe:
        raise ValueError(f"no recognised labels in {labels!r}")
    return f"{var}:" + "|".join(safe)


def _node_row(rec) -> dict:
    """Map a (id, labels, props) record to the force-graph node shape."""
    props = dict(rec["props"])
    labels = rec["labels"]
    node_type = labels[0] if labels else "Unknown"
    return {
        "id": rec["id"],
        "label": props.get("name", props.get("externalId", rec["id"])),
        "node_type": node_type,
        "source": props.get("source", "unknown"),
        "status": props.get("status", "active"),
        **{k: v for k, v in props.items() if k not in ("name",)},
    }


def _link_row(rec) -> dict:
    """Map a (id, source, target, rel_type, props) record to the link shape."""
    props = dict(rec["props"])
    # Rename provenance 'source' → 'prov_source' to avoid collision with
    # force-graph's own 'source' field (mutated to a node object in place).
    if "source" in props:
        props["prov_source"] = props.pop("source")
    return {
        "id": rec["id"],
        "source": rec["source"],
        "target": rec["target"],
        "type": rec["rel_type"],
        **props,
    }


def get_org_graph(
    type_filter: list[str] | None = None,
    source_filter: list[str] | None = None,
    limit: int = 5000,
) -> dict:
    """Return nodes + relationships for the full org ontology.

    Relationships are keyed off the returned node ids so that both endpoints are
    guaranteed present — a filtered graph never contains dangling links.
    """
    node_where: list[str] = []
    params: dict[str, Any] = {"limit": limit}

    if type_filter:
        node_where.append(f"({_label_expr(type_filter, 'n')})")
    if source_filter:
        node_where.append("n.source IN $sources")
        params["sources"] = source_filter

    node_clause = ("WHERE " + " AND ".join(node_where)) if node_where else ""

    node_cypher = f"""
    MATCH (n)
    {node_clause}
    RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props
    ORDER BY coalesce(n.updatedAt, n.createdAt, '') DESC
    LIMIT $limit
    """

    # coalesce(): a relationship with no explicit 'active' property is live.
    # `r.active <> false` would evaluate to NULL and silently drop the row.
    rel_cypher = """
    UNWIND $ids AS nid
    MATCH (a) WHERE elementId(a) = nid
    MATCH (a)-[r]->(b)
    WHERE elementId(b) IN $ids
      AND coalesce(r.active, true) = true
    RETURN DISTINCT elementId(r) AS id, elementId(a) AS source,
           elementId(b) AS target, type(r) AS rel_type, properties(r) AS props
    LIMIT $rel_limit
    """

    nodes, links = [], []
    with session() as s:
        for rec in s.run(node_cypher, **params):
            nodes.append(_node_row(rec))
        ids = [n["id"] for n in nodes]
        if ids:
            for rec in s.run(rel_cypher, ids=ids, rel_limit=limit * 3):
                links.append(_link_row(rec))
    return {"nodes": nodes, "links": links}


def get_lens_graph(
    lens,
    *,
    limit: int = 5000,
    sources: list[str] | None = None,
    envs: list[str] | None = None,
    drop_orphans: bool = False,
) -> dict:
    """Return the subgraph projected by ``lens`` (a :class:`src.ontology.lenses.Lens`).

    Two statements, one session. The edge query is seeded from the node ids
    returned by the first, and each edge must match one of the lens's typed
    EdgeSpecs — so ``DEPENDS_ON`` scoped to ``Repository → Dependency`` does not
    drag in the ``Service → Service`` mesh.

    Nodes are ordered anchors-first so that a truncated lens still contains its
    topology skeleton; only leaves are dropped.
    """
    label_expr = _label_expr(list(lens.labels), "n")

    node_where = ["coalesce(n.status, 'active') <> 'retired'"]
    params: dict[str, Any] = {"limit": limit}
    if sources:
        node_where.append("n.source IN $sources")
        params["sources"] = sources
    if envs:
        node_where.append("n.environment IN $envs")
        params["envs"] = envs

    # Anchor labels sort first, so `limit` sheds leaves rather than the skeleton.
    anchor_expr = _label_expr(list(lens.anchor_labels), "n")
    anchor_case = f"CASE WHEN n:{anchor_expr.split(':', 1)[1]} THEN 0 ELSE 1 END"

    node_cypher = f"""
    MATCH ({label_expr})
    WHERE {' AND '.join(node_where)}
    RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props
    ORDER BY {anchor_case}, coalesce(n.updatedAt, n.createdAt, '') DESC
    LIMIT $limit
    """

    # One clause per EdgeSpec. Built from frozen server-side constants that were
    # validated against schema.ALL_LABELS at import, never from request input.
    clauses = []
    for e in lens.edges:
        frm = _label_expr(list(e.from_labels), "a")
        to = _label_expr(list(e.to_labels), "b")
        clauses.append(
            f"(type(r) = '{e.rel_type}' AND a:{frm.split(':', 1)[1]} "
            f"AND b:{to.split(':', 1)[1]})"
        )
    edge_predicate = "\n           OR ".join(clauses)

    rel_cypher = f"""
    UNWIND $ids AS nid
    MATCH (a) WHERE elementId(a) = nid
    MATCH (a)-[r]->(b)
    WHERE elementId(b) IN $ids
      AND coalesce(r.active, true) = true
      AND ({edge_predicate})
    RETURN DISTINCT elementId(r) AS id, elementId(a) AS source,
           elementId(b) AS target, type(r) AS rel_type, properties(r) AS props
    LIMIT $rel_limit
    """

    nodes, links = [], []
    with session() as s:
        for rec in s.run(node_cypher, **params):
            nodes.append(_node_row(rec))
        ids = [n["id"] for n in nodes]
        if ids:
            for rec in s.run(rel_cypher, ids=ids, rel_limit=limit * 3):
                links.append(_link_row(rec))

    # lensTier is stamped here rather than in _node_row: tiers are lens-local,
    # and this is what removes the frontend's duplicated NODE_TIER map.
    for n in nodes:
        n["lensTier"] = lens.tiers.get(n["node_type"], max(lens.tiers.values(), default=0))

    connected = {l["source"] for l in links} | {l["target"] for l in links}
    orphan_count = sum(1 for n in nodes if n["id"] not in connected)
    if drop_orphans and orphan_count:
        nodes = [n for n in nodes if n["id"] in connected]

    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "lensId": lens.id,
            "lensName": lens.name,
            "nodeCount": len(nodes),
            "linkCount": len(links),
            "orphanCount": orphan_count,
            "truncated": len(nodes) >= limit,
            "limit": limit,
            "labels": list(lens.labels),
            "tiers": dict(lens.tiers),
            "available": True,
        },
    }

def get_project_subgraph(project_name: str, hops: int = 1) -> dict:
    """
    Return a project-focused subgraph: the project node + all nodes directly
    connected to it (bidirectional, depth-1), plus every relationship that
    exists between those discovered nodes.  Depth-2 is intentionally skipped
    to avoid pulling in the entire graph via shared parent nodes (e.g. Aura Global).
    """
    # Used when APOC is available — still depth-1 + intra-set relationships
    cypher = """
    MATCH (root:Project)
    WHERE toLower(root.name) = toLower($name) OR root.externalId = $name
    CALL apoc.path.subgraphNodes(root, {maxLevel: 1}) YIELD node
    WITH collect(node) AS nodes
    UNWIND nodes AS n
    MATCH (n)-[r]-(m)
    WHERE m IN nodes
    RETURN collect(DISTINCT {
        id: elementId(n), label: n.name, node_type: labels(n)[0],
        source: n.source, status: n.status, externalId: n.externalId
    }) AS nodes,
    collect(DISTINCT {
        id: elementId(r), source: elementId(startNode(r)), target: elementId(endNode(r)),
        type: type(r)
    }) AS links
    """
    # Fallback (no APOC): bidirectional depth-1 + intra-set relationships
    fallback = """
    MATCH (root:Project)
    WHERE toLower(root.name) = toLower($name) OR root.externalId = $name
    // Step 1: collect depth-1 neighbours (bidirectional)
    OPTIONAL MATCH (root)-[r_root]-(n1)
    WITH root, collect(DISTINCT n1) AS neighbors
    // Step 2: the node set = root + depth-1 neighbors
    WITH [root] + neighbors AS node_set
    // Step 3: all relationships between nodes in the set
    UNWIND node_set AS a
    MATCH (a)-[r]-(b)
    WHERE b IN node_set
    WITH node_set, collect(DISTINCT r) AS inner_rels
    RETURN
      [n IN node_set WHERE n IS NOT NULL | {
        id: elementId(n), label: n.name, node_type: labels(n)[0],
        source: n.source, status: n.status, externalId: n.externalId
      }] AS nodes,
      [r IN inner_rels WHERE r IS NOT NULL | {
        id: elementId(r), source: elementId(startNode(r)),
        target: elementId(endNode(r)), type: type(r)
      }] AS links
    """
    with session() as s:
        try:
            result = s.run(cypher, name=project_name, hops=hops).single()
        except Exception:
            result = s.run(fallback, name=project_name).single()
        if not result:
            return {"nodes": [], "links": []}
        return {"nodes": result["nodes"] or [], "links": result["links"] or []}


def get_node_subgraph(node_id: str, hops: int = 2) -> dict:
    """
    Return a node-focused subgraph: the specified node + all nodes within N hops
    (bidirectional), plus every relationship that exists between those discovered nodes.
    """
    # Used when APOC is available
    cypher = """
    MATCH (root)
    WHERE elementId(root) = $node_id
    CALL apoc.path.subgraphNodes(root, {maxLevel: $hops}) YIELD node
    WITH collect(node) AS nodes
    UNWIND nodes AS n
    MATCH (n)-[r]-(m)
    WHERE m IN nodes
    RETURN collect(DISTINCT {
        id: elementId(n), label: n.name, node_type: labels(n)[0],
        source: n.source, status: n.status, externalId: n.externalId,
        hostname: n.hostname, ip: n.ip, region: n.region,
        environment: n.environment, severity: n.severity
    }) AS nodes,
    collect(DISTINCT {
        id: elementId(r), source: elementId(startNode(r)), target: elementId(endNode(r)),
        type: type(r), confidence: r.confidence, active: r.active
    }) AS links
    """
    # Fallback (no APOC): manual breadth-first traversal up to N hops
    fallback = """
    MATCH (root)
    WHERE elementId(root) = $node_id
    // Collect neighbors at each hop level
    CALL {
        WITH root
        MATCH path = (root)-[*1..%s]-(n)
        RETURN collect(DISTINCT n) AS neighbors
    }
    WITH [root] + neighbors AS node_set
    // Get all relationships between nodes in the set
    UNWIND node_set AS a
    MATCH (a)-[r]-(b)
    WHERE b IN node_set
    WITH node_set, collect(DISTINCT r) AS inner_rels
    RETURN
      [n IN node_set WHERE n IS NOT NULL | {
        id: elementId(n), label: n.name, node_type: labels(n)[0],
        source: n.source, status: n.status, externalId: n.externalId,
        hostname: n.hostname, ip: n.ip, region: n.region,
        environment: n.environment, severity: n.severity
      }] AS nodes,
      [r IN inner_rels WHERE r IS NOT NULL | {
        id: elementId(r), source: elementId(startNode(r)),
        target: elementId(endNode(r)), type: type(r),
        confidence: r.confidence, active: r.active
      }] AS links
    """ % hops
    with session() as s:
        try:
            result = s.run(cypher, node_id=node_id, hops=hops).single()
        except Exception:
            result = s.run(fallback, node_id=node_id).single()
        if not result:
            return {"nodes": [], "links": []}
        return {"nodes": result["nodes"] or [], "links": result["links"] or []}


def search_nodes(query: str, node_type: str | None = None, limit: int = 20) -> list[dict]:
    label_clause = f":{node_type}" if node_type else ""
    cypher = f"""
    MATCH (n{label_clause})
    WHERE toLower(n.name) CONTAINS toLower($q)
       OR toLower(n.externalId) CONTAINS toLower($q)
       OR toLower(coalesce(n.hostname, '')) CONTAINS toLower($q)
    RETURN elementId(n) AS id, labels(n)[0] AS label_type, n.name AS name,
           n.externalId AS externalId, n.source AS source, n.status AS status
    LIMIT $limit
    """
    with session() as s:
        return [
            {
                "id": r["id"],
                "type": r["label_type"],
                "name": r["name"],
                "externalId": r["externalId"],
                "source": r["source"],
                "status": r["status"],
            }
            for r in s.run(cypher, q=query, limit=limit)
        ]


def get_node_by_id(node_id: str) -> dict | None:
    cypher = """
    MATCH (n) WHERE elementId(n) = $id
    RETURN labels(n) AS labels, properties(n) AS props, elementId(n) AS id
    """
    with session() as s:
        record = s.run(cypher, id=node_id).single()
        if not record:
            return None
        return {"id": record["id"], "labels": record["labels"], **dict(record["props"])}


def get_relationship_by_id(rel_id: str) -> dict | None:
    """One relationship with its endpoints, for the edge trace panel.

    `RelationshipDetailPanel` had no provenance at all, and could not have: the UI
    holds an engine element id and there was no endpoint that resolved one.
    """
    cypher = """
    MATCH (a)-[r]->(b) WHERE elementId(r) = $id
    RETURN elementId(r) AS id, type(r) AS relType, properties(r) AS props,
           elementId(a) AS sourceId, a.externalId AS sourceEid,
           a.name AS sourceName, labels(a)[0] AS sourceLabel,
           elementId(b) AS targetId, b.externalId AS targetEid,
           b.name AS targetName, labels(b)[0] AS targetLabel
    """
    with session() as s:
        record = s.run(cypher, id=rel_id).single()
        if not record:
            return None
        return {
            "id": record["id"], "type": record["relType"],
            "source": {"id": record["sourceId"], "externalId": record["sourceEid"],
                       "name": record["sourceName"], "label": record["sourceLabel"]},
            "target": {"id": record["targetId"], "externalId": record["targetEid"],
                       "name": record["targetName"], "label": record["targetLabel"]},
            **dict(record["props"]),
        }


def provenance_summary() -> dict:
    """Per-pipeline counts and freshness, plus attribution coverage.

    A full scan by nature: "how much of the graph is unattributed" cannot be
    answered from any per-label index, and the honest response to that is to run it
    on a summary endpoint rather than to pretend an index would help.
    """
    node_cypher = """
    MATCH (n)
    RETURN coalesce(n.pipeline, 'unattributed') AS pipeline,
           coalesce(n.attribution, 'pre-trace') AS attribution,
           count(*) AS count,
           max(coalesce(n.lastSeenAt, n.updatedAt)) AS lastSeen
    ORDER BY count DESC
    """
    rel_cypher = """
    MATCH ()-[r]->()
    RETURN coalesce(r.pipeline, 'unattributed') AS pipeline, count(*) AS count
    """
    pipelines: dict[str, dict] = {}
    coverage = {"traced": 0, "partial": 0, "unattributed": 0}
    try:
        with session() as s:
            for row in s.run(node_cypher):
                name = row["pipeline"]
                entry = pipelines.setdefault(
                    name, {"pipeline": name, "nodes": 0, "edges": 0, "lastSeen": ""})
                entry["nodes"] += row["count"]
                if (row["lastSeen"] or "") > entry["lastSeen"]:
                    entry["lastSeen"] = row["lastSeen"] or ""
                attribution = row["attribution"]
                if attribution == "traced":
                    coverage["traced"] += row["count"]
                elif attribution == "pre-trace":
                    coverage["partial"] += row["count"]
                else:
                    coverage["unattributed"] += row["count"]
            for row in s.run(rel_cypher):
                entry = pipelines.setdefault(
                    row["pipeline"],
                    {"pipeline": row["pipeline"], "nodes": 0, "edges": 0, "lastSeen": ""})
                entry["edges"] += row["count"]
    except Exception as exc:  # noqa: BLE001 — a summary must not 500 the page
        log.warning("provenance_summary failed: %s", exc)
        return {"pipelines": [], "coverage": coverage, "available": False}

    total = sum(coverage.values())
    return {
        "pipelines": sorted(pipelines.values(), key=lambda p: -p["nodes"]),
        "coverage": {**coverage, "total": total,
                     "tracedPct": round(100 * coverage["traced"] / total, 1) if total else 0.0},
        "available": True,
    }


def entities_written_by_run(run_id: str, limit: int = 200) -> list[dict]:
    """Everything a run touched, straight from the graph.

    Read from `lastSeenRunId` rather than from the changelog because the changelog
    is diff-only: a run that re-confirmed 1,200 nodes without changing them wrote no
    rows, and the Run Inspector should still be able to show what it covered.
    """
    cypher = """
    MATCH (n) WHERE n.lastSeenRunId = $rid
    RETURN elementId(n) AS id, labels(n)[0] AS label, n.name AS name,
           n.externalId AS externalId, n.firstSeenRunId AS firstSeenRunId
    LIMIT $limit
    """
    try:
        with session() as s:
            return [{
                "id": r["id"], "label": r["label"],
                "name": r["name"] or r["externalId"] or r["id"],
                "externalId": r["externalId"],
                # A node whose first run is this one was created here; anything else
                # it touched, it updated or re-confirmed.
                "change": "new" if r["firstSeenRunId"] == run_id else "updated",
            } for r in s.run(cypher, rid=run_id, limit=limit)]
    except Exception as exc:  # noqa: BLE001
        log.warning("entities_written_by_run failed: %s", exc)
        return []


def update_node_property(node_id: str, prop: str, value: Any) -> bool:
    cypher = f"""
    MATCH (n) WHERE elementId(n) = $id
    SET n.`{prop}` = $val, n += $props
    RETURN n
    """
    # A manual edit is a write like any other, and must show up in the node's trace
    # with the person who made it — not only in the changelog.
    with session() as s:
        result = s.run(cypher, id=node_id, val=value, props=prov_ctx.stamp({}))
        return result.single() is not None


def write_audit_log(actor: str, action: str, target_id: str, before: Any, after: Any) -> str:
    """Create an AuditLog node linked to the target node."""
    ts = datetime.now(timezone.utc).isoformat()
    import uuid as _uuid
    audit_id = str(_uuid.uuid4())
    cypher = """
    CREATE (a:AuditLog {
        auditId: $aid, actor: $actor, action: $action,
        targetId: $tid, before: $before, after: $after, timestamp: $ts
    })
    WITH a
    MATCH (n) WHERE elementId(n) = $tid
    CREATE (n)-[:AUDITED_BY]->(a)
    RETURN a.auditId
    """
    import json as _json
    with session() as s:
        s.run(
            cypher,
            aid=audit_id, actor=actor, action=action,
            tid=target_id,
            before=_json.dumps(before) if not isinstance(before, str) else before,
            after=_json.dumps(after) if not isinstance(after, str) else after,
            ts=ts,
        )
    return audit_id


def get_audit_log(page: int = 0, page_size: int = 50) -> list[dict]:
    """Audit records, newest first.

    Filtered on `auditId`, which only `write_audit_log` sets. A bare
    `MATCH (a:AuditLog)` also returned INGESTED nodes: "AuditLog" is in
    `ontology/schema.ALL_LABELS`, so an MCP tool called `list_audit_logs` is
    classified into that label and written by `upsert_node` — with no `action`,
    `actor` or `auditId`. Those rows reached the maintainer's Audit Trail tab and
    crashed it on `entry.action.startsWith`.

    Filtering rather than tolerating is the right fix twice over: a customer's own
    audit data is not a record of what Aura changed, so showing it in this trail
    would be wrong even if it rendered.
    """
    skip = page * page_size
    cypher = """
    MATCH (a:AuditLog)
    WHERE a.auditId IS NOT NULL
    RETURN a ORDER BY a.timestamp DESC
    SKIP $skip LIMIT $limit
    """
    with session() as s:
        return [dict(r["a"]) for r in s.run(cypher, skip=skip, limit=page_size)]


def get_stats() -> dict:
    if not is_available():
        return {"totalNodes": 0, "totalRelationships": 0, "byType": {}, "isAvailable": False}

    try:
        count_cypher = "MATCH (n) RETURN count(n) AS total"
        rel_cypher = "MATCH ()-[r]->() RETURN count(r) AS total"
        label_cypher = (
            "MATCH (n) "
            "UNWIND labels(n) AS lbl "
            "RETURN lbl, count(*) AS cnt "
            "ORDER BY cnt DESC"
        )
        with session() as s:
            node_total = s.run(count_cypher).single()["total"]
            rel_total = s.run(rel_cypher).single()["total"]
            by_type: dict[str, int] = {
                r["lbl"]: r["cnt"] for r in s.run(label_cypher)
            }
        return {
            "totalNodes": node_total,
            "totalRelationships": rel_total,
            "byType": by_type,
            "isAvailable": True,
        }
    except Exception as exc:
        log.warning("get_stats failed: %s", exc)
        return {"totalNodes": 0, "totalRelationships": 0, "byType": {}, "isAvailable": False}


def run_query(cypher: str, params: dict | None = None) -> list[dict]:
    """Execute an arbitrary read Cypher query and return all rows as dicts."""
    with session() as s:
        return [dict(r) for r in s.run(cypher, **(params or {}))]


def count_nodes() -> int:
    """Return the total number of nodes in the graph."""
    if not is_available():
        return 0
    with session() as s:
        result = s.run("MATCH (n) RETURN count(n) AS total").single()
        return result["total"] if result else 0


def count_orphan_nodes() -> int:
    """Return the count of nodes with no outgoing or incoming relationships."""
    if not is_available():
        return -1
    with session() as s:
        result = s.run(
            "MATCH (n) WHERE NOT (n)--() RETURN count(n) AS total"
        ).single()
        return result["total"] if result else 0


# ── Observability: full-text retrieval ───────────────────────────────────────

def _fts_escape(text: str) -> str:
    """Neutralise Lucene operators so operator text can't blow up the query."""
    import re as _re
    cleaned = _re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', " ", text or "")
    return " ".join(cleaned.split())


def _search_terms(*parts: str) -> list[str]:
    """Distinct lowercase terms, for the portable retrieval path."""
    seen: list[str] = []
    for part in parts:
        for token in _fts_escape(part).lower().split():
            if len(token) > 2 and token not in seen:
                seen.append(token)
    return seen


def _portable_text_search(
    label: str, fields: list[str], terms: list[str], limit: int,
    extra_return: str = "", extra_where: str = "", params: dict | None = None,
) -> list[dict]:
    """Candidate retrieval for engines without a full-text index.

    This is not a reimplementation of Lucene and does not need to be.
    search_runbooks is only the *retrieval* half — `observability/runbooks.py::
    score_runbooks` does the ranking and normalises `fts_score` against the result
    set (runbooks.py:47,72). So a term-overlap count in that field preserves the
    caller's weighting exactly; only the candidate ordering within a tie changes.
    """
    if not terms:
        return []
    # One CONTAINS per (field, term). Built as a parameterised OR rather than
    # interpolated text so a runbook title can never inject Cypher.
    conditions, values = [], {}
    for t_idx, term in enumerate(terms):
        key = f"t{t_idx}"
        values[key] = term
        for field in fields:
            conditions.append(f"toLower(coalesce(n.{field}, '')) CONTAINS ${key}")
    score_expr = " + ".join(
        f"(CASE WHEN toLower(coalesce(n.{field}, '')) CONTAINS ${k} THEN 1 ELSE 0 END)"
        for k in values for field in fields
    )
    cypher = f"""
    MATCH (n:{label})
    WHERE ({' OR '.join(conditions)}){extra_where}
    WITH n, ({score_expr}) AS fts_score
    {extra_return}
    ORDER BY fts_score DESC, n.externalId ASC
    LIMIT $limit
    """
    try:
        return run_query(cypher, {**values, **(params or {}), "limit": limit})
    except Exception as exc:  # noqa: BLE001
        log.warning("portable text search on %s failed: %s", label, exc)
        return []


def search_runbooks(
    service: str = "",
    alert_signature: str = "",
    labels: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text search over Runbook nodes, returning nodes + a raw FTS score.

    Relevance *ranking* is deliberately done by the caller
    (src/observability/runbooks.py::score_runbooks) so the weighting is testable
    without a live database. Swapping FTS for embeddings later happens behind this
    signature — nothing above needs to know.
    """
    if not is_available():
        return []
    terms = " ".join(filter(None, [
        _fts_escape(service), _fts_escape(alert_signature),
        _fts_escape(" ".join(labels or [])),
    ])).strip()
    if not terms:
        terms = "*"

    if not dialect().supports_fulltext:
        rows = _portable_text_search(
            "Runbook", ["title", "bodySnippet", "tags", "services"],
            _search_terms(service, alert_signature, " ".join(labels or [])),
            limit,
            extra_return=("OPTIONAL MATCH (n)-[:DOCUMENTS]->(svc) "
                          "WITH n, fts_score, collect(svc.name) AS documented_services "
                          "RETURN n, fts_score, documented_services"),
        )
        return [{**dict(r["n"]),
                 "fts_score": float(r.get("fts_score") or 0.0),
                 "documented_services": r.get("documented_services") or []}
                for r in rows]

    cypher = """
    CALL db.index.fulltext.queryNodes('runbook_fts', $terms)
    YIELD node, score
    RETURN node AS n, score AS fts_score,
           [(node)-[:DOCUMENTS]->(s) | s.name] AS documented_services
    ORDER BY score DESC LIMIT $limit
    """
    try:
        rows = run_query(cypher, {"terms": terms, "limit": limit})
        return [{**dict(r["n"]),
                 "fts_score": r["fts_score"],
                 "documented_services": r.get("documented_services") or []}
                for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("search_runbooks failed: %s", exc)
        return []


def search_incidents(
    service: str = "",
    signatures: list[str] | None = None,
    limit: int = 20,
    exclude_investigation_id: str = "",
) -> list[dict]:
    """Full-text search over past Incident nodes, for case-based retrieval."""
    if not is_available():
        return []
    terms = " ".join(filter(None, [
        _fts_escape(service), _fts_escape(" ".join(signatures or [])),
    ])).strip()
    if not terms:
        return []

    if not dialect().supports_fulltext:
        rows = _portable_text_search(
            "Incident",
            ["title", "rootCauseStatement", "errorSignatures", "serviceName"],
            _search_terms(service, " ".join(signatures or [])),
            limit,
            extra_where=" AND n.externalId <> $exclude",
            extra_return=("OPTIONAL MATCH (n)-[:HAS_OUTCOME]->(o:IncidentOutcome) "
                          "WITH n, fts_score, o AS outcome "
                          "RETURN n, fts_score, outcome"),
            params={"exclude": exclude_investigation_id or "__none__"},
        )
        out = []
        for r in rows:
            node = dict(r["n"])
            node["fts_score"] = float(r.get("fts_score") or 0.0)
            node["outcome"] = dict(r["outcome"]) if r.get("outcome") else None
            out.append(node)
        return out

    cypher = """
    CALL db.index.fulltext.queryNodes('incident_fts', $terms)
    YIELD node, score
    WHERE node.externalId <> $exclude
    OPTIONAL MATCH (node)-[:HAS_OUTCOME]->(o:IncidentOutcome)
    RETURN node AS n, score AS fts_score, o AS outcome
    ORDER BY score DESC LIMIT $limit
    """
    try:
        rows = run_query(cypher, {
            "terms": terms, "limit": limit,
            "exclude": exclude_investigation_id or "__none__",
        })
        out = []
        for r in rows:
            node = dict(r["n"])
            node["fts_score"] = r["fts_score"]
            node["outcome"] = dict(r["outcome"]) if r.get("outcome") else None
            out.append(node)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("search_incidents failed: %s", exc)
        return []


def service_neighbours(service_name: str, hops: int = 2) -> dict:
    """Upstream/downstream services within N hops — the blast radius query."""
    if not is_available() or not service_name:
        return {"upstream": [], "downstream": [], "hops": hops, "source": "neo4j"}
    cypher = f"""
    MATCH (s:Service {{name: $name}})
    OPTIONAL MATCH (s)-[:DEPENDS_ON|CALLS|CONNECTS_TO*1..{int(hops)}]->(d)
    OPTIONAL MATCH (u)-[:DEPENDS_ON|CALLS|CONNECTS_TO*1..{int(hops)}]->(s)
    RETURN collect(DISTINCT d.name) AS downstream,
           collect(DISTINCT u.name) AS upstream
    """
    try:
        rows = run_query(cypher, {"name": service_name})
        if not rows:
            return {"upstream": [], "downstream": [], "hops": hops, "source": "neo4j"}
        row = rows[0]
        return {
            "upstream": [x for x in (row.get("upstream") or []) if x],
            "downstream": [x for x in (row.get("downstream") or []) if x],
            "hops": hops,
            "source": "neo4j",
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("service_neighbours failed: %s", exc)
        return {"upstream": [], "downstream": [], "hops": hops, "source": "neo4j"}


def list_service_names(limit: int = 500) -> list[str]:
    """All known Service node names — the authoritative service vocabulary."""
    if not is_available():
        return []
    try:
        rows = run_query(
            "MATCH (s:Service) WHERE s.name IS NOT NULL "
            "RETURN DISTINCT s.name AS name ORDER BY name LIMIT $limit",
            {"limit": limit},
        )
        return [r["name"] for r in rows if r.get("name")]
    except Exception as exc:  # noqa: BLE001
        log.warning("list_service_names failed: %s", exc)
        return []

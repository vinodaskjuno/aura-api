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
    """The backend reads and writes go to. None when no engine is configured."""
    from src.graph import backends
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
    backend = active_backend()
    if backend is None:
        raise RuntimeError("No graph backend is configured")
    with backend.session() as s:
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


INDEXES_DDL = [
    "CREATE INDEX node_name_svc IF NOT EXISTS FOR (n:Service) ON (n.name)",
    "CREATE INDEX node_name_app IF NOT EXISTS FOR (n:Application) ON (n.name)",
    "CREATE INDEX node_name_repo IF NOT EXISTS FOR (n:Repository) ON (n.name)",
    "CREATE INDEX node_hostname IF NOT EXISTS FOR (n:Infrastructure) ON (n.hostname)",
    "CREATE INDEX node_ip IF NOT EXISTS FOR (n:Infrastructure) ON (n.ip)",
    "CREATE INDEX audit_ts IF NOT EXISTS FOR (n:AuditLog) ON (n.timestamp)",
    # Provenance/version queries
    "CREATE INDEX node_version IF NOT EXISTS FOR (n:Service) ON (n.versionId)",
    "CREATE INDEX rel_confidence IF NOT EXISTS FOR ()-[r:DEPENDS_ON]-() ON (r.confidence)",
    # ── Observability / SRE agents ───────────────────────────────────────────
    "CREATE INDEX incident_started IF NOT EXISTS FOR (n:Incident) ON (n.startedAt)",
    "CREATE INDEX incident_service IF NOT EXISTS FOR (n:Incident) ON (n.serviceName)",
    "CREATE INDEX alert_ts IF NOT EXISTS FOR (n:Alert) ON (n.timestamp)",
    "CREATE INDEX runbook_origin IF NOT EXISTS FOR (n:Runbook) ON (n.origin)",
    "CREATE INDEX runbook_status IF NOT EXISTS FOR (n:Runbook) ON (n.status)",
]

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
    # Full-text indexes are Neo4j-only. Skipping them on an engine without the
    # feature is not a degradation: search_runbooks takes its portable retrieval
    # path when dialect.supports_fulltext is False.
    fulltext = FULLTEXT_DDL if d.supports_fulltext else []
    with session() as s:
        for ddl in constraints + INDEXES_DDL + fulltext:
            try:
                s.run(ddl)
            except Exception:
                pass
    log.info("Graph schema ready on %s (%d constraints, %d indexes, %d fulltext)",
             d.name, len(constraints), len(INDEXES_DDL), len(fulltext))


# ── Generic MERGE upsert ──────────────────────────────────────────────────────

def upsert_node(label: str, external_id: str, props: dict[str, Any]) -> dict:
    """MERGE on externalId — creates or updates.  Never deletes."""
    props_clean = {k: v for k, v in props.items() if v is not None}
    props_clean["updatedAt"] = datetime.now(timezone.utc).isoformat()
    if "createdAt" not in props_clean:
        props_clean["createdAt"] = props_clean["updatedAt"]

    cypher = f"""
    MERGE (n:{label} {{externalId: $eid}})
    SET n += $props
    RETURN n
    """
    with session() as s:
        result = s.run(cypher, eid=external_id, props=props_clean)
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
    props_clean["updatedAt"] = datetime.now(timezone.utc).isoformat()
    if "createdAt" not in props_clean:
        props_clean["createdAt"] = props_clean["updatedAt"]

    cypher = f"""
    OPTIONAL MATCH (existing:{label} {{externalId: $eid}})
    WITH existing IS NULL AS created,
         CASE WHEN existing IS NULL THEN {{}} ELSE properties(existing) END AS before
    MERGE (n:{label} {{externalId: $eid}})
    SET n += $props
    RETURN before, properties(n) AS after, elementId(n) AS id, created
    """
    with session() as s:
        record = s.run(cypher, eid=external_id, props=props_clean).single()
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
    provenance: dict[str, Any] | None = None,
) -> bool:
    """MERGE relationship — creates if absent, updates props if present.

    Optional provenance dict accepts: source, sourceRecordId, discoveredBy,
    confidence (float), evidence (list), factType (known|inferred|hypothesis).
    """
    props = {k: v for k, v in (props or {}).items() if v is not None}
    if provenance:
        now = datetime.now(timezone.utc).isoformat()
        prov = {k: v for k, v in provenance.items() if v is not None}
        if "evidence" in prov and isinstance(prov["evidence"], list):
            import json as _json
            prov["evidence"] = _json.dumps(prov["evidence"])
        if "firstSeen" not in prov:
            prov["firstSeen"] = now
        prov["lastSeen"] = now
        props.update(prov)
    props["active"] = True
    props["updatedAt"] = datetime.now(timezone.utc).isoformat()

    cypher = f"""
    MATCH (a:{from_label} {{externalId: $from_eid}})
    MATCH (b:{to_label} {{externalId: $to_eid}})
    MERGE (a)-[r:{rel_type}]->(b)
    SET r += $props
    RETURN r
    """
    with session() as s:
        result = s.run(cypher, from_eid=from_eid, to_eid=to_eid, props=props)
        return result.single() is not None


def link_nodes_by_eid(
    from_eid: str,
    to_eid: str,
    rel_type: str,
    props: dict[str, Any] | None = None,
) -> bool:
    """MERGE a relationship between two nodes identified by externalId, without requiring labels."""
    p = {k: v for k, v in (props or {}).items() if v is not None}
    p["active"] = True
    p["updatedAt"] = datetime.now(timezone.utc).isoformat()
    cypher = f"""
    MATCH (a {{externalId: $from_eid}})
    MATCH (b {{externalId: $to_eid}})
    MERGE (a)-[r:{rel_type}]->(b)
    SET r += $props
    RETURN r
    """
    try:
        with session() as s:
            result = s.run(cypher, from_eid=from_eid, to_eid=to_eid, props=p)
            return result.single() is not None
    except Exception as exc:
        log.debug("link_nodes_by_eid %s→%s [%s]: %s", from_eid, to_eid, rel_type, exc)
        return False


def archive_relationship(rel_id: str) -> bool:
    """Soft-delete a relationship by internal Neo4j ID."""
    cypher = """
    MATCH ()-[r]->()
    WHERE elementId(r) = $rid
    SET r.active = false, r.archivedAt = $ts
    RETURN r
    """
    with session() as s:
        result = s.run(cypher, rid=rel_id, ts=datetime.now(timezone.utc).isoformat())
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
    SET n.status = 'retired', n.retiredAt = $ts
    RETURN n
    """
    with session() as s:
        result = s.run(cypher, eid=external_id, ts=datetime.now(timezone.utc).isoformat())
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


def update_node_property(node_id: str, prop: str, value: Any) -> bool:
    cypher = f"""
    MATCH (n) WHERE elementId(n) = $id
    SET n.`{prop}` = $val, n.updatedAt = $ts
    RETURN n
    """
    with session() as s:
        result = s.run(cypher, id=node_id, val=value, ts=datetime.now(timezone.utc).isoformat())
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
    skip = page * page_size
    cypher = """
    MATCH (a:AuditLog)
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

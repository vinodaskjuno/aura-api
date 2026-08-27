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

_driver = None
_driver_attempted = False   # distinguishes "not yet tried" from "tried and failed"


def _get_driver():
    global _driver, _driver_attempted
    if _driver is not None:
        return _driver
    if _driver_attempted:
        return None
    _driver_attempted = True
    try:
        from neo4j import GraphDatabase
        from src.config_settings import get_settings
        s = get_settings()
        if not s.neo4j_enabled:
            return None
        _driver = GraphDatabase.driver(
            s.neo4j_uri,
            auth=(s.neo4j_user, s.neo4j_password),
        )
        _driver.verify_connectivity()
        log.info("Neo4j connected at %s", s.neo4j_uri)
    except Exception as exc:
        log.warning("Neo4j unavailable: %s", exc)
        _driver = None
    return _driver


def is_available() -> bool:
    return _get_driver() is not None


@contextmanager
def session() -> Generator:
    driver = _get_driver()
    if driver is None:
        raise RuntimeError("Neo4j is not available")
    from src.config_settings import get_settings
    db = get_settings().neo4j_database
    with driver.session(database=db) as s:
        yield s


def close():
    global _driver, _driver_attempted
    if _driver:
        _driver.close()
        _driver = None
    _driver_attempted = False


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
    ddl = []
    for label in _CONSTRAINED_LABELS:
        slug = label.lower().replace(" ", "_")
        ddl.append(
            f"CREATE CONSTRAINT {slug}_eid IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.externalId IS UNIQUE"
        )
    return ddl


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
]


def ensure_schema():
    if not is_available():
        log.info("Neo4j schema bootstrap skipped — not available")
        return
    constraints = _build_constraints_ddl()
    with session() as s:
        for ddl in constraints + INDEXES_DDL:
            try:
                s.run(ddl)
            except Exception:
                pass
    log.info("Neo4j schema ready (%d constraints, %d indexes)", len(constraints), len(INDEXES_DDL))


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
        logger.debug("link_nodes_by_eid %s→%s [%s]: %s", from_eid, to_eid, rel_type, exc)
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

def get_org_graph(
    type_filter: list[str] | None = None,
    source_filter: list[str] | None = None,
    limit: int = 5000,
) -> dict:
    """Return nodes + relationships for the full org ontology."""
    where_clauses = []
    if type_filter:
        labels_str = "|".join(f"n:{t}" for t in type_filter)
        where_clauses.append(f"({labels_str})")
    if source_filter:
        where_clauses.append("n.source IN $sources")

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    node_cypher = f"""
    MATCH (n)
    {where}
    RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props
    LIMIT {limit}
    """
    rel_cypher = f"""
    MATCH (a)-[r]->(b)
    {where.replace('n.', 'a.')}
    WHERE r.active <> false
    RETURN elementId(r) AS id, elementId(a) AS source, elementId(b) AS target,
           type(r) AS rel_type, properties(r) AS props
    LIMIT {limit * 3}
    """
    nodes, links = [], []
    with session() as s:
        for rec in s.run(node_cypher, sources=source_filter or []):
            props = dict(rec["props"])
            labels = rec["labels"]
            node_type = labels[0] if labels else "Unknown"
            nodes.append({
                "id": rec["id"],
                "label": props.get("name", props.get("externalId", rec["id"])),
                "node_type": node_type,
                "source": props.get("source", "unknown"),
                "status": props.get("status", "active"),
                **{k: v for k, v in props.items()
                   if k not in ("name",)},
            })
        for rec in s.run(rel_cypher, sources=source_filter or []):
            props = dict(rec["props"])
            # Rename provenance 'source' → 'prov_source' to avoid collision
            # with force-graph's own 'source' field (mutated to node object)
            if "source" in props:
                props["prov_source"] = props.pop("source")
            links.append({
                "id": rec["id"],
                "source": rec["source"],
                "target": rec["target"],
                "type": rec["rel_type"],
                **props,
            })
    return {"nodes": nodes, "links": links}


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

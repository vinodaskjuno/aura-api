"""DynamoDB helper — thin wrapper around boto3 resource.

Table names are prefixed by settings.dynamodb_table_prefix (default "aura-").
Tables used across the platform:
  aura-users               PK: userId
  aura-roles               PK: roleId
  aura-sessions            PK: sessionId   SK: userId
  aura-projects            PK: projectId   SK: userId
  aura-connectors          PK: connectorId SK: projectId
  aura-test-results        PK: testRunId   SK: projectId
  aura-logs                PK: logId       SK: timestamp
  aura-uploads             PK: uploadId    SK: userId
  aura-activity            PK: userId      SK: timestamp
  aura-agents              PK: agentRunId  SK: orchestratorId
  aura-ontology-changelog  PK: changeId    GSI1: entityId/timestamp  GSI2: actor/timestamp
  aura-token-usage         PK: userId      SK: sortKey (timestamp#messageId)
                           GSI: projectId-timestamp-index (projectId, timestamp)
  aura-user-connectors     PK: userId      SK: connectorId
  aura-budget-config         PK: orgId       (singleton: orgId="global")
  aura-user-budgets          PK: userId
  aura-gateway-providers     PK: providerId
  aura-gateway-provider-health  PK: providerId  SK: checkedAt
  aura-gateway-budgets       PK: pk (entityType#entityId)
  aura-usage-daily           PK: pk (userId | "__org__")
                             SK: sk (date#tool#model[#userId])
"""
import logging
from decimal import Decimal
from functools import lru_cache
from typing import Any
import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
from src.config_settings import get_settings

logger = logging.getLogger(__name__)

_resource = None


def _get_resource():
    global _resource
    if _resource is None:
        s = get_settings()
        kwargs: dict = {"region_name": s.dynamodb_region}
        if s.dynamodb_endpoint_url:
            kwargs["endpoint_url"] = s.dynamodb_endpoint_url
        if s.aws_access_key_id:
            kwargs["aws_access_key_id"] = s.aws_access_key_id
            kwargs["aws_secret_access_key"] = s.aws_secret_access_key
        _resource = boto3.resource("dynamodb", **kwargs)
    return _resource


def _table(name: str):
    s = get_settings()
    full_name = f"{s.dynamodb_table_prefix}{name}"
    return _get_resource().Table(full_name)


# ── Table key schemas (PK only vs PK+SK) ─────────────────────────────────────
# Tables with a single PK only — get_item needs only one key field
_PK_ONLY_TABLES = {"users", "roles", "uploads", "ontology-changelog", "budget-config", "user-budgets"}

# Tables with composite keys — get_item needs both PK and SK
_COMPOSITE_TABLES = {
    "sessions":        ("sessionId",    "userId"),
    "projects":        ("projectId",    "userId"),
    "connectors":      ("connectorId",  "projectId"),
    "test-results":    ("testRunId",    "projectId"),
    "logs":            ("logId",        "timestamp"),
    "activity":        ("userId",       "timestamp"),
    "agents":          ("agentRunId",   "orchestratorId"),
    "chat-sessions":   ("sessionId",    "userId"),
    "token-usage":     ("userId",       "sortKey"),       # sortKey = timestamp#messageId
    "usage-daily":     ("pk",           "sk"),            # pk = userId|__org__, sk = date#tool#model
    "user-connectors": ("userId",       "connectorId"),
    "services":        ("projectId",    "serviceId"),
    # Observability / SRE agents
    "observability-investigations": ("investigationId", "createdAt"),
    "observability-evidence":       ("investigationId", "evidenceId"),
    "observability-outcomes":       ("investigationId", "recordedAt"),
    "observability-cases":          ("caseId",          "createdAt"),
    "observability-traces":         ("runId",           "seq"),
    "notification-log":             ("dedupeKey",       "sentAt"),
}


# ── GSI key hygiene ──────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def _gsi_key_attrs(table_name: str) -> frozenset[str]:
    """Attribute names used as a key by any GSI on this table."""
    for schema in TABLE_SCHEMAS:
        if schema["name"] != table_name:
            continue
        names: set[str] = set()
        for gsi in schema.get("gsis", []):
            if gsi.get("pk"):
                names.add(gsi["pk"])
            if gsi.get("sk"):
                names.add(gsi["sk"])
        return frozenset(names)
    return frozenset()


def _decimalize(value: Any) -> Any:
    """Recursively convert floats to Decimal.

    DynamoDB rejects Python floats outright, and boto3 raises deep inside its
    serializer with a message that names no field — so a single nested float (a
    confidence score inside a findings list, say) fails the whole write with a
    traceback that points at boto3 rather than at the offending data.
    """
    if isinstance(value, bool):
        return value                      # bool before int/float — bools are fine
    if isinstance(value, float):
        # str() first: Decimal(float) carries binary float noise into storage.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimalize(v) for v in value]
    return value


def _drop_empty_gsi_keys(table_name: str, item: dict) -> dict:
    """Remove GSI key attributes whose value is an empty string.

    DynamoDB rejects an empty string for ANY index key attribute, so a row that
    legitimately has no project or no service would fail the whole write. Omitting
    the attribute instead makes the index sparse: the row is stored and simply does
    not appear in that index — which is the correct semantics, because querying
    "investigations for project ''" is not a meaningful question.
    """
    keys = _gsi_key_attrs(table_name)
    if not keys:
        return item
    dropped = [k for k in keys if item.get(k) == ""]
    if not dropped:
        return item
    logger.debug("Omitting empty GSI key(s) %s from %s row", dropped, table_name)
    return {k: v for k, v in item.items() if k not in dropped}


# ── CRUD helpers ─────────────────────────────────────────────────────────────

def put_item(table_name: str, item: dict) -> None:
    try:
        _table(table_name).put_item(
            Item=_decimalize(_drop_empty_gsi_keys(table_name, item)))
    except ClientError as e:
        logger.error("DynamoDB put_item failed [%s]: %s", table_name, e)
        raise


def put_item_if_absent(table_name: str, item: dict, key_fields: tuple[str, ...]) -> bool:
    """Put an item only if no item with the same key exists.

    Returns True if this write created the item, False if it already existed.
    This is the dedup gate for at-least-once delivery: OTLP exporters retry, and
    ADD-based rollup counters are not idempotent, so callers bump aggregates only
    when this returns True.
    """
    cond = " AND ".join(f"attribute_not_exists({f})" for f in key_fields)
    try:
        _table(table_name).put_item(Item=item, ConditionExpression=cond)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        logger.error("DynamoDB put_item_if_absent failed [%s]: %s", table_name, e)
        raise


def get_item(table_name: str, key: dict) -> dict | None:
    """Get a single item by its full key. Pass all key fields."""
    try:
        resp = _table(table_name).get_item(Key=key)
        return resp.get("Item")
    except ClientError as e:
        logger.error("DynamoDB get_item failed [%s]: %s", table_name, e)
        return None


def get_item_by_pk(table_name: str, pk_value: str) -> dict | None:
    """Get a single item by PK only — works for both single-key and composite-key tables.
    For composite tables, scans for first match by PK field name."""
    schema = _COMPOSITE_TABLES.get(table_name)
    if schema:
        pk_name = schema[0]
        items = query_items(table_name, pk_name, pk_value)
        return items[0] if items else None
    # Single PK table — figure out PK name from known tables
    pk_names = {"users": "userId", "roles": "roleId", "uploads": "uploadId"}
    pk_name = pk_names.get(table_name, list(_COMPOSITE_TABLES.get(table_name, ("id",)))[0])
    try:
        resp = _table(table_name).get_item(Key={pk_name: pk_value})
        return resp.get("Item")
    except ClientError as e:
        logger.error("DynamoDB get_item_by_pk failed [%s]: %s", table_name, e)
        return None


def delete_item(table_name: str, key: dict) -> None:
    try:
        _table(table_name).delete_item(Key=key)
    except ClientError as e:
        logger.error("DynamoDB delete_item failed [%s]: %s", table_name, e)
        raise


def update_item(table_name: str, key: dict, updates: dict) -> dict | None:
    """Update individual fields. `updates` is a flat dict of field→value."""
    # Setting a GSI key to "" is rejected exactly as it is on put; skip those.
    updates = {k: _decimalize(v) for k, v in updates.items()
               if not (v == "" and k in _gsi_key_attrs(table_name))}
    if not updates:
        return None
    expr_parts = []
    expr_names: dict = {}
    expr_values: dict = {}
    for i, (k, v) in enumerate(updates.items()):
        placeholder = f"#f{i}"
        value_key = f":v{i}"
        expr_parts.append(f"{placeholder} = {value_key}")
        expr_names[placeholder] = k
        expr_values[value_key] = v
    update_expr = "SET " + ", ".join(expr_parts)
    try:
        resp = _table(table_name).update_item(
            Key=key,
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ReturnValues="ALL_NEW",
        )
        return resp.get("Attributes")
    except ClientError as e:
        logger.error("DynamoDB update_item failed [%s]: %s", table_name, e)
        raise


def _to_number(value: Any) -> Decimal:
    """Coerce to Decimal — DynamoDB rejects Python floats outright."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    # str() first: Decimal(float) carries binary float noise into the stored value.
    return Decimal(str(value))


def atomic_add(table_name: str, key: dict, field: str, delta: float) -> None:
    """Atomically increment a numeric field (ADD expression). Creates field if absent."""
    atomic_add_many(table_name, key, {field: delta})


def atomic_add_many(table_name: str, key: dict, deltas: dict[str, Any],
                    set_fields: dict | None = None) -> None:
    """Atomically increment several numeric fields in ONE request.

    ADD creates absent fields, so this doubles as an upsert for counter rows.
    `set_fields` writes non-numeric metadata (labels, timestamps) in the same
    call via SET — cheaper and race-free versus a separate update_item.
    """
    if not deltas and not set_fields:
        return

    expr_names: dict = {}
    expr_values: dict = {}
    add_parts: list[str] = []
    set_parts: list[str] = []

    for i, (field, delta) in enumerate(deltas.items()):
        expr_names[f"#a{i}"] = field
        expr_values[f":a{i}"] = _to_number(delta)
        add_parts.append(f"#a{i} :a{i}")

    for i, (field, value) in enumerate((set_fields or {}).items()):
        expr_names[f"#s{i}"] = field
        expr_values[f":s{i}"] = value
        set_parts.append(f"#s{i} = :s{i}")

    expr = ""
    if set_parts:
        expr += "SET " + ", ".join(set_parts)
    if add_parts:
        expr += (" " if expr else "") + "ADD " + ", ".join(add_parts)

    try:
        _table(table_name).update_item(
            Key=key,
            UpdateExpression=expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        logger.error("DynamoDB atomic_add_many failed [%s]: %s", table_name, e)
        raise


def query_items(table_name: str, pk_name: str, pk_value: Any,
                sk_name: str = None, sk_value: Any = None,
                index_name: str = None, limit: int = 100) -> list[dict]:
    kwargs: dict = {
        "KeyConditionExpression": Key(pk_name).eq(pk_value),
        "Limit": limit,
    }
    if sk_name and sk_value is not None:
        kwargs["KeyConditionExpression"] &= Key(sk_name).eq(sk_value)
    if index_name:
        kwargs["IndexName"] = index_name
    try:
        resp = _table(table_name).query(**kwargs)
        return resp.get("Items", [])
    except ClientError as e:
        logger.error("DynamoDB query failed [%s]: %s", table_name, e)
        return []


def query_range(table_name: str, pk_name: str, pk_value: Any,
                sk_name: str, sk_prefix: str = None,
                sk_from: str = None, sk_to: str = None,
                index_name: str = None, limit: int = 1000) -> list[dict]:
    """Query one partition with a sort-key prefix or range, following pagination.

    Preferred over scan_items + Python-side filtering for time-windowed reads:
    the filtering happens in DynamoDB, so results are neither truncated nor
    charged for rows that are thrown away.
    """
    cond = Key(pk_name).eq(pk_value)
    if sk_prefix:
        cond &= Key(sk_name).begins_with(sk_prefix)
    elif sk_from and sk_to:
        cond &= Key(sk_name).between(sk_from, sk_to)
    elif sk_from:
        cond &= Key(sk_name).gte(sk_from)
    elif sk_to:
        cond &= Key(sk_name).lte(sk_to)

    kwargs: dict = {"KeyConditionExpression": cond}
    if index_name:
        kwargs["IndexName"] = index_name

    items: list[dict] = []
    try:
        table = _table(table_name)
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key or len(items) >= limit:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items[:limit]
    except ClientError as e:
        logger.error("DynamoDB query_range failed [%s]: %s", table_name, e)
        return []


def scan_items(table_name: str, filter_expr=None, limit: int = 500) -> list[dict]:
    kwargs: dict = {}
    if filter_expr is not None:
        kwargs["FilterExpression"] = filter_expr
    try:
        items: list[dict] = []
        table = _table(table_name)
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key or len(items) >= limit:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return items[:limit]
    except ClientError as e:
        logger.error("DynamoDB scan failed [%s]: %s", table_name, e)
        return []


# ── Table bootstrap (creates tables if they don't exist — for local dev) ─────

TABLE_SCHEMAS = [
    {"name": "users",         "pk": "userId",         "sk": None},
    {"name": "roles",         "pk": "roleId",         "sk": None},
    {"name": "sessions",      "pk": "sessionId",      "sk": "userId"},
    {"name": "projects",      "pk": "projectId",      "sk": "userId"},
    {"name": "connectors",    "pk": "connectorId",    "sk": "projectId"},
    {"name": "test-results",  "pk": "testRunId",      "sk": "projectId"},
    {"name": "logs",          "pk": "logId",          "sk": "timestamp"},
    {"name": "uploads",       "pk": "uploadId",       "sk": "userId"},
    {"name": "activity",      "pk": "userId",         "sk": "timestamp"},
    {"name": "sops",          "pk": "sopId",          "sk": "projectId"},
    {"name": "agents",        "pk": "agentRunId",     "sk": "orchestratorId"},
    {"name": "chat-sessions",   "pk": "sessionId",      "sk": "userId"},
    {
        "name": "token-usage",
        "pk": "userId",
        "sk": "sortKey",
        "gsis": [
            {"index": "projectId-timestamp-index", "pk": "projectId", "sk": "timestamp"},
        ],
    },
    {"name": "user-connectors", "pk": "userId",         "sk": "connectorId"},
    # Daily usage rollups — pk is a userId or the literal "__org__" aggregate row,
    # sk is "<date>#<tool>#<model>" (plus "#<userId>" on org rows). Counters are
    # incremented with atomic_add_many so dashboards never scan raw events.
    {"name": "usage-daily",     "pk": "pk",             "sk": "sk"},
    {"name": "budget-config",   "pk": "orgId",          "sk": None},
    {"name": "user-budgets",    "pk": "userId",         "sk": None},
    {
        "name": "ontology-changelog",
        "pk": "changeId",
        "sk": None,
        "gsis": [
            {"index": "entityId-timestamp-index", "pk": "entityId", "sk": "timestamp"},
            {"index": "actor-timestamp-index",    "pk": "actor",    "sk": "timestamp"},
        ],
    },
    {"name": "ontology-versions", "pk": "versionId", "sk": None},
    {"name": "scheduler-state",   "pk": "jobId",     "sk": None},
    {"name": "services",          "pk": "projectId", "sk": "serviceId"},
    # AURA AI Gateway tables
    {
        "name": "gateway-api-keys",
        "pk": "keyId",
        "sk": None,
        "gsis": [
            {"index": "userId-index", "pk": "userId"},
        ],
    },
    {"name": "gateway-audit-log",      "pk": "requestId",  "sk": "timestamp"},
    {"name": "gateway-providers",      "pk": "providerId", "sk": None},
    {"name": "gateway-provider-health","pk": "providerId", "sk": "checkedAt"},
    {"name": "gateway-budgets",        "pk": "pk",         "sk": None},
    # ── Observability / SRE agents ───────────────────────────────────────────
    # Evidence payloads live in S3 (a real incident is megabytes; DynamoDB items
    # cap at 400KB). These tables hold indexes and records the SPA pages through.
    {
        "name": "observability-investigations",
        "pk": "investigationId",
        "sk": "createdAt",
        "gsis": [
            {"index": "projectId-createdAt-index",   "pk": "projectId",   "sk": "createdAt"},
            {"index": "serviceName-createdAt-index", "pk": "serviceName", "sk": "createdAt"},
        ],
    },
    {"name": "observability-evidence", "pk": "investigationId", "sk": "evidenceId"},
    {
        "name": "observability-outcomes",
        "pk": "investigationId",
        "sk": "recordedAt",
        "gsis": [
            {"index": "verdict-recordedAt-index", "pk": "verdict", "sk": "recordedAt"},
        ],
    },
    {
        "name": "observability-cases",
        "pk": "caseId",
        "sk": "createdAt",
        "gsis": [
            {"index": "serviceName-createdAt-index", "pk": "serviceName", "sk": "createdAt"},
        ],
    },
    {"name": "observability-traces",   "pk": "runId",     "sk": "seq"},
    {"name": "notification-log",       "pk": "dedupeKey", "sk": "sentAt"},
]


def ensure_tables() -> None:
    """Create DynamoDB tables if they don't exist. Idempotent — safe to call on startup."""
    s = get_settings()
    client = _get_resource().meta.client
    for schema in TABLE_SCHEMAS:
        full_name = f"{s.dynamodb_table_prefix}{schema['name']}"
        key_schema = [{"AttributeName": schema["pk"], "KeyType": "HASH"}]
        attr_defs = [{"AttributeName": schema["pk"], "AttributeType": "S"}]
        if schema.get("sk"):
            key_schema.append({"AttributeName": schema["sk"], "KeyType": "RANGE"})
            attr_defs.append({"AttributeName": schema["sk"], "AttributeType": "S"})

        gsis = schema.get("gsis", [])
        gsi_definitions = []
        for gsi in gsis:
            gsi_key = [{"AttributeName": gsi["pk"], "KeyType": "HASH"}]
            if gsi.get("sk"):
                gsi_key.append({"AttributeName": gsi["sk"], "KeyType": "RANGE"})
                if not any(a["AttributeName"] == gsi["sk"] for a in attr_defs):
                    attr_defs.append({"AttributeName": gsi["sk"], "AttributeType": "S"})
            if not any(a["AttributeName"] == gsi["pk"] for a in attr_defs):
                attr_defs.append({"AttributeName": gsi["pk"], "AttributeType": "S"})
            gsi_definitions.append({
                "IndexName": gsi["index"],
                "KeySchema": gsi_key,
                "Projection": {"ProjectionType": "ALL"},
            })

        create_kwargs: dict = {
            "TableName": full_name,
            "KeySchema": key_schema,
            "AttributeDefinitions": attr_defs,
            "BillingMode": "PAY_PER_REQUEST",
        }
        if gsi_definitions:
            create_kwargs["GlobalSecondaryIndexes"] = gsi_definitions

        try:
            client.create_table(**create_kwargs)
            logger.info("Created DynamoDB table: %s", full_name)
            # Wait until ACTIVE — new tables are in CREATING state on real AWS
            client.get_waiter("table_exists").wait(
                TableName=full_name,
                WaiterConfig={"Delay": 1, "MaxAttempts": 30},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                pass  # already exists
            else:
                logger.error("Failed to create table %s: %s", full_name, e)


# ── Ontology Changelog helpers ────────────────────────────────────────────────

def build_changelog_entry(
    entity_id: str,
    entity_type: str,
    entity_label: str,
    entity_name: str,
    change_type: str,
    actor: str,
    before: Any,
    after: Any,
    session_id: str = "api",
    source: str = "api",
    notes: str = "",
    external_id: str | None = None,
) -> dict:
    """Build one changelog row.

    Lives here rather than in routers/ontology_universe.py so services can write
    history without importing from a router, and so every writer shares one schema.

    `entityId` is the **externalId** when the caller knows one, falling back to the
    engine's own node id otherwise.

    It used to be the Neo4j elementId. That does not survive a database rebuild,
    and — now that a deployment may run Neo4j or Memgraph — it is a different value
    on each engine, so history written against one store would not resolve against
    the other. externalId is deterministic and identical everywhere. The engine id
    is still recorded in `elementId` so a row can be traced back to the node it was
    written against.
    """
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz
    return {
        "changeId": str(_uuid.uuid4()),
        "timestamp": _dt.now(_tz.utc).isoformat(),
        # Portable identity first; the engine id only when there is nothing better.
        "entityId": external_id or entity_id,
        "elementId": entity_id,
        "entityType": entity_type,
        "entityLabel": entity_label,
        "entityName": entity_name,
        "changeType": change_type,
        "actor": actor,
        "before": _json.dumps(before) if before is not None else None,
        "after": _json.dumps(after) if after is not None else None,
        "sessionId": session_id,
        "source": source,
        "notes": notes,
        "externalId": external_id,
    }


def write_changelog(change: dict) -> None:
    """Write a single versioning entry to the ontology changelog table."""
    try:
        put_item("ontology-changelog", change)
    except Exception as exc:
        logger.error("write_changelog failed: %s", exc)


def get_entity_changelog(entity_id: str, limit: int = 20) -> list[dict]:
    """Return changelog entries for a specific Neo4j entity, newest first."""
    items = query_items(
        "ontology-changelog",
        pk_name="entityId",
        pk_value=entity_id,
        index_name="entityId-timestamp-index",
        limit=limit,
    )
    return sorted(items, key=lambda x: x.get("timestamp", ""), reverse=True)


def get_actor_changelog(actor: str, limit: int = 50) -> list[dict]:
    """Return changelog entries for a specific actor (username), newest first."""
    items = query_items(
        "ontology-changelog",
        pk_name="actor",
        pk_value=actor,
        index_name="actor-timestamp-index",
        limit=limit,
    )
    return sorted(items, key=lambda x: x.get("timestamp", ""), reverse=True)


def get_recent_changelog(limit: int = 50) -> list[dict]:
    """Return the most recent changelog entries across all entities (scan + client-side sort)."""
    items = scan_items("ontology-changelog", limit=limit * 2)
    return sorted(items, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]

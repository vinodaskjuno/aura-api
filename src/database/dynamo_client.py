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
"""
import logging
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
    "user-connectors": ("userId",       "connectorId"),
    "services":        ("projectId",    "serviceId"),
}


# ── CRUD helpers ─────────────────────────────────────────────────────────────

def put_item(table_name: str, item: dict) -> None:
    try:
        _table(table_name).put_item(Item=item)
    except ClientError as e:
        logger.error("DynamoDB put_item failed [%s]: %s", table_name, e)
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


def atomic_add(table_name: str, key: dict, field: str, delta: float) -> None:
    """Atomically increment a numeric field (ADD expression). Creates field if absent."""
    try:
        _table(table_name).update_item(
            Key=key,
            UpdateExpression="ADD #f :d",
            ExpressionAttributeNames={"#f": field},
            ExpressionAttributeValues={":d": delta},
        )
    except ClientError as e:
        logger.error("DynamoDB atomic_add failed [%s]: %s", table_name, e)
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

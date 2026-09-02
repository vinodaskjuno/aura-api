"""Connectors router — user-scoped connector registry backed by DynamoDB."""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.database import dynamo_client as db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/connectors", tags=["connectors"])

CONNECTOR_TYPES = {"git", "project_mgmt", "itsm", "security", "storage", "sql", "mcp", "api",
                   "observability", "incident", "notification"}
PROVIDER_MAP = {
    "observability": ["grafana_loki", "grafana_mimir", "grafana_tempo", "datadog",
                      "sentry", "elasticsearch", "kubernetes", "cloudwatch"],
    "incident":      ["pagerduty"],
    "notification":  ["slack", "telegram"],
    "git":          ["github", "gitlab", "bitbucket", "azure_devops"],
    "project_mgmt": ["jira", "rally", "azure_boards"],
    "itsm":         ["servicenow"],
    "security":     ["wiz", "snyk"],
    "storage":      ["s3", "azure_blob", "gcs"],
    "sql":          ["postgresql", "mysql", "mssql"],
    "mcp":          ["custom"],
    "api":          ["rest"],
}


REPO_TYPES = {
    "auto", "mule", "spring", "python", "ui-react", "ui-angular",
    "terraform", "cicd", "config", "library", "unknown",
}


class ConnectorCreate(BaseModel):
    name: str
    type: str
    provider: str = ""
    config: dict[str, Any] = {}
    projectId: str = ""
    serviceId: str = ""
    repoType: str = ""   # mule|spring|python|ui-react|ui-angular|terraform|cicd|config|library|auto
    repoUrl: str = ""    # top-level convenience field (also stored in config.repoUrl)


class ConnectorUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    provider: str | None = None
    config: dict[str, Any] | None = None
    projectId: str | None = None
    serviceId: str | None = None
    repoType: str | None = None
    repoUrl: str | None = None


# Substring match, case- and separator-insensitive. The previous exact-match list
# covered only camelCase keys, while every connector under connectors/sources/ uses
# snake_case (api_token, secret_access_key, app_key) — so those secrets were being
# returned to the client in full.
_SECRET_HINTS = (
    "token", "password", "secret", "apikey", "appkey", "clientsecret",
    "credentials", "authtoken", "bottoken", "dsn", "accesskey", "privatekey",
    "passphrase", "webhookurl", "cacert", "sessionkey",
)


def _is_secret(key: str) -> bool:
    normalized = key.replace("_", "").replace("-", "").lower()
    return any(hint in normalized for hint in _SECRET_HINTS)


def _safe_config(config: dict) -> dict:
    """Mask sensitive fields for API responses."""
    masked = dict(config)
    for key, value in list(masked.items()):
        if not value or not _is_secret(key):
            continue
        text = str(value)
        masked[key] = (text[:4] + "****") if len(text) > 4 else "****"
    return masked


@router.get("")
@router.get("/")
def list_connectors(user: dict = Depends(get_current_user)):
    """List all connectors for the current user."""
    user_id = user.get("userId", "")
    try:
        items = db.query_items("user-connectors", "userId", user_id, limit=200)
        return [dict(i, config=_safe_config(i.get("config", {}))) for i in sorted(items, key=lambda x: x.get("createdAt", ""), reverse=True)]
    except Exception as e:
        log.error("Failed to list connectors: %s", e)
        return []


def _invalidate_mcp(user_id: str) -> None:
    """Drop this user's cached MCP tool discovery.

    Without it, a server added or re-pointed here stays invisible to chat for up to the
    cache TTL — which reads as "the connector does not work" to whoever just added it.
    Import is local and guarded so the connectors API keeps working even where the
    optional `mcp` package is absent.
    """
    try:
        from src.mcp_client import invalidate
        invalidate(user_id)
    except Exception as exc:                              # noqa: BLE001
        log.debug("MCP cache invalidate skipped: %s", exc)


@router.post("", status_code=201)
@router.post("/", status_code=201)
def create_connector(body: ConnectorCreate, user: dict = Depends(get_current_user)):
    """Create a new connector for the current user."""
    user_id = user.get("userId", "")
    if body.type not in CONNECTOR_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown type '{body.type}'. Valid: {sorted(CONNECTOR_TYPES)}")
    connector_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    # Merge top-level repoUrl into config for convenience
    config = dict(body.config)
    if body.repoUrl and not config.get("repoUrl"):
        config["repoUrl"] = body.repoUrl
    item = {
        "userId": user_id,
        "connectorId": connector_id,
        "name": body.name,
        "type": body.type,
        "provider": body.provider,
        "config": config,
        "projectId": body.projectId,
        "serviceId": body.serviceId,
        "repoType": body.repoType,
        "repoUrl": body.repoUrl,
        "createdAt": now,
        "updatedAt": now,
        "testStatus": "untested",
        "lastTested": None,
    }
    if body.type == "mcp":
        # Allocated once, at registration, and stored. Tool names are built from this
        # rather than from connectorId, which is a uuid and therefore both too long and
        # illegal (hyphens) for model tool names — see src/mcp_client/naming.py.
        from src.mcp_client.naming import slug_for
        item["mcpSlug"] = slug_for(body.name, connector_id)

    db.put_item("user-connectors", item)
    if body.type == "mcp":
        _invalidate_mcp(user_id)
    return dict(item, config=_safe_config(item["config"]))


@router.get("/{connector_id}")
def get_connector(connector_id: str, user: dict = Depends(get_current_user)):
    user_id = user.get("userId", "")
    item = db.get_item("user-connectors", {"userId": user_id, "connectorId": connector_id})
    if not item:
        raise HTTPException(status_code=404, detail=f"Connector {connector_id!r} not found")
    return dict(item, config=_safe_config(item.get("config", {})))


@router.put("/{connector_id}")
def update_connector(connector_id: str, body: ConnectorUpdate, user: dict = Depends(get_current_user)):
    user_id = user.get("userId", "")
    item = db.get_item("user-connectors", {"userId": user_id, "connectorId": connector_id})
    if not item:
        raise HTTPException(status_code=404, detail=f"Connector {connector_id!r} not found")
    updates: dict[str, Any] = {"updatedAt": datetime.now(timezone.utc).isoformat()}
    if body.name is not None:
        updates["name"] = body.name
    if body.type is not None:
        updates["type"] = body.type
    if body.provider is not None:
        updates["provider"] = body.provider
    if body.config is not None:
        # Merge config — preserve existing sensitive fields if not re-supplied
        merged = dict(item.get("config", {}))
        merged.update(body.config)
        updates["config"] = merged
    updated = db.update_item("user-connectors", {"userId": user_id, "connectorId": connector_id}, updates)
    if item.get("type") == "mcp":
        _invalidate_mcp(user_id)
    if updated:
        return dict(updated, config=_safe_config(updated.get("config", {})))
    return {**item, **updates, "config": _safe_config({**item.get("config", {}), **updates.get("config", {})})}


@router.delete("/{connector_id}", status_code=204)
def delete_connector(connector_id: str, user: dict = Depends(get_current_user)):
    user_id = user.get("userId", "")
    item = db.get_item("user-connectors", {"userId": user_id, "connectorId": connector_id})
    if not item:
        raise HTTPException(status_code=404, detail=f"Connector {connector_id!r} not found")
    db.delete_item("user-connectors", {"userId": user_id, "connectorId": connector_id})
    if item.get("type") == "mcp":
        _invalidate_mcp(user_id)


@router.post("/{connector_id}/test")
def test_connector(connector_id: str, user: dict = Depends(get_current_user)):
    """Test a connector's connection."""
    user_id = user.get("userId", "")
    item = db.get_item("user-connectors", {"userId": user_id, "connectorId": connector_id})
    if not item:
        raise HTTPException(status_code=404, detail=f"Connector {connector_id!r} not found")

    config = item.get("config", {})
    conn_type = item.get("type", "")
    provider = item.get("provider", "")
    t0 = time.time()
    success = False
    message = ""

    try:
        if conn_type == "git":
            import subprocess
            url = config.get("baseUrl") or config.get("repoUrl", "")
            token = config.get("token", "")
            if url and token:
                auth_url = url.replace("https://", f"https://x-token:{token}@")
                result = subprocess.run(["git", "ls-remote", "--exit-code", auth_url], capture_output=True, timeout=10)
                success = result.returncode == 0
                message = "Connected" if success else "Authentication failed"
            else:
                message = "Missing URL or token"
        elif conn_type == "storage" and provider == "s3":
            import boto3
            s3 = boto3.client("s3", region_name=config.get("region", "us-east-1"))
            s3.head_bucket(Bucket=config.get("bucket", ""))
            success = True
            message = "S3 bucket accessible"
        elif conn_type == "mcp":
            # Was falling through to the generic branch below, which reads
            # baseUrl/instanceUrl — while everything else that touches MCP reads
            # config["endpoint"]. So this ALWAYS returned "No URL configured", however
            # the connector was filled in.
            #
            # And a liveness ping was never the right test anyway: it proves a web
            # server exists, not that it speaks MCP. This does a real initialize +
            # tools/list and reports the tool count, which is what the operator
            # actually needs to know.
            from src.mcp_client import health_check
            success, message = health_check(item)

        else:
            # Generic HTTP ping for API / ITSM / project_mgmt connectors
            import urllib.request
            base_url = config.get("baseUrl") or config.get("instanceUrl", "")
            if base_url:
                urllib.request.urlopen(base_url, timeout=8)
                success = True
                message = "Endpoint reachable"
            else:
                message = "No URL configured"
    except Exception as e:
        success = False
        message = str(e)[:100]

    latency_ms = round((time.time() - t0) * 1000)
    now = datetime.now(timezone.utc).isoformat()
    status = "connected" if success else "failed"
    db.update_item("user-connectors", {"userId": user_id, "connectorId": connector_id},
                   {"testStatus": status, "lastTested": now})
    if conn_type == "mcp":
        _invalidate_mcp(user_id)
    return {"success": success, "message": message, "latencyMs": latency_ms}


@router.get("/providers/list")
def list_providers(_: dict = Depends(get_current_user)):
    """Return available connector types and their providers."""
    return {"types": CONNECTOR_TYPES, "providers": PROVIDER_MAP}

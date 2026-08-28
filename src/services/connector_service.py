import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.connectors.base import AbstractConnector
from src.connectors.api_connector import ApiConnector
from src.connectors.sql_connector import SqlConnector
from src.connectors.cloud_storage_connector import CloudStorageConnector
from src.connectors.mcp_connector import McpConnector
from src.connectors.sources.pagerduty import PagerDutyConnector
from src.connectors.sources.kubernetes import KubernetesConnector
from src.connectors.sources.confluence import ConfluenceConnector

REGISTRY_PATH = Path(__file__).parent.parent / "connectors_registry.json"

_TYPE_MAP: dict[str, type[AbstractConnector]] = {
    "api": ApiConnector,
    "sql": SqlConnector,
    "s3": CloudStorageConnector,
    "cloud": CloudStorageConnector,
    "mcp": McpConnector,
    # Previously written but never registered — their sync() bodies are now real.
    "pagerduty": PagerDutyConnector,
    "kubernetes": KubernetesConnector,
    "confluence": ConfluenceConnector,
}


def _load() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        with REGISTRY_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(connectors: list[dict]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRY_PATH.open("w", encoding="utf-8") as fh:
        json.dump(connectors, fh, indent=2, default=str)


def list_connectors() -> list[dict]:
    return _load()


def get_connector(connector_id: str) -> dict | None:
    for c in _load():
        if c.get("id") == connector_id:
            return c
    return None


def create_connector(data: dict) -> dict:
    connectors = _load()
    now = datetime.now(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": data.get("name", ""),
        "type": data.get("type", ""),
        "config": data.get("config", {}),
        "status": "idle",
        "created_at": now,
        "updated_at": now,
        "last_synced": None,
    }
    connectors.append(entry)
    _save(connectors)
    return entry


def update_connector(connector_id: str, data: dict) -> dict | None:
    connectors = _load()
    for i, c in enumerate(connectors):
        if c.get("id") == connector_id:
            for key in ("name", "type", "config", "status", "last_synced"):
                if key in data:
                    connectors[i][key] = data[key]
            connectors[i]["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save(connectors)
            return connectors[i]
    return None


def delete_connector(connector_id: str) -> bool:
    connectors = _load()
    filtered = [c for c in connectors if c.get("id") != connector_id]
    if len(filtered) == len(connectors):
        return False
    _save(filtered)
    return True


def get_connector_instance(connector_id: str) -> AbstractConnector:
    entry = get_connector(connector_id)
    if entry is None:
        raise ValueError(f"Connector {connector_id!r} not found")
    connector_type = entry.get("type", "").lower().replace(" ", "_")
    cls = _TYPE_MAP.get(connector_type)
    if cls is None:
        raise ValueError(f"Unknown connector type: {entry.get('type')!r}. Supported: {list(_TYPE_MAP)}")
    return cls(entry.get("config", {}))

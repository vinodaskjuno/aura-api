"""Group → permission mapping, editable at runtime.

Stored in DynamoDB rather than Settings for the same reason the graph read-source is:
get_settings() is lru_cached, so anything read from there needs a restart to change.
Adding an AD group must not require a deploy.

Deliberately independent of *where* the groups came from. It takes a list of group
names and returns permissions, so an OIDC provider could feed it later without the
menus, the routes or any endpoint noticing.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TABLE = "auth-config"
CONFIG_ID = "ldap"
_TTL_SECONDS = 10.0

_cache: tuple[float, "AuthConfig"] | None = None
_lock = threading.Lock()


@dataclass
class Mapping:
    group: str
    role_id: str = ""
    permissions: list[str] = field(default_factory=list)
    priority: int = 0

    def resolved_permissions(self) -> list[str]:
        """A mapping names a role OR an explicit permission list.

        Both are supported so a group can be granted one extra screen without
        inventing a whole role for it.
        """
        from src.services.auth_service import ROLE_PERMISSIONS
        out = list(ROLE_PERMISSIONS.get(self.role_id, [])) if self.role_id else []
        for p in self.permissions:
            if p not in out:
                out.append(p)
        return out

    def as_item(self) -> dict:
        return {"group": self.group, "roleId": self.role_id,
                "permissions": self.permissions, "priority": self.priority}


@dataclass
class AuthConfig:
    enabled: bool = False
    mappings: list[Mapping] = field(default_factory=list)
    updated_at: str = ""
    updated_by: str = ""

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "mappings": [m.as_item() for m in self.mappings],
            "updatedAt": self.updated_at,
            "updatedBy": self.updated_by,
        }

    @property
    def group_names(self) -> list[str]:
        return [m.group for m in self.mappings]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_config(refresh: bool = False) -> AuthConfig:
    global _cache
    now = time.monotonic()
    if not refresh and _cache and (now - _cache[0]) < _TTL_SECONDS:
        return _cache[1]

    config = AuthConfig()
    try:
        from src.database import dynamo_client as db
        row = db.get_item(TABLE, {"configId": CONFIG_ID})
        if row:
            config = AuthConfig(
                enabled=bool(row.get("enabled", False)),
                mappings=[
                    Mapping(group=str(m.get("group", "")),
                            role_id=str(m.get("roleId", "")),
                            permissions=list(m.get("permissions") or []),
                            priority=int(m.get("priority") or 0))
                    for m in (row.get("mappings") or []) if m.get("group")
                ],
                updated_at=row.get("updatedAt", ""),
                updated_by=row.get("updatedBy", ""),
            )
    except Exception as exc:  # noqa: BLE001 — a missing table must not break login
        log.debug("auth config read failed, using defaults: %s", exc)

    with _lock:
        _cache = (now, config)
    return config


def set_config(enabled: bool, mappings: list[dict], actor: str) -> AuthConfig:
    global _cache
    from src.database import dynamo_client as db
    db.put_item(TABLE, {
        "configId": CONFIG_ID,
        "enabled": bool(enabled),
        "mappings": [
            {"group": str(m.get("group", "")).strip(),
             "roleId": str(m.get("roleId", "")).strip(),
             "permissions": list(m.get("permissions") or []),
             "priority": int(m.get("priority") or 0)}
            for m in mappings if str(m.get("group", "")).strip()
        ],
        "updatedAt": _now(),
        "updatedBy": actor,
    })
    with _lock:
        _cache = None
    return get_config(refresh=True)


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def resolve(groups: list[str], config: AuthConfig | None = None) -> dict:
    """Map a user's groups to permissions.

    A user in several mapped groups gets the UNION of their permissions — anything
    else would make membership order-dependent and surprising. `role` comes from the
    highest-priority match, because roleLabel and the one remaining isAdmin check
    still need a single role name.

    Matching is on the group NAME, case-insensitively, so an administrator types
    "AURA-Dev" rather than a full distinguished name.
    """
    config = config or get_config()
    held = {g.strip().lower() for g in (groups or []) if g and g.strip()}

    matched = [m for m in config.mappings if m.group.strip().lower() in held]
    if not matched:
        return {"matched": [], "permissions": [], "roleId": "", "priority": -1}

    permissions: list[str] = []
    for m in sorted(matched, key=lambda m: -m.priority):
        for p in m.resolved_permissions():
            if p not in permissions:
                permissions.append(p)

    top = max(matched, key=lambda m: m.priority)
    return {
        "matched": [m.group for m in sorted(matched, key=lambda m: -m.priority)],
        "permissions": permissions,
        "roleId": top.role_id or "ldap_user",
        "priority": top.priority,
    }

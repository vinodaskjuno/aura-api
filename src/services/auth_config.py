"""Directory group → organization role → permissions, editable at runtime.

Three tiers, and each answers a different question:

    AD group            who someone is, according to the directory
    Organization role   what business function that makes them
    permissions         which menus that function may open

The middle tier is the one that earns its place. Several AD groups routinely mean the
same thing to the product — `AURA-Dev` and `platform-eng` are both Engineering — and
without somewhere to say so, the same permission list gets copied onto each group and
they drift. An org role is that place: attach groups to it, and changing what
Engineering may see changes it once.

Stored in DynamoDB rather than Settings for the same reason the graph read-source is:
get_settings() is lru_cached, so anything read from there needs a restart to change.
Adding an AD group must not require a deploy.

Deliberately independent of *where* the groups came from. It takes a list of group
names and returns permissions, so an OIDC provider could feed it later without the
menus, the routes or any endpoint noticing.

── On the org role owning permissions ───────────────────────────────────────────
It used to be `ROLE_PERMISSIONS`, a constant in auth_service, that decided what a
mapped group granted — while the Role Management screen wrote its edits to the
`roles` DynamoDB table, which nothing on this path ever read. Editing a role there
therefore changed local sign-in and nothing else, despite the screen existing to
widen a group's access. Org roles collapse the two into one runtime-editable source;
`ROLE_PERMISSIONS` survives only as clone-from templates and as the permission source
for break-glass accounts.
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


# Prefix for org roles invented for a mapping that granted bare permissions with no
# role to name it after. A mapping that DID name a role keeps that role's id, so the
# `roleId` this module returns is unchanged by the upgrade — the JWT claim, ROLE_LABELS
# and every existing role check keep seeing "user_dev" rather than something new.
_GROUP_ROLE_PREFIX = "group:"


@dataclass
class OrgRole:
    """A business function, and the menus it may open.

    `permissions` is the authority. `based_on` records which built-in role the
    admin cloned to get started and grants nothing on its own — keeping it means the
    UI can show "based on User + Dev" without that becoming a second, competing
    source of permissions, which is precisely the bug this replaces.
    """

    id: str
    label: str = ""
    description: str = ""
    based_on: str = ""
    permissions: list[str] = field(default_factory=list)
    priority: int = 0

    def as_item(self) -> dict:
        return {"id": self.id, "label": self.label, "description": self.description,
                "basedOn": self.based_on, "permissions": self.permissions,
                "priority": self.priority}


@dataclass
class Mapping:
    """One directory group, attached to one org role.

    `role_id` / `permissions` / `priority` are the pre-org-role shape. They are read
    and upgraded (see `_upgrade`) but never written, so a configuration saved by an
    older build keeps resolving identically until someone next presses Save.
    """

    group: str
    org_role_id: str = ""
    # ── legacy, read-only ──
    role_id: str = ""
    permissions: list[str] = field(default_factory=list)
    priority: int = 0

    def as_item(self) -> dict:
        return {"group": self.group, "orgRoleId": self.org_role_id}


@dataclass
class AuthConfig:
    enabled: bool = False
    org_roles: list[OrgRole] = field(default_factory=list)
    mappings: list[Mapping] = field(default_factory=list)
    updated_at: str = ""
    updated_by: str = ""

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "orgRoles": [r.as_item() for r in self.org_roles],
            "mappings": [m.as_item() for m in self.mappings],
            "updatedAt": self.updated_at,
            "updatedBy": self.updated_by,
        }

    @property
    def group_names(self) -> list[str]:
        return [m.group for m in self.mappings]

    def org_role(self, role_id: str) -> OrgRole | None:
        for role in self.org_roles:
            if role.id == role_id:
                return role
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upgrade(config: AuthConfig) -> AuthConfig:
    """Give pre-org-role mappings an org role, in memory, on read.

    Dev already has mappings configured and this is the sign-in path, so a migration
    that must be run before anyone can log in is the wrong shape — get it wrong and
    the way back in is the way that is broken. Upgrading on read means an older
    configuration keeps resolving to exactly the permissions it did before, and the
    upgraded form is only persisted when an admin next saves.

    A mapping naming a built-in role collapses with every other mapping naming the
    same role, which is the behaviour an admin would have wanted all along: three
    groups on `user_dev` become three groups on one org role. A mapping carrying bare
    permissions cannot be collapsed with anything, so it gets an org role of its own,
    named after the group.
    """
    if config.org_roles or not any(m.role_id or m.permissions for m in config.mappings):
        return config

    from src.services.auth_service import ROLE_LABELS, ROLE_PERMISSIONS

    roles: dict[str, OrgRole] = {}
    for mapping in config.mappings:
        if mapping.org_role_id:
            continue
        if mapping.role_id:
            key = mapping.role_id
            base = list(ROLE_PERMISSIONS.get(mapping.role_id, []))
            label = ROLE_LABELS.get(mapping.role_id, mapping.role_id)
            based_on = mapping.role_id
        else:
            key = f"{_GROUP_ROLE_PREFIX}{mapping.group.strip().lower()}"
            base = []
            label = mapping.group
            based_on = ""

        role = roles.get(key)
        if role is None:
            role = OrgRole(id=key, label=label, based_on=based_on,
                           description="Carried over from the previous group mapping.",
                           permissions=list(base), priority=mapping.priority)
            roles[key] = role
        # Extras granted on top of a role were per-group; the union preserves access,
        # which is the property that matters when the alternative is locking someone out.
        for permission in mapping.permissions:
            if permission not in role.permissions:
                role.permissions.append(permission)
        role.priority = max(role.priority, mapping.priority)
        mapping.org_role_id = key

    config.org_roles = list(roles.values())
    return config


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
                org_roles=[
                    OrgRole(id=str(r.get("id", "")),
                            label=str(r.get("label", "")),
                            description=str(r.get("description", "")),
                            based_on=str(r.get("basedOn", "")),
                            permissions=list(r.get("permissions") or []),
                            priority=int(r.get("priority") or 0))
                    for r in (row.get("orgRoles") or []) if r.get("id")
                ],
                mappings=[
                    Mapping(group=str(m.get("group", "")),
                            org_role_id=str(m.get("orgRoleId", "")),
                            role_id=str(m.get("roleId", "")),
                            permissions=list(m.get("permissions") or []),
                            priority=int(m.get("priority") or 0))
                    for m in (row.get("mappings") or []) if m.get("group")
                ],
                updated_at=row.get("updatedAt", ""),
                updated_by=row.get("updatedBy", ""),
            )
            config = _upgrade(config)
    except Exception as exc:  # noqa: BLE001 — a missing table must not break login
        log.debug("auth config read failed, using defaults: %s", exc)

    with _lock:
        _cache = (now, config)
    return config


def set_config(enabled: bool, mappings: list[dict], actor: str,
               org_roles: list[dict] | None = None) -> AuthConfig:
    """Persist the configuration. Writes the org-role shape only.

    `org_roles=None` means "leave them alone" rather than "clear them", so a caller
    that only edits mappings cannot silently delete every org role and with it
    everyone's access.
    """
    global _cache
    from src.database import dynamo_client as db

    roles = org_roles if org_roles is not None else [
        r.as_item() for r in get_config().org_roles
    ]

    db.put_item(TABLE, {
        "configId": CONFIG_ID,
        "enabled": bool(enabled),
        "orgRoles": [
            {"id": str(r.get("id", "")).strip(),
             "label": str(r.get("label", "")).strip(),
             "description": str(r.get("description", "")).strip(),
             "basedOn": str(r.get("basedOn", "")).strip(),
             "permissions": list(r.get("permissions") or []),
             "priority": int(r.get("priority") or 0)}
            for r in roles if str(r.get("id", "")).strip()
        ],
        "mappings": [
            {"group": str(m.get("group", "")).strip(),
             "orgRoleId": str(m.get("orgRoleId", "")).strip()}
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
    """Map a user's directory groups to permissions, via their org roles.

    A user in several mapped groups gets the UNION of their permissions — anything
    else would make membership order-dependent and surprising. `roleId` comes from the
    highest-priority matched org role, because roleLabel and the remaining role checks
    still need a single name.

    Matching is on the group NAME, case-insensitively, so an administrator types
    "AURA-Dev" rather than a full distinguished name.

    The return shape is unchanged from the pre-org-role version on purpose: the
    preview endpoint, the login path and the tests all read these four keys.
    """
    config = _upgrade(config or get_config())
    held = {g.strip().lower() for g in (groups or []) if g and g.strip()}

    matched = [m for m in config.mappings if m.group.strip().lower() in held]
    resolved = [(m, config.org_role(m.org_role_id)) for m in matched]
    # A mapping pointing at an org role that no longer exists grants nothing rather
    # than everything. Save-time validation rejects that state, but a row hand-edited
    # in DynamoDB can still produce it.
    resolved = [(m, r) for m, r in resolved if r is not None]
    if not resolved:
        return {"matched": [], "permissions": [], "roleId": "", "priority": -1}

    permissions: list[str] = []
    for _mapping, role in sorted(resolved, key=lambda pair: -pair[1].priority):
        for permission in role.permissions:
            if permission not in permissions:
                permissions.append(permission)

    top = max(resolved, key=lambda pair: pair[1].priority)[1]
    return {
        "matched": [m.group for m, _ in sorted(resolved, key=lambda pair: -pair[1].priority)],
        "permissions": permissions,
        "roleId": top.id,
        "roleLabel": top.label or top.id,
        "priority": top.priority,
    }

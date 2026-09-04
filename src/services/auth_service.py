"""Auth service — DynamoDB-backed users and roles.

Tables:
  aura-users   PK: userId    fields: username, email, passwordHash, roleId, status, createdAt, lastLogin
  aura-roles   PK: roleId    fields: name, label, permissions (list of feature keys)

Access for directory users is granted by AD group → organization role, in
`auth_config`. What lives here is local sign-in and the built-in role templates.
"""
import uuid
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from src.config_settings import get_settings
from src.database.dynamo_client import put_item, get_item, scan_items, update_item, delete_item

logger = logging.getLogger(__name__)

# ── Role definitions ─────────────────────────────────────────────────────────

# The six built-in roles. Two narrow jobs now, and neither is granting access to a
# directory user:
#   * clone-from templates in the org-role editor, so an admin starts from something
#     sensible rather than an empty checklist;
#   * the permission set for break-glass and other local accounts, which must keep
#     working when org roles are misconfigured — recovering from exactly that is what
#     break-glass is for.
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "user_dev": [
        "dashboard", "dev_workspace", "knowledge_graph",
        "ontology", "connectors", "upload", "logs",
    ],
    "user_qa": [
        "dashboard", "qa_workspace", "knowledge_graph",
        "upload", "logs",
    ],
    "user_ops": [
        "dashboard", "aiops", "observability", "knowledge_graph", "logs",
        # `connectors` is required so an ops user can configure the observability
        # providers their own page depends on.
        "connectors",
    ],
    "ontology_maintainer": [
        "dashboard", "dev_workspace", "knowledge_graph",
        "ontology", "ontology_maintain", "connectors",
        "scheduler", "logs",
    ],
    "admin": [
        "dashboard", "dev_workspace", "qa_workspace", "aiops", "observability",
        "knowledge_graph", "ontology", "ontology_maintain",
        "connectors", "upload", "logs", "settings", "user_management",
        "scheduler",
    ],
    "super_admin": [
        "dashboard", "dev_workspace", "qa_workspace", "aiops", "observability",
        "knowledge_graph", "ontology", "ontology_maintain",
        "connectors", "upload", "logs", "settings", "user_management",
        "scheduler",
    ],
}

ROLE_LABELS: dict[str, str] = {
    "user_dev": "User + Dev",
    "user_qa": "User + QA",
    "user_ops": "User + Ops",
    "ontology_maintainer": "Ontology Maintainer",
    "admin": "Admin",
    "super_admin": "Super Admin",
}


# ── Password hashing ──────────────────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    try:
        import bcrypt
        return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)).decode()
    except ImportError:
        # Fallback if bcrypt not installed — sha256 (not for prod)
        import hashlib
        return "sha256:" + hashlib.sha256(plain.encode()).hexdigest()


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        import bcrypt
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ImportError:
        import hashlib
        return hashed == "sha256:" + hashlib.sha256(plain.encode()).hexdigest()


# ── Seed data ─────────────────────────────────────────────────────────────────

def seed_default_data() -> None:
    """Insert default roles and admin user if they don't exist.
    For built-in roles, always merge code-defined permissions into the stored role
    so newly added permissions take effect without manual intervention.
    """
    for role_id, code_permissions in ROLE_PERMISSIONS.items():
        existing = get_item("roles", {"roleId": role_id})
        if not existing:
            put_item("roles", {
                "roleId": role_id,
                "name": role_id,
                "label": ROLE_LABELS[role_id],
                "permissions": code_permissions,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            logger.info("Seeded role: %s", role_id)
        else:
            # Merge: add any new code-defined permissions not yet in the stored role
            stored = set(existing.get("permissions", []))
            added = [p for p in code_permissions if p not in stored]
            if added:
                merged = list(stored) + added
                update_item("roles", {"roleId": role_id}, {"permissions": merged})
                logger.info("Updated role %s: added permissions %s", role_id, added)

    # Seed super_admin user
    existing_users = scan_items("users")
    if not existing_users:
        admin_id = str(uuid.uuid4())
        put_item("users", {
            "userId": admin_id,
            "username": "admin",
            "email": "admin@aura.com",
            "passwordHash": _hash_password("Admin@123"),
            "roleId": "super_admin",
            "status": "active",
            # Break-glass: still able to sign in when the directory is unreachable.
            # Without at least one such account, a wrong bind DN locks everyone out —
            # including out of the screen where the directory is configured.
            "breakGlass": True,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "lastLogin": None,
        })
        logger.info("Seeded default super_admin user (username: admin, password: Admin@123)")

    else:
        # An environment seeded before break-glass existed has no marked account, so
        # enabling LDAP would strand it. Promote the first super_admin once.
        if not any(u.get("breakGlass") for u in existing_users):
            for u in existing_users:
                if u.get("roleId") == "super_admin":
                    update_item("users", {"userId": u["userId"]}, {"breakGlass": True})
                    logger.info("Marked %s as a break-glass account", u.get("username"))
                    break


# ── User CRUD ─────────────────────────────────────────────────────────────────

def get_user_by_username(username: str) -> dict | None:
    users = scan_items("users")
    for u in users:
        if u.get("username") == username:
            return u
    return None


def get_user_by_id(user_id: str) -> dict | None:
    return get_item("users", {"userId": user_id})


def list_users() -> list[dict]:
    users = scan_items("users")
    return [_safe_user(u) for u in users]


def create_user(username: str, email: str, role_id: str, password: str) -> dict:
    """Kept for `seed_default_data`'s break-glass account only — no endpoint calls it.

    Local accounts are not a way to grant access any more; directory groups are.
    """
    if get_user_by_username(username):
        raise ValueError(f"Username '{username}' already exists")
    user_id = str(uuid.uuid4())
    user = {
        "userId": user_id,
        "username": username,
        "email": email,
        "passwordHash": _hash_password(password),
        "roleId": role_id,
        "status": "active",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "lastLogin": None,
    }
    put_item("users", user)
    return _safe_user(user)


def update_user(user_id: str, updates: dict) -> dict | None:
    if "password" in updates:
        updates["passwordHash"] = _hash_password(updates.pop("password"))
    updates.pop("passwordHash", None) if "passwordHash" in updates and "password" not in updates else None
    result = update_item("users", {"userId": user_id}, updates)
    return _safe_user(result) if result else None


def deactivate_user(user_id: str) -> None:
    update_item("users", {"userId": user_id}, {"status": "inactive"})


def _safe_user(user: dict) -> dict:
    """Remove password hash before returning to API."""
    return {k: v for k, v in user.items() if k != "passwordHash"}


# ── Role CRUD ─────────────────────────────────────────────────────────────────

def list_roles() -> list[dict]:
    return scan_items("roles")


def get_role(role_id: str) -> dict | None:
    return get_item("roles", {"roleId": role_id})


# `update_role_permissions` used to live here. It wrote to the `roles` table, which
# the directory path never read — so editing a role in the UI changed local sign-in
# and nothing else, while the screen existed to widen a group's access. Org roles in
# `auth_config` are now the one editable source; see that module's header.


# ── Auth ──────────────────────────────────────────────────────────────────────

class DirectoryRefused(Exception):
    """Authenticated against the directory but holds no mapped group.

    A distinct exception because the caller must say WHICH groups to ask for. A bare
    "invalid credentials" sends the user to reset a password that was correct.
    """


def authenticate(username: str, password: str) -> dict | None:
    """Verify credentials and return the user with their permissions.

    Dispatches to the directory when LDAP is enabled, and falls back to local
    accounts otherwise — or, when the directory is unreachable, to break-glass
    accounts only. Everything downstream reads `permissions` from the dict this
    returns, so directory-driven access needs no change anywhere else.
    """
    from src.services import auth_config

    config = auth_config.get_config()
    if not config.enabled:
        return _authenticate_local(username, password)

    from src.services import ldap_auth
    try:
        directory_user = ldap_auth.authenticate(username, password)
    except ldap_auth.LdapError as exc:
        # The DIRECTORY failed, not the credentials. Break-glass only: a normal local
        # account must not become usable just because AD is down, or an outage would
        # silently widen who can sign in.
        logger.error("LDAP unavailable (%s) — break-glass accounts only", exc)
        return _authenticate_local(username, password, break_glass_only=True)

    if directory_user is None:
        # Wrong password, or no such directory user. Never falls back: that would let
        # a stale local password outlive the directory account it mirrors.
        return None

    resolved = auth_config.resolve(directory_user.groups, config)
    if not resolved["permissions"]:
        raise DirectoryRefused(
            f"{username} authenticated but belongs to no AURA group. Ask an "
            f"administrator to add you to one of: "
            f"{', '.join(config.group_names) or '(none configured)'}")

    role_id = resolved["roleId"]
    logger.info("LDAP login %s — groups=%s role=%s", username,
                resolved["matched"], role_id)
    return {
        "userId": directory_user.user_id,
        "username": directory_user.username,
        "email": directory_user.email,
        "role": role_id,
        # The org role's own label first — it is what an admin named this business
        # function, and ROLE_LABELS only knows the six built-ins.
        "roleLabel": (resolved.get("roleLabel")
                      or ROLE_LABELS.get(role_id)
                      or directory_user.display_name or "Directory User"),
        "permissions": resolved["permissions"],
        # Carried into the token so each request can re-resolve without an LDAP call.
        "groups": directory_user.groups,
        "local": False,
    }


def _authenticate_local(username: str, password: str,
                        break_glass_only: bool = False) -> dict | None:
    user = get_user_by_username(username)
    if not user:
        return None
    if user.get("status") != "active":
        return None
    if not _verify_password(password, user.get("passwordHash", "")):
        return None
    if break_glass_only and not user.get("breakGlass"):
        logger.warning("Local login refused for %r: the directory is unavailable and "
                       "this account is not marked break-glass", username)
        return None
    # Update last login
    update_item("users", {"userId": user["userId"]},
                {"lastLogin": datetime.now(timezone.utc).isoformat()})
    role = get_role(user.get("roleId", "user_dev"))
    permissions = role.get("permissions", []) if role else []
    return {
        "userId": user["userId"],
        "username": user["username"],
        "email": user.get("email", ""),
        "role": user.get("roleId", "user_dev"),
        "roleLabel": ROLE_LABELS.get(user.get("roleId", "user_dev"), "User"),
        "permissions": permissions,
        "groups": [],
        # Marks the token as local so `verify_token` resolves it from the roles table
        # rather than from org roles — see the break-glass note there.
        "local": True,
    }


def create_token(user_data: dict) -> str:
    """Mint a session token.

    Identity only. `permissions` is deliberately NOT a claim: it used to be, and the
    token lives for `jwt_expire_minutes` (8 hours by default), so moving someone
    between AD groups left them with their old menus for the rest of the working day.
    `groups` and `local` are carried instead — enough to re-resolve permissions on
    each request without another directory round trip.
    """
    s = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=s.jwt_expire_minutes)
    payload = {
        "sub": user_data["username"],
        "userId": user_data["userId"],
        "role": user_data["role"],
        "groups": user_data.get("groups", []),
        "local": bool(user_data.get("local")),
        "exp": expire,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """Decode a token and resolve current permissions for it.

    Permissions come from the live configuration, not from the token, so an edit to
    an org role reaches every signed-in user on their next request. Tokens minted by
    an older build still carry a `permissions` claim; it is honoured as a fallback so
    a deploy does not sign everyone out.
    """
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=["HS256"])
    except JWTError:
        return None

    username = payload["sub"]
    claimed = list(payload.get("permissions") or [])
    role = payload.get("role", "user_dev")

    if payload.get("local"):
        # Break-glass and local accounts resolve from the roles table, never from org
        # roles: a misconfigured org role is one of the things break-glass exists to
        # recover from, so it must not be able to lock break-glass out.
        stored = get_role(role) or {}
        permissions = list(stored.get("permissions") or ROLE_PERMISSIONS.get(role, []))
        return {"username": username, "userId": payload.get("userId", ""),
                "role": role, "permissions": permissions}

    groups = list(payload.get("groups") or [])
    if not groups:
        # Pre-upgrade token, or a local account minted before `local` existed.
        return {"username": username, "userId": payload.get("userId", ""),
                "role": role, "permissions": claimed}

    from src.services import auth_config
    resolved = auth_config.resolve(_current_groups(username, groups))
    if not resolved["permissions"]:
        # Removed from every mapped group since sign-in. Returning the token's old
        # permissions would keep them in until it expired, which is the behaviour
        # this change exists to remove.
        logger.info("Access revoked for %s — no mapped group remains", username)
        return {"username": username, "userId": payload.get("userId", ""),
                "role": "", "permissions": []}

    return {
        "username": username,
        "userId": payload.get("userId", ""),
        "role": resolved["roleId"],
        "permissions": resolved["permissions"],
    }


# Directory group membership, cached per user.
#
# Re-resolving permissions per request fixes a MAPPING edit immediately, because the
# mapping is read from a 10s-cached config. It does not by itself fix a GROUP
# membership change — the groups in the token are whatever they were at sign-in. This
# cache closes that gap without an LDAP round trip on every request.
_GROUP_TTL_SECONDS = 300.0
_group_cache: dict[str, tuple[float, list[str]]] = {}
_group_lock = threading.Lock()


def _current_groups(username: str, fallback: list[str]) -> list[str]:
    """This user's directory groups, re-read at most every `_GROUP_TTL_SECONDS`.

    Falls back to the token's groups whenever the directory cannot answer. An outage
    must not revoke everyone mid-session — that would turn a directory blip into a
    site-wide outage, which is a worse failure than access being a few minutes stale.
    """
    now = time.monotonic()
    with _group_lock:
        hit = _group_cache.get(username)
        if hit and (now - hit[0]) < _GROUP_TTL_SECONDS:
            return hit[1]

    groups = fallback
    try:
        from src.services import ldap_auth
        looked_up = ldap_auth.lookup_groups(username)
        if looked_up.get("ok"):
            groups = list(looked_up.get("groups") or [])
    except Exception as exc:  # noqa: BLE001 — never fail a request over this
        logger.debug("group refresh failed for %s, using token groups: %s", username, exc)

    with _group_lock:
        _group_cache[username] = (now, groups)
    return groups


def forget_cached_groups(username: str = "") -> None:
    """Drop cached group membership so the next request re-reads the directory.

    Not needed after a mapping edit — those are read through `get_config`, which has
    its own 10s cache. This is for the case the TTL cannot cover: an admin who has
    just moved someone in AD and wants to see it immediately, and for tests.
    """
    with _group_lock:
        if username:
            _group_cache.pop(username, None)
        else:
            _group_cache.clear()

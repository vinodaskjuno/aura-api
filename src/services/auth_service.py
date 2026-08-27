"""Auth service — DynamoDB-backed users and roles.

Tables:
  aura-users   PK: userId    fields: username, email, passwordHash, roleId, status, createdAt, lastLogin
  aura-roles   PK: roleId    fields: name, label, permissions (list of feature keys)

5 built-in roles:
  user_dev   user_qa   user_ops   admin   super_admin
"""
import uuid
import logging
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from src.config_settings import get_settings
from src.database.dynamo_client import put_item, get_item, scan_items, update_item, delete_item

logger = logging.getLogger(__name__)

# ── Role definitions ─────────────────────────────────────────────────────────

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "user_dev": [
        "dashboard", "dev_workspace", "knowledge_graph",
        "ontology", "advisor", "connectors", "upload", "logs",
    ],
    "user_qa": [
        "dashboard", "qa_workspace", "knowledge_graph",
        "advisor", "upload", "logs",
    ],
    "user_ops": [
        "dashboard", "aiops", "knowledge_graph", "advisor", "logs",
    ],
    "ontology_maintainer": [
        "dashboard", "dev_workspace", "knowledge_graph",
        "ontology", "ontology_maintain", "connectors",
        "scheduler", "advisor", "logs",
    ],
    "admin": [
        "dashboard", "dev_workspace", "qa_workspace", "aiops",
        "knowledge_graph", "ontology", "ontology_maintain", "advisor",
        "connectors", "upload", "logs", "settings", "user_management",
        "scheduler",
    ],
    "super_admin": [
        "dashboard", "dev_workspace", "qa_workspace", "aiops",
        "knowledge_graph", "ontology", "ontology_maintain", "advisor",
        "connectors", "upload", "logs", "settings", "user_management",
        "role_management", "scheduler",
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
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "lastLogin": None,
        })
        logger.info("Seeded default super_admin user (username: admin, password: Admin@123)")


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


def update_role_permissions(role_id: str, permissions: list[str]) -> dict | None:
    return update_item("roles", {"roleId": role_id}, {"permissions": permissions})


# ── Auth ──────────────────────────────────────────────────────────────────────

def authenticate(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user:
        return None
    if user.get("status") != "active":
        return None
    if not _verify_password(password, user.get("passwordHash", "")):
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
    }


def create_token(user_data: dict) -> str:
    s = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=s.jwt_expire_minutes)
    payload = {
        "sub": user_data["username"],
        "userId": user_data["userId"],
        "role": user_data["role"],
        "permissions": user_data["permissions"],
        "exp": expire,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=["HS256"])
        return {
            "username": payload["sub"],
            "userId": payload.get("userId", ""),
            "role": payload.get("role", "user_dev"),
            "permissions": payload.get("permissions", []),
        }
    except JWTError:
        return None

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Callable
from src.services.auth_service import (
    authenticate, create_token, verify_token,
    list_users, create_user, update_user, deactivate_user,
    list_roles, get_role, update_role_permissions,
    ROLE_PERMISSIONS,
)

router = APIRouter(prefix="/auth", tags=["auth"])
bearer = HTTPBearer()


# ── Models ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    userId: str
    role: str
    roleLabel: str
    permissions: list[str]


class CreateUserRequest(BaseModel):
    username: str
    email: str
    roleId: str
    password: str


class UpdateUserRequest(BaseModel):
    email: str | None = None
    roleId: str | None = None
    status: str | None = None
    password: str | None = None


class UpdateRoleRequest(BaseModel):
    permissions: list[str]


# ── Auth dependencies ─────────────────────────────────────────────────────────

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    user = verify_token(creds.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def require_role(*allowed_roles: str) -> Callable:
    """FastAPI dependency factory — raises 403 if user role not in allowed_roles."""
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Required roles: {', '.join(allowed_roles)}"
            )
        return user
    return _check


def require_permission(permission: str) -> Callable:
    """FastAPI dependency factory — raises 403 if user lacks a specific permission."""
    def _check(user: dict = Depends(get_current_user)) -> dict:
        if permission not in user.get("permissions", []):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {permission}"
            )
        return user
    return _check


# ── Auth endpoints ────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest):
    from src.services.auth_service import DirectoryRefused
    try:
        user = authenticate(req.username, req.password)
    except DirectoryRefused as exc:
        # 403, not 401: the password was correct. Returning "invalid credentials"
        # would send the user to reset a password that works, and the real fix —
        # being added to a group — would never occur to them.
        raise HTTPException(status_code=403, detail=str(exc))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials or account inactive")
    token = create_token(user)
    return TokenResponse(
        access_token=token,
        username=user["username"],
        userId=user["userId"],
        role=user["role"],
        roleLabel=user["roleLabel"],
        permissions=user["permissions"],
    )


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    """The caller's identity, plus whether access is coming from the directory.

    `directoryManaged` is here rather than on the admin-only LDAP endpoints because
    the pages that need it (User and Role Management) are gated on
    role_management, which does not imply user_management — they could not read the
    LDAP config to find out.
    """
    from src.services.auth_config import get_config
    try:
        managed = get_config().enabled
    except Exception:  # noqa: BLE001 — a config read must never break sign-in state
        managed = False
    return {**user, "directoryManaged": managed}


# ── User management (admin+) ──────────────────────────────────────────────────

@router.get("/users")
def get_users(user: dict = Depends(require_role("admin", "super_admin"))):
    return list_users()


@router.post("/users")
def post_user(req: CreateUserRequest, user: dict = Depends(require_role("admin", "super_admin"))):
    if req.roleId not in ROLE_PERMISSIONS:
        raise HTTPException(status_code=400, detail=f"Invalid roleId: {req.roleId}")
    try:
        return create_user(req.username, req.email, req.roleId, req.password)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.put("/users/{user_id}")
def put_user(user_id: str, req: UpdateUserRequest,
             user: dict = Depends(require_role("admin", "super_admin"))):
    updates = req.model_dump(exclude_none=True)
    result = update_user(user_id, updates)
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result


@router.delete("/users/{user_id}")
def del_user(user_id: str, user: dict = Depends(require_role("super_admin",))):
    deactivate_user(user_id)
    return {"ok": True}


# ── Role management (super_admin) ─────────────────────────────────────────────

@router.get("/roles")
def get_roles(user: dict = Depends(require_role("admin", "super_admin"))):
    return list_roles()


@router.put("/roles/{role_id}")
def put_role(role_id: str, req: UpdateRoleRequest,
             user: dict = Depends(require_role("super_admin",))):
    result = update_role_permissions(role_id, req.permissions)
    if not result:
        raise HTTPException(status_code=404, detail="Role not found")
    return result


# ── Directory (LDAP / Active Directory) ──────────────────────────────────────
#
# Access is granted by AD group membership; these endpoints configure the mapping
# from group to permission. Gated on `user_management`, the same permission that
# gated the local user administration this replaces.

class LdapMapping(BaseModel):
    group: str
    roleId: str = ""
    permissions: list[str] = []
    priority: int = 0


class LdapConfigRequest(BaseModel):
    enabled: bool
    mappings: list[LdapMapping] = []


@router.get("/ldap/config")
def get_ldap_config(_: dict = Depends(require_permission("user_management"))):
    from src.config_settings import get_settings
    from src.services import auth_config
    from src.services.auth_service import ROLE_PERMISSIONS

    s = get_settings()
    return {
        **auth_config.get_config(refresh=True).as_dict(),
        # Connection facts are read-only here: they carry a password and live in
        # Secrets Manager, so they are shown for confirmation, not editing.
        "connection": {
            "uri": s.ldap_uri,
            "baseDn": s.ldap_base_dn,
            "bindDn": s.ldap_bind_dn,
            "userFilter": s.ldap_user_filter,
            "allowInsecure": s.ldap_allow_insecure,
            "bindPasswordSet": bool(s.ldap_bind_password),
        },
        "availableRoles": sorted(ROLE_PERMISSIONS),
        "availablePermissions": sorted({p for v in ROLE_PERMISSIONS.values() for p in v}),
    }


@router.put("/ldap/config")
def put_ldap_config(body: LdapConfigRequest,
                    user: dict = Depends(require_permission("user_management"))):
    from src.services import auth_config
    from src.services.auth_service import ROLE_PERMISSIONS

    known = set(ROLE_PERMISSIONS)
    for m in body.mappings:
        if m.roleId and m.roleId not in known:
            raise HTTPException(status_code=400,
                                detail=f"Unknown role {m.roleId!r}. Known: {sorted(known)}")
        if not m.roleId and not m.permissions:
            raise HTTPException(
                status_code=400,
                detail=f"Mapping for {m.group!r} grants nothing — give it a role or "
                       "an explicit permission list.")

    # Enabling with no mappings would authenticate everyone and then refuse them all,
    # which reads as a broken product rather than a misconfiguration.
    if body.enabled and not body.mappings:
        raise HTTPException(
            status_code=400,
            detail="Enable directory sign-in only once at least one group is mapped, "
                   "otherwise every directory user is refused after authenticating.")

    return auth_config.set_config(
        body.enabled, [m.model_dump() for m in body.mappings], user["username"]).as_dict()


@router.post("/ldap/test")
def test_ldap(_: dict = Depends(require_permission("user_management"))):
    from src.services import ldap_auth
    return ldap_auth.test_connection()


@router.get("/ldap/preview")
def preview_ldap_user(username: str,
                      _: dict = Depends(require_permission("user_management"))):
    """What access would this person get? Answered without their password, so an
    administrator can debug "why can't Priya see QualityMind" in seconds instead of
    asking her to try again."""
    from src.services import auth_config, ldap_auth

    found = ldap_auth.lookup_groups(username)
    if not found.get("ok"):
        return {**found, "permissions": [], "matchedGroups": [], "wouldSignIn": False}

    config = auth_config.get_config(refresh=True)
    resolved = auth_config.resolve(found["groups"], config)
    return {
        "ok": True,
        "dn": found.get("dn", ""),
        "groupSource": found.get("groupSource", ""),
        "groups": found["groups"],
        "matchedGroups": resolved["matched"],
        "unmatchedGroups": [g for g in found["groups"] if g not in resolved["matched"]],
        "roleId": resolved["roleId"],
        "permissions": resolved["permissions"],
        "wouldSignIn": bool(resolved["permissions"]),
        "configuredGroups": config.group_names,
    }

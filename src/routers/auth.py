from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Callable
from src.services.auth_service import (
    authenticate, create_token, verify_token,
    list_users, update_user, get_role,
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


class RotatePasswordRequest(BaseModel):
    password: str


# ── Auth dependencies ─────────────────────────────────────────────────────────

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> dict:
    user = verify_token(creds.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


# `require_role` used to live here. It guarded the six user- and role-administration
# endpoints and nothing else, so retiring those retired it — and with it a real trap:
# a directory user whose org role was not one of the six built-in names passed NO
# require_role check, however much access their groups actually granted. Everything
# now gates on a permission, which is the thing the menus read too.


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

    `permissions` is resolved here and now rather than read from the token, so a
    change to an organization role reaches the UI on the next poll instead of at the
    next sign-in.
    """
    from src.services.auth_config import get_config
    try:
        managed = get_config().enabled
    except Exception:  # noqa: BLE001 — a config read must never break sign-in state
        managed = False
    return {**user, "directoryManaged": managed}


# ── Break-glass accounts ─────────────────────────────────────────────────────
#
# What is left of local user administration. Creating and editing local accounts is
# gone: access is granted by directory group membership, and a second place to grant
# it is a second place for it to be wrong.
#
# Rotating a break-glass password stays, because break-glass is what gets you back in
# when the directory is misconfigured — and a credential you cannot rotate is one you
# eventually stop trusting.

@router.get("/users")
def get_users(_: dict = Depends(require_permission("user_management"))):
    """The local account roster. Break-glass accounts first — they are the point."""
    users = list_users()
    return sorted(users, key=lambda u: (not u.get("breakGlass"), u.get("username", "")))


@router.post("/users/{user_id}/password")
def rotate_password(user_id: str, req: RotatePasswordRequest,
                    _: dict = Depends(require_permission("user_management"))):
    """Set a new password on a local account."""
    if len(req.password) < 12:
        raise HTTPException(
            status_code=400,
            detail="A break-glass password must be at least 12 characters.")
    result = update_user(user_id, {"password": req.password})
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    log.info("Break-glass password rotated for %s", result.get("username"))
    return {"ok": True, "username": result.get("username")}


# ── Directory (LDAP / Active Directory) ──────────────────────────────────────
#
# Access is granted by AD group membership: a group attaches to an organization role,
# and the organization role grants menus. These endpoints configure both. Gated on
# `user_management`, the same permission that gated the local user administration
# this replaces.

class LdapMapping(BaseModel):
    """One directory group, attached to one organization role."""

    group: str
    orgRoleId: str = ""


class LdapOrgRole(BaseModel):
    """A business function and the menus it grants.

    `permissions` is the authority. `basedOn` only records which built-in role was
    cloned to start it, so the UI can say "based on User + Dev" without that becoming
    a second source of permissions.
    """

    id: str
    label: str = ""
    description: str = ""
    basedOn: str = ""
    permissions: list[str] = []
    priority: int = 0


class LdapConfigRequest(BaseModel):
    enabled: bool
    orgRoles: list[LdapOrgRole] = []
    mappings: list[LdapMapping] = []


@router.get("/ldap/config")
def get_ldap_config(_: dict = Depends(require_permission("user_management"))):
    from src.config_settings import get_settings
    from src.services import auth_config
    from src.services.auth_service import ROLE_LABELS, ROLE_PERMISSIONS

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
            # Terraform creates the secret holding REPLACE_ME and never sets its
            # value, so a non-empty string is not the same as a configured one.
            # Reporting "set" for the placeholder would send an administrator
            # hunting a connection fault that is really an unstored password.
            "bindPasswordSet": bool(s.ldap_bind_password)
                               and s.ldap_bind_password != "REPLACE_ME",
        },
        "availableRoles": sorted(ROLE_PERMISSIONS),
        "availablePermissions": sorted({p for v in ROLE_PERMISSIONS.values() for p in v}),
        # Clone-from templates for the org-role editor. Sent with their permission
        # sets so picking one fills the checklist without a second request.
        "roleTemplates": [
            {"id": rid, "label": ROLE_LABELS.get(rid, rid), "permissions": perms}
            for rid, perms in sorted(ROLE_PERMISSIONS.items())
        ],
    }


@router.put("/ldap/config")
def put_ldap_config(body: LdapConfigRequest,
                    user: dict = Depends(require_permission("user_management"))):
    from src.services import auth_config

    roles = {r.id: r for r in (body.orgRoles or [])}
    if len(roles) != len(body.orgRoles or []):
        raise HTTPException(status_code=400,
                            detail="Two organization roles share an id.")

    for role in body.orgRoles or []:
        if not role.permissions:
            raise HTTPException(
                status_code=400,
                detail=f"{role.label or role.id!r} grants no menus. An organization "
                       "role that grants nothing refuses everyone attached to it.")

    for m in body.mappings:
        if not m.orgRoleId:
            raise HTTPException(
                status_code=400,
                detail=f"Group {m.group!r} is not attached to an organization role.")
        if m.orgRoleId not in roles:
            raise HTTPException(
                status_code=400,
                detail=f"Group {m.group!r} points at unknown organization role "
                       f"{m.orgRoleId!r}.")

    # Enabling with no mappings would authenticate everyone and then refuse them all,
    # which reads as a broken product rather than a misconfiguration.
    if body.enabled and not body.mappings:
        raise HTTPException(
            status_code=400,
            detail="Enable directory sign-in only once at least one group is mapped, "
                   "otherwise every directory user is refused after authenticating.")

    # Refuse a configuration that locks every directory administrator out of this
    # screen. Break-glass would still get someone back in, but a save that quietly
    # removes the only way to undo itself should not be accepted in the first place.
    if body.enabled:
        grants_admin = any(
            "user_management" in roles[m.orgRoleId].permissions for m in body.mappings)
        if not grants_admin:
            raise HTTPException(
                status_code=400,
                detail="No mapped group would grant User Management, so nobody could "
                       "reach this screen to undo it. Give at least one organization "
                       "role that permission before saving.")

    return auth_config.set_config(
        body.enabled,
        [m.model_dump() for m in body.mappings],
        user["username"],
        org_roles=[r.model_dump() for r in (body.orgRoles or [])],
    ).as_dict()


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

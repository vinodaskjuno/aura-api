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
    user = authenticate(req.username, req.password)
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
    return user


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

"""AURA AI Gateway — API key management endpoints.

POST   /gateway/keys              — create a new key (returned once, store it)
GET    /gateway/keys              — list caller's active keys
DELETE /gateway/keys/{keyId}      — revoke a key
GET    /gateway/keys/all          — admin: list all users' keys
GET    /gateway/keys/me/{tool}    — get-or-create a key for a specific tool label
                                    (used by the VS Code plugin on login)
POST   /gateway/keys/me/{tool}/rotate — replace that key and return the new plaintext
                                    (recovery when the client no longer holds it)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.services.gateway_service import (
    generate_api_key,
    get_or_create_tool_key,
    rotate_tool_key,
    list_user_keys,
    revoke_api_key,
    list_all_keys,
)

router = APIRouter(tags=["gateway-keys"])

# Valid tool labels the VS Code plugin can request keys for
_VALID_TOOL_LABELS = {
    "aura-plugin", "claude-ext", "claude-cli", "codex-cli", "gemini-cli", "unknown",
    # Emitted by the OTLP receiver from Claude Code's app.entrypoint
    "claude-code-cli", "claude-code-ext", "claude-code-sdk",
    # AI Observability onboarding: a team instrumenting its own agent asks for one
    # of these and gets a key it can paste into the Opik SDK or a raw OTel exporter.
    # Separate labels so usage can be attributed per integration style.
    "opik-sdk", "otel-sdk",
    # Demo agents (aura-infra/demo-agents). One label per agent, plus one for the
    # shared trace export, so that revoking a single agent's key is a one-agent event
    # rather than taking the whole demo down — which is a failure drill in
    # DEMO_AGENTS.md, not just a claim. They are listed here because this allowlist is
    # what get_or_create_tool_key validates against: without an entry, provisioning a
    # key for an agent fails with "Unknown tool_label" and the agent silently falls
    # back to synthetic spans.
    "demo-rag", "demo-tools", "demo-chat", "demo-flaky", "demo-traces",
    # Self-hosted QualityMind runner (src/qatest/agent.py). Same reason as the demo
    # labels: this allowlist is what get_or_create_tool_key validates against, so
    # without an entry the runner cannot be given a key at all.
    "qa-runner",
}


class CreateKeyRequest(BaseModel):
    label: str = "default"
    tool_label: str = "unknown"


@router.post("/keys", status_code=201)
def create_gateway_key(body: CreateKeyRequest, user: dict = Depends(get_current_user)):
    """Generate a new gateway API key for the calling user."""
    user_id = user.get("userId", "")
    return generate_api_key(user_id, label=body.label, tool_label=body.tool_label)


@router.get("/keys/me/{tool_label}")
def get_or_create_my_tool_key(tool_label: str, user: dict = Depends(get_current_user)):
    """Get-or-create a gateway key for the given tool label.

    The VS Code plugin calls this once per tool on login:
      GET /gateway/keys/me/aura-plugin
      GET /gateway/keys/me/claude-ext
      GET /gateway/keys/me/claude-cli
      GET /gateway/keys/me/codex-cli

    If an active key already exists for this (user, tool_label) pair the existing
    key metadata is returned (plaintext key is NOT re-shown after creation).
    If no active key exists a new one is created and the plaintext key is returned
    once — the plugin must store it in VS Code SecretStorage immediately.

    Response:
      { keyId, key (only on first creation), keyHint, toolLabel, createdAt, exists }
    """
    if tool_label not in _VALID_TOOL_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown tool_label '{tool_label}'")
    user_id = user.get("userId", "")
    return get_or_create_tool_key(user_id, tool_label)


@router.post("/keys/me/{tool_label}/rotate")
def rotate_my_tool_key(tool_label: str, user: dict = Depends(get_current_user)):
    """Replace this tool's key and return the new plaintext once.

    The plaintext key is only ever returned at creation, so a client that has
    lost it (SecretStorage cleared, reinstall, new machine) cannot recover it --
    /keys/me/{tool} would keep answering {"exists": true} with no key, and
    anything depending on it, such as Claude Code telemetry, would silently stop
    reporting. Rotation replaces the key so the client can store a usable one.

    Rotation invalidates the previous key everywhere it was in use, so clients
    should call this only when they hold no key at all.
    """
    if tool_label not in _VALID_TOOL_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown tool_label '{tool_label}'")
    user_id = user.get("userId", "")
    return rotate_tool_key(user_id, tool_label)


@router.get("/keys")
def get_my_keys(user: dict = Depends(get_current_user)):
    """List active gateway API keys for the calling user."""
    user_id = user.get("userId", "")
    return {"keys": list_user_keys(user_id)}


@router.delete("/keys/{key_id}", status_code=200)
def delete_gateway_key(key_id: str, user: dict = Depends(get_current_user)):
    """Revoke a gateway API key."""
    user_id = user.get("userId", "")
    is_admin = user.get("role", "") in ("admin", "super_admin")
    revoke_api_key(key_id, user_id, is_admin=is_admin)
    return {"ok": True, "keyId": key_id}


@router.get("/keys/all")
def get_all_keys(user: dict = Depends(get_current_user)):
    """List all gateway API keys across all users (admin only)."""
    if user.get("role", "") not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return {"keys": list_all_keys()}

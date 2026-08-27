"""Chat history service — DynamoDB-backed with in-memory fallback."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

# In-memory store used when DynamoDB is unreachable
_in_memory: dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_session(
    session_id: str,
    user_id: str,
    project_name: str,
    session_name: str,
    project_id: str = "",
    messages: list[dict] | None = None,
) -> dict:
    now = _now_iso()
    existing = _in_memory.get(session_id, {})
    return {
        "sessionId": session_id,
        "userId": user_id,
        "projectName": project_name,
        "projectId": project_id,
        "sessionName": session_name,
        "createdAt": existing.get("createdAt", now),
        "updatedAt": now,
        "messageCount": len(messages) if messages is not None else existing.get("messageCount", 0),
        "messages": messages if messages is not None else existing.get("messages", []),
    }


# ── DynamoDB helpers ──────────────────────────────────────────────────────────

_TABLE = "chat-sessions"


def _dynamo_put(session: dict) -> None:
    from src.database import dynamo_client as db
    db.put_item(_TABLE, session)


def _dynamo_get(session_id: str, user_id: str) -> dict | None:
    from src.database import dynamo_client as db
    return db.get_item(_TABLE, {"sessionId": session_id, "userId": user_id})


def _dynamo_delete(session_id: str, user_id: str) -> None:
    from src.database import dynamo_client as db
    db.delete_item(_TABLE, {"sessionId": session_id, "userId": user_id})


def _dynamo_list_user(user_id: str) -> list[dict]:
    from src.database import dynamo_client as db
    from boto3.dynamodb.conditions import Attr
    return db.scan_items(_TABLE, filter_expr=Attr("userId").eq(user_id))


# ── Public API ────────────────────────────────────────────────────────────────

def create_or_update_session(
    session_id: str,
    user_id: str,
    project_name: str,
    session_name: str,
    project_id: str = "",
) -> dict:
    # Try to get existing record — preserve messages, projectName, and projectId
    existing_messages: list[dict] | None = None
    existing_record: dict = {}
    try:
        existing = _dynamo_get(session_id, user_id)
        if existing:
            existing_messages = existing.get("messages", [])
            existing_record = existing
    except Exception:
        mem = _in_memory.get(session_id, {})
        existing_messages = mem.get("messages")
        existing_record = mem

    # Never overwrite a known projectName/projectId with an empty value
    resolved_project_name = project_name or existing_record.get("projectName", "")
    resolved_project_id = project_id or existing_record.get("projectId", "")

    session = _build_session(
        session_id, user_id, resolved_project_name, session_name, resolved_project_id, existing_messages
    )

    try:
        _dynamo_put(session)
    except Exception:
        # Fall back to in-memory
        _in_memory[session_id] = session

    return session


def append_message(session_id: str, user_id: str, role: str, content: str) -> None:
    msg = {
        "id": str(uuid.uuid4()),
        "role": role,
        "content": content,
        "timestamp": _now_iso(),
    }

    # Try DynamoDB path: read → modify → write
    try:
        session = _dynamo_get(session_id, user_id)
        if session is None:
            session = _build_session(session_id, user_id, "", session_id)
        msgs: list[dict] = session.get("messages", [])
        msgs.append(msg)
        session["messages"] = msgs
        session["messageCount"] = len(msgs)
        session["updatedAt"] = _now_iso()
        _dynamo_put(session)
        # Mirror update in in-memory so get_user_sessions works offline
        _in_memory[session_id] = session
        return
    except Exception:
        pass

    # In-memory fallback
    if session_id not in _in_memory:
        _in_memory[session_id] = _build_session(session_id, user_id, "", session_id)
    session = _in_memory[session_id]
    msgs = session.setdefault("messages", [])
    msgs.append(msg)
    session["messageCount"] = len(msgs)
    session["updatedAt"] = _now_iso()


def get_user_sessions(user_id: str) -> list[dict]:
    try:
        rows = _dynamo_list_user(user_id)
        if rows:
            rows.sort(key=lambda s: s.get("updatedAt", ""), reverse=True)
            return rows
    except Exception:
        pass

    # In-memory fallback
    sessions = [s for s in _in_memory.values() if s.get("userId") == user_id]
    sessions.sort(key=lambda s: s.get("updatedAt", ""), reverse=True)
    return sessions


def get_session(session_id: str, user_id: str) -> dict | None:
    try:
        session = _dynamo_get(session_id, user_id)
        if session is not None:
            return session
    except Exception:
        pass

    session = _in_memory.get(session_id)
    if session and session.get("userId") == user_id:
        return session
    return None


def delete_session(session_id: str, user_id: str) -> None:
    try:
        _dynamo_delete(session_id, user_id)
    except Exception:
        pass

    _in_memory.pop(session_id, None)

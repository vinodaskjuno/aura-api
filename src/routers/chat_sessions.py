"""Chat session CRUD router — /api/chat/sessions."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.services import chat_history

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ── Request models ────────────────────────────────────────────────────────────

class UpsertSessionRequest(BaseModel):
    sessionId: str
    projectName: str = ""
    projectId: str = ""
    sessionName: str = ""


class AppendMessageRequest(BaseModel):
    role: str
    content: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/sessions")
def list_sessions(user: dict = Depends(get_current_user)):
    try:
        sessions = chat_history.get_user_sessions(user["userId"])
        # Strip messages from list response — load them individually via GET /sessions/{id}
        return [{k: v for k, v in s.items() if k != "messages"} for s in sessions]
    except Exception:
        return []


@router.post("/sessions")
def upsert_session(body: UpsertSessionRequest, user: dict = Depends(get_current_user)):
    try:
        return chat_history.create_or_update_session(
            session_id=body.sessionId,
            user_id=user["userId"],
            project_name=body.projectName,
            session_name=body.sessionName or body.sessionId,
            project_id=body.projectId,
        )
    except Exception:
        # Return a minimal session dict so the client is never left with nothing
        now = datetime.now(timezone.utc).isoformat()
        return {
            "sessionId": body.sessionId,
            "userId": user["userId"],
            "projectName": body.projectName,
            "projectId": body.projectId,
            "sessionName": body.sessionName or body.sessionId,
            "createdAt": now,
            "updatedAt": now,
            "messageCount": 0,
            "messages": [],
        }


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user: dict = Depends(get_current_user)):
    try:
        session = chat_history.get_session(session_id, user["userId"])
    except Exception:
        session = None
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/sessions/{session_id}/messages")
def append_message(
    session_id: str,
    body: AppendMessageRequest,
    user: dict = Depends(get_current_user),
):
    try:
        chat_history.append_message(session_id, user["userId"], body.role, body.content)
        return {"ok": True}
    except Exception:
        return {"ok": False}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    try:
        chat_history.delete_session(session_id, user["userId"])
    except Exception:
        pass
    return {"ok": True}


@router.post("/sessions/{session_id}/summarize")
def summarize_session(session_id: str, user: dict = Depends(get_current_user)):
    """Generate a 150-word summary of a chat session using the configured LLM."""
    from src.config_settings import get_settings

    try:
        session = chat_history.get_session(session_id, user["userId"])
    except Exception:
        session = None
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = session.get("messages", [])
    if not messages:
        return {"summary": "No messages in this session."}

    # Build conversation text for the prompt
    conversation_lines = []
    for msg in messages:
        role = msg.get("role", "user").capitalize()
        content = msg.get("content", "")
        conversation_lines.append(f"{role}: {content}")
    conversation_text = "\n".join(conversation_lines)

    prompt = (
        "Summarize this conversation in 150 words, focusing on key technical topics "
        "discussed and decisions made:\n\n" + conversation_text
    )

    settings = get_settings()

    if settings.llm_backend == "anthropic" and settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            response = client.messages.create(
                model=settings.anthropic_model_id,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = response.content[0].text if response.content else ""
            return {"summary": summary}
        except Exception as exc:
            log.warning("Anthropic summarize failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"LLM error: {exc}")
    else:
        # Bedrock fallback via boto3 converse
        try:
            import boto3
            client = boto3.client("bedrock-runtime", region_name=settings.bedrock_region)
            response = client.converse(
                modelId=settings.bedrock_model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 300},
            )
            output = response.get("output", {})
            text = output.get("message", {}).get("content", [{}])[0].get("text", "")
            return {"summary": text}
        except Exception as exc:
            log.warning("Bedrock summarize failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"LLM error: {exc}")


@router.get("/stats")
def chat_stats(user: dict = Depends(get_current_user)):
    try:
        sessions = chat_history.get_user_sessions(user["userId"])
    except Exception:
        sessions = []

    today = datetime.now(timezone.utc).date().isoformat()
    today_count = sum(
        1 for s in sessions if (s.get("createdAt") or "").startswith(today)
    )
    total_messages = sum(s.get("messageCount", 0) for s in sessions)

    return {
        "totalSessions": len(sessions),
        "todaySessions": today_count,
        "totalMessages": total_messages,
    }

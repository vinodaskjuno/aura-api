"""Orchestrator API — WebSocket /ws/orchestrate + REST /api/orchestrate."""
from __future__ import annotations
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException
from pydantic import BaseModel

from src.routers.auth import get_current_user
from src.orchestrator.executor import run_orchestration
from src.orchestrator.agent_registry import all_agents

logger = logging.getLogger(__name__)
router = APIRouter(tags=["orchestrator"])


# ── REST trigger (fire-and-forget, returns run_id) ────────────────────────────

class OrchestrateRequest(BaseModel):
    message: str
    project_id: str | None = None
    session_id: str | None = None


@router.post("/api/orchestrate")
async def orchestrate_rest(req: OrchestrateRequest, user: dict = Depends(get_current_user)):
    """Non-streaming trigger. Returns immediately with agent list; use WS for progress."""
    from src.orchestrator.intent_classifier import classify
    classification = classify(req.message)
    return {
        "agents": classification.get("agents", []),
        "intentSummary": classification.get("intent_summary", ""),
        "hint": "Connect to /ws/orchestrate for real-time progress",
    }


@router.get("/api/orchestrate/agents")
def list_agents(user: dict = Depends(get_current_user)):
    """List all registered agents and their descriptions."""
    return [
        {"name": name, "description": agent.description}
        for name, agent in all_agents().items()
    ]


# ── WebSocket streaming orchestrator ─────────────────────────────────────────

@router.websocket("/ws/orchestrate")
async def ws_orchestrate(ws: WebSocket):
    """
    WebSocket endpoint for real-time orchestration.

    Client sends:
      {"type": "run", "message": "...", "projectId": "...", "sessionId": "...", "token": "..."}

    Server sends stream of events:
      orchestration_start | intent_classified | agent_start | agent_done |
      orchestration_done | status | error
    """
    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
                continue

            if msg_type == "run":
                # Validate token
                from src.services.auth_service import verify_token
                token = data.get("token", "")
                user = verify_token(token)
                if not user:
                    await ws.send_json({"type": "error", "message": "Unauthorized"})
                    continue

                await run_orchestration(
                    ws=ws,
                    user_message=data.get("message", ""),
                    user_id=user.get("userId", ""),
                    username=user.get("username", ""),
                    role=user.get("role", ""),
                    session_id=data.get("sessionId", "default"),
                    project_id=data.get("projectId"),
                )
            else:
                await ws.send_json({"type": "error", "message": f"Unknown type: {msg_type}"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("ws_orchestrate error")
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass

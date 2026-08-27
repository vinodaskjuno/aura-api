import logging

from fastapi import APIRouter, WebSocket, Query

from src.services.auth_service import verify_token
from src.services.advisor.react_orchestrator import run_advisor
from src.services import chat_history
from src.config_settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["advisor"])


@router.websocket("/ws/advisor")
async def advisor_ws(websocket: WebSocket, token: str = Query(...)):
    user = verify_token(token)
    if not user:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()

            # Frontend sends { type, sessionId, text, context? }
            # Support both camelCase (frontend) and snake_case (legacy) key names
            session_id = (
                data.get("sessionId")
                or data.get("session_id")
                or f"session-{user['username']}"
            )
            content = data.get("text") or data.get("content", "")
            context = data.get("context")  # optional: { project, nodes, links }

            if content:
                model_override = data.get("model", "")
                project_id = data.get("projectId", "")
                user_id = user.get("userId", "")
                # Use explicit projectName field first; fall back to context.project
                project_name = (
                    data.get("projectName")
                    or (context.get("project") if context else "")
                    or ""
                )

                # Check budget and potentially switch tier before invoking LLM
                settings = get_settings()
                try:
                    from src.routers.budget import check_and_enforce_budget
                    model_override, budget_events = await check_and_enforce_budget(
                        user_id, model_override or settings.bedrock_model_id, settings
                    )
                    for ev in budget_events:
                        await websocket.send_json(ev)
                except Exception:
                    pass  # budget enforcement is non-critical

                assistant_text = await run_advisor(
                    websocket,
                    content,
                    session_id,
                    settings,
                    context=context,
                    user_id=user_id,
                    project_id=project_id,
                    model_override=model_override,
                )
                # Persist session metadata plus both the user and assistant messages
                try:
                    chat_history.create_or_update_session(
                        session_id,
                        user_id,
                        project_name,
                        session_name=project_name or session_id,
                        project_id=project_id,
                    )
                    chat_history.append_message(session_id, user_id, "user", content)
                    if assistant_text:
                        chat_history.append_message(session_id, user_id, "assistant", assistant_text)
                except Exception:
                    pass  # chat history is non-critical — never break the advisor
    except Exception as exc:
        logger.debug("advisor_ws closed: %s", exc)

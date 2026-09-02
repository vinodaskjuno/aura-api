import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from src.services.advisor import bedrock_client, session_memory
from src.services.advisor import anthropic_client
from src.services.advisor import tools as ontology_tools
from src.services.advisor.streaming import (
    send_done,
    send_error,
    send_token,
    send_tool_call,
    send_tool_result,
    send_usage,
)

logger = logging.getLogger(__name__)

BASE_SYSTEM_PROMPT = (
    "You are the AURA Dev Chatbot — an AI assistant for software developers and architects. "
    "You help with architecture questions, code design, infrastructure, security, incident analysis, "
    "and development best practices. When a project's ontology context is provided, use it to give "
    "specific, grounded answers about that project's services, infrastructure, dependencies, and "
    "security posture. Be concise, practical, and direct."
)

SYSTEM_PROMPT = BASE_SYSTEM_PROMPT  # kept for backward-compat

TOOLS_DEF: list[dict] = []

_TOOL_DISPATCH: dict[str, Any] = {}

# Git file-system tools — injected into TOOLS_DEF only when a project has a cloned repo
GIT_TOOLS_DEF: list[dict] = [
    {
        "name": "list_files",
        "description": (
            "List all files in the cloned git repository for the current project. "
            "Use this to explore the codebase before reading or editing files. "
            "Pass a sub-directory to narrow the listing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Relative path within the repo to list. Defaults to '.' (repo root).",
                }
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "Read the full contents of a file from the cloned git repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Repo-relative path, e.g. 'src/main.py' or 'README.md'.",
                }
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write or overwrite a file in the cloned git repository. "
            "Creates any missing parent directories automatically. "
            "Always provide the complete file content — partial writes are not supported."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Repo-relative path where the file should be written.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete content to write to the file.",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "get_diff",
        "description": (
            "Show all uncommitted changes in the cloned repository as a unified diff. "
            "Use after writing files to confirm what has changed before asking the user to review."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _call_tool(name: str, tool_input: dict, dispatch: dict[str, Any] | None = None) -> dict:
    fn = (dispatch or _TOOL_DISPATCH).get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**tool_input)
    except Exception as exc:
        logger.exception("Tool %s raised an exception", name)
        return {"error": str(exc)}


def _build_system_prompt(context: dict | None) -> str:
    """Build a system prompt that includes project-specific ontology context."""
    if not context:
        return BASE_SYSTEM_PROMPT

    project = context.get("project", "")
    nodes: list[dict] = context.get("nodes", [])
    links: list[dict] = context.get("links", [])

    # Summarise nodes by type
    type_counts: dict[str, list[str]] = {}
    for n in nodes:
        ntype = (n.get("node_type") or n.get("type") or "unknown").lower()
        label = n.get("label") or n.get("name") or n.get("id") or ""
        type_counts.setdefault(ntype, []).append(label)

    node_summary_lines = []
    for ntype, labels in sorted(type_counts.items()):
        sample = ", ".join(labels[:8])
        if len(labels) > 8:
            sample += f", … +{len(labels) - 8} more"
        node_summary_lines.append(f"  • {ntype}: {sample}")

    # Summarise relationships
    rel_counts: dict[str, int] = {}
    for l in links:
        rel = (l.get("type") or l.get("relationship") or "REL")
        rel_counts[rel] = rel_counts.get(rel, 0) + 1
    rel_summary = ", ".join(f"{k}×{v}" for k, v in sorted(rel_counts.items()))

    context_block = (
        f"\n\n--- PROJECT CONTEXT: {project} ---\n"
        f"Ontology graph contains {len(nodes)} nodes and {len(links)} relationships.\n"
        f"Node breakdown:\n" + "\n".join(node_summary_lines) +
        f"\nRelationship types: {rel_summary or 'none'}\n"
        "Use this context to give project-specific, accurate answers.\n"
        "---"
    )

    return BASE_SYSTEM_PROMPT + context_block


def _persist_token_usage(
    user_id: str, session_id: str, project_id: str,
    model: str, input_tokens: int, output_tokens: int, cost: float,
) -> None:
    """Write a token-usage record to DynamoDB. Non-blocking best-effort."""
    try:
        import uuid
        from datetime import datetime, timezone
        from src.database import dynamo_client as db
        now = datetime.now(timezone.utc).isoformat()
        db.put_item("token-usage", {
            "userId": user_id,
            "sortKey": f"{now}#{uuid.uuid4().hex[:8]}",
            "sessionId": session_id,
            "projectId": project_id,
            "model": model,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cost": str(cost),
            "timestamp": now,
        })
    except Exception as exc:
        logger.warning("Failed to persist token usage: %s", exc)


def _resolve_model(settings: Any, model_override: str) -> tuple[bool, str]:
    """Return (use_anthropic, model_id) based on override and settings."""
    if model_override:
        # Bedrock model IDs start with 'us.anthropic.' or 'amazon.'
        if model_override.startswith("us.anthropic.") or model_override.startswith("amazon."):
            return False, model_override
        # Anthropic direct model IDs (e.g. 'claude-sonnet-4-5')
        if getattr(settings, "anthropic_api_key", ""):
            return True, model_override
    # Fall back to settings
    use_anthropic = (
        getattr(settings, "llm_backend", "bedrock") == "anthropic"
        and getattr(settings, "anthropic_api_key", "")
    )
    return use_anthropic, (settings.anthropic_model_id if use_anthropic else settings.bedrock_model_id)


async def run_advisor(
    ws: WebSocket,
    user_message: str,
    session_id: str,
    settings: Any,
    context: dict | None = None,
    user_id: str = "",
    project_id: str = "",
    model_override: str = "",
) -> str:
    try:
        system_prompt = _build_system_prompt(context)
        session_memory.append_turn(session_id, "user", user_message)
        history = session_memory.get_history(session_id)

        # Build Anthropic-format messages list from history
        messages: list[dict] = []
        for turn in history:
            messages.append({"role": turn["role"], "content": turn["content"]})

        accumulated_text = ""
        total_input_tokens = 0
        total_output_tokens = 0
        MAX_ITERATIONS = 10

        use_anthropic, actual_model = _resolve_model(settings, model_override)

        # Inject git file tools when the project has a cloned repo
        effective_tools_def = list(TOOLS_DEF)
        effective_tool_dispatch: dict[str, Any] = dict(_TOOL_DISPATCH)
        if project_id:
            try:
                ontology_tools._resolve_project_dir(project_id)
                effective_tools_def = effective_tools_def + GIT_TOOLS_DEF
                _pid = project_id  # capture for lambdas
                effective_tool_dispatch.update({
                    "list_files": lambda directory=".": ontology_tools.list_files(_pid, directory),
                    "read_file": lambda file_path: ontology_tools.read_file(_pid, file_path),
                    "write_file": lambda file_path, content: ontology_tools.write_file(_pid, file_path, content),
                    "get_diff": lambda: ontology_tools.get_diff(_pid),
                })
                system_prompt += (
                    "\n\nYou also have access to git file tools for this project's cloned repository: "
                    "list_files, read_file, write_file, get_diff. "
                    "Use them to autonomously read and edit code based on user requests. "
                    "After making file changes, always call get_diff so the user can review what changed."
                )
            except FileNotFoundError:
                pass  # no cloned repo — don't add git tools

        # Inject the user's MCP tools, exactly as the git block above injects its own:
        # per-request, never mutating the module-level TOOLS_DEF/_TOOL_DISPATCH.
        #
        # Fail-closed on user_id. MCP servers are per-user connector rows, so a missing
        # identity means no MCP tools rather than someone else's.
        mcp_names: list[str] = []
        if user_id:
            try:
                from src.mcp_client import bundle_for_user
                reserved = {t["name"] for t in effective_tools_def}
                bundle = await bundle_for_user(user_id, reserved)
                if bundle.defs:
                    effective_tools_def = effective_tools_def + bundle.defs
                    effective_tool_dispatch.update(bundle.dispatch)
                    mcp_names = [s.name for s in bundle.servers.values()]
                    system_prompt += (
                        "\n\nYou also have tools from the user's connected MCP servers "
                        f"({', '.join(mcp_names)}). Their names all begin with `mcp__`. "
                        "Use them whenever the question concerns data those systems hold, "
                        "and say which server an answer came from."
                    )
                if bundle.degraded:
                    logger.warning("MCP servers unreachable for %s: %s",
                                   user_id, ", ".join(bundle.degraded))
            except Exception as exc:
                # Discovery must never be able to break a chat turn. Worst case the
                # user gets the built-in tools and nothing else.
                logger.warning("MCP tool discovery skipped: %s", exc)

        for iteration in range(MAX_ITERATIONS):
            if use_anthropic:
                events = list(
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: list(
                            anthropic_client.invoke_streaming(
                                messages,
                                system_prompt,
                                effective_tools_def,
                                actual_model,
                                settings.anthropic_api_key,
                            )
                        ),
                    )
                )
            else:
                events = list(
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: list(
                            bedrock_client.invoke_streaming(
                                messages,
                                system_prompt,
                                effective_tools_def,
                                actual_model,
                                settings.aws_region,
                            )
                        ),
                    )
                )

            # Separate text, tool_use, usage, and end events
            text_parts: list[str] = []
            tool_uses: list[dict] = []
            stop_reason = "end_turn"

            for event in events:
                etype = event.get("type")
                if etype == "text":
                    content = event["content"]
                    text_parts.append(content)
                    accumulated_text += content
                    await send_token(ws, content)
                elif etype == "usage":
                    total_input_tokens += event.get("input", 0)
                    total_output_tokens += event.get("output", 0)
                elif etype == "tool_use":
                    tool_uses.append(event)
                elif etype == "end":
                    stop_reason = event.get("stop_reason", "end_turn")
                elif etype == "error":
                    await send_error(ws, event.get("message", "Bedrock error"))
                    return ""

            # Build the assistant message content block
            assistant_content: list[dict] = []
            if text_parts:
                assistant_content.append({"type": "text", "text": "".join(text_parts)})
            for tu in tool_uses:
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": tu["id"],
                        "name": tu["name"],
                        "input": tu["input"],
                    }
                )

            if assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})

            if not tool_uses:
                # No tool calls — we're done
                break

            # Dispatch tool calls and collect results
            tool_result_contents: list[dict] = []
            for tu in tool_uses:
                name = tu["name"]
                tool_input = tu["input"]
                tool_id = tu["id"]

                await send_tool_call(ws, name, tool_input)
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda n=name, i=tool_input: _call_tool(n, i, effective_tool_dispatch)
                )
                await send_tool_result(ws, name, result)

                import json as _json
                tool_result_contents.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": _json.dumps(result),
                    }
                )

            messages.append({"role": "user", "content": tool_result_contents})

            if stop_reason not in ("tool_use",):
                break

        session_memory.append_turn(session_id, "assistant", accumulated_text)

        # Emit aggregated usage to the frontend and persist server-side
        if total_input_tokens or total_output_tokens:
            from src.config_settings import calculate_cost
            cost = calculate_cost(actual_model, total_input_tokens, total_output_tokens)
            await send_usage(ws, total_input_tokens, total_output_tokens, cost)
            if user_id:
                _persist_token_usage(
                    user_id, session_id, project_id,
                    actual_model, total_input_tokens, total_output_tokens, cost,
                )
                try:
                    from src.routers.budget import update_user_budget
                    update_user_budget(user_id, actual_model, cost)
                except Exception as exc:
                    logger.warning("update_user_budget failed: %s", exc)

        await send_done(ws, session_id)
        return accumulated_text

    except Exception as exc:
        logger.exception("run_advisor failed for session %s", session_id)
        await send_error(ws, f"Advisor error: {exc}")
        return ""

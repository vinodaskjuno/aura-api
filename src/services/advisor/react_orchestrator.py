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
            # Without these two, a DevMate turn is indistinguishable from a row
            # whose origin was never recorded: `get_tool_breakdown` files it
            # under "other", and no query can say what the advisor costs. Every
            # other producer tags itself (gateway, claude-code); this one did
            # not. Rows written before this line stay untagged forever, so any
            # DevMate spend metric is "since tagging began" and must say so.
            "source": "dev-mate",
            "tool": "dev-mate",
        })
    except Exception as exc:
        logger.warning("Failed to persist token usage: %s", exc)


#: Opik project for DevMate's own turns. Mirrors the routing `_opik_project_for` does
#: in observability/llm.py (obs_/judge_ -> aura-observability, qa_ -> aura-qualitymind):
#: DevMate had no entry there because it never emitted a span at all, so the AI Traces
#: page was blind to the one surface most people use.
AIOBS_PROJECT = "aura-devmate"

#: Spans carry previews, not transcripts. The trace store already offloads anything
#: larger, and a DevMate turn can hold a whole file the agent just read.
_SPAN_PREVIEW_CHARS = 2_000


def _preview(value: Any, limit: int = _SPAN_PREVIEW_CHARS) -> str:
    """A bounded, single-string rendering of anything a span wants to carry."""
    if value is None:
        return ""
    if not isinstance(value, str):
        import json as _json
        try:
            value = _json.dumps(value, default=str)
        except Exception:                                     # noqa: BLE001
            value = str(value)
    return value[:limit]


def _emit_turn_span(
    *, trace_id: str, agent: str, model: str, provider: str,
    prompt: str, completion: str, input_tokens: int, output_tokens: int,
    cost_usd: float, latency_ms: int, thread_id: str, error: str = "",
    metadata: dict | None = None,
) -> dict:
    """One LLM iteration as an Opik span. Returns {"traceId", "spanId"}, possibly empty.

    Best-effort to the point of silence: tracing a chat turn must never be able to
    break the chat turn. The caller threads the returned traceId into the next
    iteration so a multi-step ReAct turn is ONE trace with one span per model call,
    rather than N unrelated traces the Threads tab cannot group.
    """
    try:
        from src.aiobs import opik_client
        ref = opik_client.emit_llm_span(
            project=AIOBS_PROJECT, agent=agent, model=model, provider=provider,
            masked_input=prompt, masked_output=completion,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=cost_usd, latency_ms=latency_ms,
            trace_id=trace_id, thread_id=thread_id, error=error,
            metadata=metadata or {},
        )
        return ref or {}
    except Exception as exc:                                  # noqa: BLE001
        logger.debug("advisor span emission skipped: %s", exc)
        return {}


def _emit_tool_span(*, trace_id: str, parent_span_id: str, name: str,
                    tool_input: Any, result: Any, latency_ms: int) -> None:
    """One tool call as a child span. Silent on every failure, same as above."""
    if not trace_id:
        return
    try:
        from src.aiobs import opik_client
        error = ""
        if isinstance(result, dict) and result.get("error"):
            error = str(result["error"])
        opik_client.emit_span(
            project=AIOBS_PROJECT, trace_id=trace_id, name=name, kind="tool",
            masked_input=_preview(tool_input), masked_output=_preview(result),
            parent_span_id=parent_span_id, latency_ms=latency_ms, error=error,
        )
    except Exception as exc:                                  # noqa: BLE001
        logger.debug("advisor tool span skipped: %s", exc)


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
    actor: str = "",
) -> str:
    # A DevMate turn is the only pipeline in the product that produced no run
    # record: PIPELINE_DEV_MATE existed but the sole writer was the Reverse
    # Engineering "Analyse" button, so `pipeline='dev-mate'` had zero rows and
    # the delivery board's "Mapped" column was unreachable through runs.
    #
    # `actor` is the username, and it is NOT the same as actorId. `_open_run_record`
    # falls back to `actor or "system"`, so passing only actorId — which is what this
    # call did — filed every DevMate turn in the Lineage feed under "system". The
    # identity column there renders `actor`, so the human who typed the message was
    # the one thing the run did not record.
    from src.graph import provenance
    run = provenance.trace_run(
        provenance.PIPELINE_DEV_MATE,
        trigger=provenance.TRIGGER_MANUAL,   # a signed-in human asked for it
        actor=actor,
        actorId=user_id,
        projectId=project_id,
        sessionId=session_id,
        writtenBy="advisor.run_advisor",
        source="dev-chat",
    )
    with run as ctx:
        return await _run_advisor_traced(
            ws, user_message, session_id, settings, context,
            user_id, project_id, model_override, ctx,
        )


async def _run_advisor_traced(
    ws: WebSocket,
    user_message: str,
    session_id: str,
    settings: Any,
    context: dict | None = None,
    user_id: str = "",
    project_id: str = "",
    model_override: str = "",
    ctx: Any = None,
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
                    # session/user threaded through so the staged proposal can
                    # say who asked for it — the REST worker that later applies
                    # or discards it knows only the project and the path.
                    "write_file": lambda file_path, content: ontology_tools.write_file(
                        _pid, file_path, content, session_id, user_id),
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

        # One Opik trace for the whole turn, threaded across ReAct iterations. Empty
        # until the first span lands; `_emit_turn_span` mints it and every later call
        # passes it back so the iterations group under one trace instead of scattering.
        import time as _time
        aiobs_trace_id = ""
        turn_started = _time.monotonic()

        for iteration in range(MAX_ITERATIONS):
            iter_started = _time.monotonic()
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

            iter_latency_ms = int((_time.monotonic() - iter_started) * 1000)

            # Separate text, tool_use, usage, and end events
            text_parts: list[str] = []
            tool_uses: list[dict] = []
            stop_reason = "end_turn"
            iter_input_tokens = 0
            iter_output_tokens = 0

            for event in events:
                etype = event.get("type")
                if etype == "text":
                    content = event["content"]
                    text_parts.append(content)
                    accumulated_text += content
                    await send_token(ws, content)
                elif etype == "usage":
                    iter_input_tokens += event.get("input", 0)
                    iter_output_tokens += event.get("output", 0)
                    total_input_tokens += event.get("input", 0)
                    total_output_tokens += event.get("output", 0)
                elif etype == "tool_use":
                    tool_uses.append(event)
                elif etype == "end":
                    stop_reason = event.get("stop_reason", "end_turn")
                elif etype == "error":
                    message = event.get("message", "Bedrock error")
                    # Record the failed iteration before bailing out, so an errored
                    # turn is visible on the Traces tab rather than simply absent.
                    if ctx is not None:
                        ctx.fail(f"model error: {message}")
                    _emit_turn_span(
                        trace_id=aiobs_trace_id, agent="dev-mate", model=actual_model,
                        provider="anthropic" if use_anthropic else "bedrock",
                        prompt=_preview(user_message), completion="",
                        input_tokens=iter_input_tokens, output_tokens=iter_output_tokens,
                        cost_usd=0.0, latency_ms=iter_latency_ms,
                        thread_id=session_id, error=str(message),
                        metadata={"iteration": iteration, "projectId": project_id,
                                  "stopReason": "error"},
                    )
                    await send_error(ws, message)
                    return ""

            # This iteration's span. Cost is deliberately left at 0 here and reported
            # once for the whole turn below: `calculate_cost` is the billing figure and
            # splitting it per iteration would let the parts disagree with the total.
            from src.config_settings import calculate_cost as _calc
            iter_ref = _emit_turn_span(
                trace_id=aiobs_trace_id, agent="dev-mate", model=actual_model,
                provider="anthropic" if use_anthropic else "bedrock",
                prompt=_preview(user_message if iteration == 0 else "(tool results)"),
                completion=_preview("".join(text_parts)),
                input_tokens=iter_input_tokens, output_tokens=iter_output_tokens,
                cost_usd=_calc(actual_model, iter_input_tokens, iter_output_tokens),
                latency_ms=iter_latency_ms, thread_id=session_id,
                metadata={"iteration": iteration, "projectId": project_id,
                          "sessionId": session_id, "stopReason": stop_reason,
                          "toolCalls": [t["name"] for t in tool_uses]},
            )
            aiobs_trace_id = iter_ref.get("traceId", "") or aiobs_trace_id
            iter_span_id = iter_ref.get("spanId", "")

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
                tool_started = _time.monotonic()
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda n=name, i=tool_input: _call_tool(n, i, effective_tool_dispatch)
                )
                _emit_tool_span(
                    trace_id=aiobs_trace_id, parent_span_id=iter_span_id, name=name,
                    tool_input=tool_input, result=result,
                    latency_ms=int((_time.monotonic() - tool_started) * 1000),
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

        # The run context was opened, yielded, passed in as a parameter and then never
        # touched — so every DevMate run closed with all-zero stats, described nothing,
        # and pointed at nothing. `TraceContext` is frozen, so the summary goes onto the
        # run record itself; this is also the link that lets the Traces tab and the
        # Lineage feed be talking about the same turn.
        if ctx is not None and getattr(ctx, "runId", ""):
            try:
                from src.services import ontology_version_service as _versions
                _versions.annotate_version_record(
                    ctx.runId,
                    aiobsTraceId=aiobs_trace_id,
                    aiobsProject=AIOBS_PROJECT if aiobs_trace_id else "",
                    modelCalls=iteration + 1,
                    totalTokens=total_input_tokens + total_output_tokens,
                    durationMs=int((_time.monotonic() - turn_started) * 1000),
                )
            except Exception as exc:                          # noqa: BLE001
                logger.debug("run annotation skipped: %s", exc)

        await send_done(ws, session_id)
        return accumulated_text

    except Exception as exc:
        logger.exception("run_advisor failed for session %s", session_id)
        # Without this the turn fails, returns "", and the run record still reads
        # "success" — a half-failed run looking identical to a clean one is exactly
        # what `TraceContext.fail` exists to prevent.
        if ctx is not None:
            ctx.fail(f"{type(exc).__name__}: {exc}")
        await send_error(ws, f"Advisor error: {exc}")
        return ""

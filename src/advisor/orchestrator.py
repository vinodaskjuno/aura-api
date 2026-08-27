"""ReAct advisor loop: reason -> act (grounded tool) -> observe -> repeat.

Not a single prompt: the model iterates, calling tools until it can answer,
then finalizes. Streams tokens + tool lifecycle to the caller's emit() and
persists the full turn to encrypted memory (with sweep) for real recall.

Supports general coding tasks (read/write/analyse files, run tests, generate
docs, search the web, etc.) via the unified tool registry in src/tools/.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from .bedrock import BedrockClient, BedrockError
from .memory import SessionMemory
from ..tools.registry import TOOL_SPECS, execute_tool

_MAX_ITERS = 12  # increased from 8 to handle multi-tool coding flows

_SYSTEM_BASE = """You are AURA — an AI coding and architecture assistant for your cloud application universe.

You are a full-capability developer assistant. You can:
- **Analyse code**: read files, inspect project structure, run AST analysis, find symbol references
- **Generate code**: write new files, scaffold projects, create test files, produce boilerplate
- **Modify code**: edit files with surgical precision, apply patches, insert lines, rename symbols
- **Run quality checks**: execute tests (pytest/jest), lint (mypy/pylint/eslint), check coverage
- **Document**: generate docstrings, READMEs, and API reference docs from code
- **Research**: search the web for docs, fetch URLs, look up error messages
- **Git operations**: inspect diffs, view commit history, trace blame

## Hard rules
- Always use tools to ground your answers. Never invent file contents, test results, or facts.
- For code tasks: use list_directory first to understand the project, then read_file, then act.
- When writing or editing code, write the complete, working implementation — no placeholders or TODO stubs.
- If a file write succeeds, confirm the path and char count. If it fails, explain the error clearly.
- For shell/test output: surface the actual stdout/stderr, don't paraphrase it.
- Security: never attempt to access paths outside the workspace root.

Be concise, practical, and direct. Use markdown. Cite file paths and line numbers when relevant."""


class Advisor:
    def __init__(self, session_id: str):
        self.bedrock = BedrockClient()
        self.memory = SessionMemory(session_id, summarizer=self._summarize)

    def _summarize(self, transcript: str) -> str:
        try:
            parts = []
            msgs = [{"role": "user", "content": [{"text":
                     "Summarize this conversation excerpt in 2-3 sentences, keeping any "
                     "component names, file paths, counts, and decisions:\n\n" + transcript[:6000]}]}]
            for ev in self.bedrock.converse_stream(msgs, "You write terse factual summaries.",
                                                    tools=None, max_tokens=300):
                if ev["type"] == "token":
                    parts.append(ev["text"])
            return "".join(parts).strip() or transcript[:800]
        except Exception:
            return transcript[:800]

    def run(self, user_message: str, emit: Callable[[dict], None],
            abort: Optional[threading.Event] = None,
            workspace_root: str = "") -> str:
        self.memory.note_question(user_message)
        system_text = _SYSTEM_BASE
        if workspace_root:
            system_text += f"\n\n**Workspace root:** `{workspace_root}`"
        mem_ctx = self.memory.get_context_summary(user_message)
        if mem_ctx:
            system_text += "\n\n" + mem_ctx

        messages = list(self.memory.messages)
        messages.append({"role": "user", "content": [{"text": user_message}]})

        final_text = ""
        try:
            for _ in range(_MAX_ITERS):
                if abort and abort.is_set():
                    emit({"type": "aborted"})
                    break

                assistant_text = ""
                tool_uses = []
                stop_reason = "end_turn"
                for ev in self.bedrock.converse_stream(messages, system_text, TOOL_SPECS, abort):
                    et = ev["type"]
                    if et == "token":
                        assistant_text += ev["text"]
                        emit({"type": "token", "text": ev["text"]})
                    elif et == "tool_use":
                        tool_uses.append(ev)
                    elif et == "usage":
                        emit({"type": "usage", "input": ev.get("input"), "output": ev.get("output")})
                    elif et == "stop":
                        stop_reason = ev["reason"]

                # Record the assistant turn (text + any toolUse blocks)
                assistant_content = []
                if assistant_text:
                    assistant_content.append({"text": assistant_text})
                for tu in tool_uses:
                    assistant_content.append({"toolUse": {
                        "toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}})
                messages.append({"role": "assistant", "content": assistant_content or [{"text": ""}]})

                if not tool_uses or stop_reason in ("end_turn", "aborted"):
                    final_text = assistant_text
                    break

                # Execute tools and feed results back
                tool_results = []
                for tu in tool_uses:
                    emit({"type": "tool_start", "name": tu["name"], "input": tu["input"]})
                    result = execute_tool(tu["name"], tu["input"], workspace_root)
                    emit({"type": "tool_end", "name": tu["name"]})
                    tool_results.append({"toolResult": {
                        "toolUseId": tu["id"], "content": [{"text": result}]}})
                messages.append({"role": "user", "content": tool_results})

        except BedrockError as e:
            msg = str(e)
            hint = ""
            if "credentials" in msg.lower() or "token" in msg.lower() or "auth" in msg.lower():
                hint = " → Check ~/.aws/credentials and AWS_PROFILE in src/config/dev.env."
            elif "timeout" in msg.lower() or "timed out" in msg.lower():
                hint = " → Bedrock timed out. Check your AWS region and model access."
            elif "access" in msg.lower() or "model" in msg.lower():
                hint = f" → Model '{self.bedrock.model_id}' may not be enabled in your account/region."
            emit({"type": "error", "message": f"Bedrock error: {msg}{hint}"})
            return ""
        except Exception as e:
            emit({"type": "error", "message": f"Advisor error: {e}"})
            return ""

        # Persist turn + sweep + save
        self.memory.messages = messages
        self.memory.maybe_sweep()
        self.memory.save()
        emit({"type": "done", "text": final_text})
        return final_text

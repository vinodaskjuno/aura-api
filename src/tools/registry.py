"""Master tool registry — composes all tool modules into a single dispatch table.

Tool spec format matches the Bedrock Converse API:
  {"toolSpec": {"name": str, "description": str, "inputSchema": {"json": {...}}}}

execute_tool() is the single entry point for the ReAct loop.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .fs_tools import (
    t_read_file, t_write_file, t_edit_file,
    t_list_directory, t_apply_patch, t_insert_at_line,
)
from .shell_tools import (
    t_run_bash, t_run_tests, t_run_linter,
    t_git_diff, t_git_log, t_git_blame,
    t_install_package, t_list_packages,
)
from .analysis_tools import (
    t_analyze_code, t_find_references,
    t_rename_symbol, t_get_coverage,
)
from .doc_tools import (
    t_generate_docstring, t_generate_readme, t_generate_api_docs,
)
from .web_tools import t_web_search, t_fetch_url

_S = {"type": "string"}
_SI = {"type": "integer"}
_SB = {"type": "boolean"}

# ── Tier 1: Core Coding Loop ─────────────────────────────────────────────────

_TIER1: list[dict] = [
    {"toolSpec": {
        "name": "read_file",
        "description": (
            "Read the contents of a file in the workspace. "
            "Optionally limit to a line range (1-indexed). "
            "Returns the file text (capped at 20 000 chars)."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path":       {**_S, "description": "Workspace-relative or absolute file path"},
                "start_line": {**_SI, "description": "First line to read (1-indexed, inclusive)"},
                "end_line":   {**_SI, "description": "Last line to read (1-indexed, inclusive)"},
            },
            "required": ["path"]}}}},

    {"toolSpec": {
        "name": "write_file",
        "description": (
            "Create or overwrite a file in the workspace with the given content. "
            "Parent directories are created automatically. "
            "Returns a confirmation with the file path and character count."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path":    {**_S, "description": "Workspace-relative or absolute file path"},
                "content": {**_S, "description": "Full file content to write"},
            },
            "required": ["path", "content"]}}}},

    {"toolSpec": {
        "name": "edit_file",
        "description": (
            "Replace the FIRST occurrence of old_str with new_str inside an existing file. "
            "Returns an error if old_str is not found. "
            "Use for targeted, surgical edits without rewriting the whole file."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path":    {**_S, "description": "Workspace-relative or absolute file path"},
                "old_str": {**_S, "description": "Exact text to find (must be unique enough)"},
                "new_str": {**_S, "description": "Replacement text"},
            },
            "required": ["path", "old_str", "new_str"]}}}},

    {"toolSpec": {
        "name": "search_files",
        "description": (
            "Find files in the workspace. "
            "search_type='glob' matches filenames by pattern (e.g. '**/*.py'). "
            "search_type='content' searches file contents for a string/regex. "
            "Returns a list of matching paths (and matching lines for content search)."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "pattern":     {**_S, "description": "Glob pattern or content regex"},
                "search_type": {"type": "string", "enum": ["glob", "content"],
                                "description": "glob = filename match, content = text search"},
                "path":        {**_S, "description": "Sub-directory to search in (default: workspace root)"},
            },
            "required": ["pattern"]}}}},

    {"toolSpec": {
        "name": "run_bash",
        "description": (
            "Run a shell command in the workspace root directory. "
            "Returns stdout (capped 8 000 chars) and stderr (capped 2 000 chars). "
            "Use for builds, formatting, custom scripts, etc."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "command": {**_S, "description": "Shell command to execute"},
                "timeout": {**_SI, "description": "Timeout in seconds (default 30, max 120)"},
            },
            "required": ["command"]}}}},

    {"toolSpec": {
        "name": "list_directory",
        "description": (
            "List the directory tree of the workspace (or a sub-path). "
            "Returns a compact tree showing files, sizes, and directory structure. "
            "Use this first to understand project layout before reading files."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path":      {**_S,  "description": "Sub-path relative to workspace root (default: root)"},
                "max_depth": {**_SI, "description": "Max directory depth (default 3, max 6)"},
            }}}}},
]

# ── Tier 2: Quality & Safety ─────────────────────────────────────────────────

_TIER2: list[dict] = [
    {"toolSpec": {
        "name": "run_tests",
        "description": (
            "Run the test suite (pytest for Python, jest for JS/TS). "
            "Auto-detects the test runner. Returns test results with pass/fail counts."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path": {**_S,  "description": "File or directory to test (default: workspace root)"},
                "args": {**_S,  "description": "Extra CLI args (e.g. '-v -k test_name')"},
            }}}}},

    {"toolSpec": {
        "name": "run_linter",
        "description": (
            "Run a linter/type-checker on a file or directory. "
            "Auto-detects mypy/pylint (Python) or eslint (JS/TS) from project config. "
            "Returns structured error and warning list."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path":   {**_S, "description": "File or directory to lint"},
                "linter": {**_S, "description": "Force a specific linter: mypy, pylint, eslint (optional)"},
            },
            "required": ["path"]}}}},

    {"toolSpec": {
        "name": "git_diff",
        "description": (
            "Show uncommitted changes (git diff HEAD). "
            "Optionally filter to a specific file. "
            "Returns unified diff output (capped 10 000 chars)."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path": {**_S, "description": "File path to limit diff to (optional)"},
            }}}}},

    {"toolSpec": {
        "name": "git_log",
        "description": (
            "Show recent git commit history. "
            "Returns commit hash, author, date, and message for each commit."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "limit": {**_SI, "description": "Number of commits to show (default 10)"},
                "path":  {**_S,  "description": "Limit log to commits touching this file (optional)"},
            }}}}},

    {"toolSpec": {
        "name": "git_blame",
        "description": (
            "Show who last changed a specific line in a file (git blame). "
            "Returns author, date, and commit for that line."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path": {**_S,  "description": "File path"},
                "line": {**_SI, "description": "Line number (1-indexed)"},
            },
            "required": ["path", "line"]}}}},

    {"toolSpec": {
        "name": "apply_patch",
        "description": (
            "Apply a unified diff patch to a file. "
            "Safer than write_file for large files — only touches changed lines."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path":  {**_S, "description": "File path to patch"},
                "patch": {**_S, "description": "Unified diff content (--- a/... +++ b/... @@ ... @@)"},
            },
            "required": ["path", "patch"]}}}},
]

# ── Tier 3: Document Generation & Research ───────────────────────────────────

_TIER3: list[dict] = [
    {"toolSpec": {
        "name": "analyze_code",
        "description": (
            "Deep AST analysis of a Python file or directory. "
            "Returns classes, functions, imports, test coverage estimate, and dependency graph."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path": {**_S, "description": "File or directory path to analyze"},
            },
            "required": ["path"]}}}},

    {"toolSpec": {
        "name": "generate_docstring",
        "description": (
            "Extract the source of a function or class from a file and return it "
            "so you can write a proper docstring for it. "
            "Returns the raw source block — you then compose the docstring and use edit_file."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path":          {**_S, "description": "File path"},
                "function_name": {**_S, "description": "Name of the function or class"},
            },
            "required": ["path", "function_name"]}}}},

    {"toolSpec": {
        "name": "generate_readme",
        "description": (
            "Scan the workspace structure and generate a README.md skeleton. "
            "Writes the file and returns its path. "
            "You can refine the content with edit_file afterwards."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "output_path": {**_S, "description": "Where to write README (default: README.md in workspace root)"},
            }}}}},

    {"toolSpec": {
        "name": "generate_api_docs",
        "description": (
            "Parse FastAPI router decorators in the workspace and generate API documentation "
            "as a Markdown file listing all endpoints, methods, and paths."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "output_path": {**_S, "description": "Where to write docs (default: docs/API.md)"},
            }}}}},

    {"toolSpec": {
        "name": "web_search",
        "description": (
            "Search the web for documentation, error messages, or library usage. "
            "Returns top results with titles, URLs, and snippets."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "query": {**_S, "description": "Search query"},
            },
            "required": ["query"]}}}},

    {"toolSpec": {
        "name": "fetch_url",
        "description": (
            "Fetch the text content of a URL (docs page, API spec, GitHub issue). "
            "Returns cleaned text content capped at 10 000 chars."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "url": {**_S, "description": "URL to fetch"},
            },
            "required": ["url"]}}}},
]

# ── Tier 4: Advanced Refactoring ─────────────────────────────────────────────

_TIER4: list[dict] = [
    {"toolSpec": {
        "name": "find_references",
        "description": (
            "Find all occurrences of a symbol (function, class, variable) across the workspace. "
            "Returns file paths and line numbers of each usage."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "symbol": {**_S, "description": "Symbol name to search for"},
                "path":   {**_S, "description": "Sub-directory to limit search (optional)"},
            },
            "required": ["symbol"]}}}},

    {"toolSpec": {
        "name": "rename_symbol",
        "description": (
            "Rename a symbol across all files in the workspace (or a sub-path). "
            "Performs a safe whole-word grep-and-replace. "
            "Returns a list of files modified and the number of replacements in each."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "old_name": {**_S, "description": "Current symbol name"},
                "new_name": {**_S, "description": "New symbol name"},
                "path":     {**_S, "description": "Sub-directory to limit rename (optional)"},
            },
            "required": ["old_name", "new_name"]}}}},

    {"toolSpec": {
        "name": "insert_at_line",
        "description": (
            "Insert one or more lines of text at a specific line number in a file. "
            "Existing content from that line onward is shifted down."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path":    {**_S,  "description": "File path"},
                "line":    {**_SI, "description": "Line number to insert BEFORE (1-indexed)"},
                "content": {**_S,  "description": "Text to insert (may contain newlines)"},
            },
            "required": ["path", "line", "content"]}}}},

    {"toolSpec": {
        "name": "get_coverage",
        "description": (
            "Read the test coverage report for the workspace. "
            "Runs 'coverage report' or reads an existing coverage.xml. "
            "Returns per-file coverage percentages."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "path": {**_S, "description": "Limit report to a sub-path (optional)"},
            }}}}},

    {"toolSpec": {
        "name": "install_package",
        "description": (
            "Install a Python or Node.js package in the workspace. "
            "Detects pip vs npm from context, or specify manager explicitly."
        ),
        "inputSchema": {"json": {"type": "object",
            "properties": {
                "package": {**_S, "description": "Package name (e.g. 'requests' or 'lodash@4')"},
                "manager": {**_S, "description": "pip or npm (auto-detected if omitted)"},
            },
            "required": ["package"]}}}},

    {"toolSpec": {
        "name": "list_packages",
        "description": (
            "Read the project's dependency manifests: requirements.txt and/or package.json. "
            "Returns the dependency lists without running any installs."
        ),
        "inputSchema": {"json": {"type": "object", "properties": {}}}}},
]

# ── Master spec list (all tiers) ─────────────────────────────────────────────

TOOL_SPECS: list[dict] = _TIER1 + _TIER2 + _TIER3 + _TIER4

# ── Security guard ───────────────────────────────────────────────────────────

def _safe_path(raw: str, workspace_root: str) -> Path:
    """Resolve path and ensure it stays inside workspace_root.
    Raises ValueError on path-traversal attempts."""
    if not workspace_root:
        return Path(raw).resolve()
    root = Path(workspace_root).resolve()
    candidate = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Path '{raw}' resolves to '{candidate}' which is outside "
            f"workspace root '{root}'. Access denied."
        )
    return candidate

# ── Dispatch table ───────────────────────────────────────────────────────────

def _make_dispatch(workspace_root: str) -> dict:
    wr = workspace_root

    def sp(raw: str) -> Path:
        return _safe_path(raw, wr)

    return {
        # Tier 1
        "read_file":      lambda a: t_read_file(sp(a["path"]), a.get("start_line"), a.get("end_line")),
        "write_file":     lambda a: t_write_file(sp(a["path"]), a["content"]),
        "edit_file":      lambda a: t_edit_file(sp(a["path"]), a["old_str"], a["new_str"]),
        "search_files":   lambda a: t_search_files(a["pattern"], a.get("search_type", "glob"),
                                                    sp(a["path"]) if a.get("path") else Path(wr or ".")),
        "run_bash":       lambda a: t_run_bash(a["command"], wr, min(int(a.get("timeout") or 30), 120)),
        "list_directory": lambda a: t_list_directory(
                                        sp(a["path"]) if a.get("path") else Path(wr or "."),
                                        min(int(a.get("max_depth") or 3), 6)),
        # Tier 2
        "run_tests":      lambda a: t_run_tests(
                                        sp(a["path"]) if a.get("path") else Path(wr or "."),
                                        a.get("args", ""), wr),
        "run_linter":     lambda a: t_run_linter(sp(a["path"]), a.get("linter", ""), wr),
        "git_diff":       lambda a: t_git_diff(sp(a["path"]) if a.get("path") else None, wr),
        "git_log":        lambda a: t_git_log(int(a.get("limit") or 10),
                                               sp(a["path"]) if a.get("path") else None, wr),
        "git_blame":      lambda a: t_git_blame(sp(a["path"]), int(a["line"]), wr),
        "apply_patch":    lambda a: t_apply_patch(sp(a["path"]), a["patch"], wr),
        # Tier 3
        "analyze_code":       lambda a: t_analyze_code(sp(a["path"])),
        "generate_docstring": lambda a: t_generate_docstring(sp(a["path"]), a["function_name"]),
        "generate_readme":    lambda a: t_generate_readme(
                                            Path(wr or "."),
                                            sp(a["output_path"]) if a.get("output_path") else None),
        "generate_api_docs":  lambda a: t_generate_api_docs(
                                            Path(wr or "."),
                                            sp(a["output_path"]) if a.get("output_path") else None),
        "web_search":         lambda a: t_web_search(a["query"]),
        "fetch_url":          lambda a: t_fetch_url(a["url"]),
        # Tier 4
        "find_references": lambda a: t_find_references(
                                         a["symbol"],
                                         sp(a["path"]) if a.get("path") else Path(wr or ".")),
        "rename_symbol":   lambda a: t_rename_symbol(
                                         a["old_name"], a["new_name"],
                                         sp(a["path"]) if a.get("path") else Path(wr or ".")),
        "insert_at_line":  lambda a: t_insert_at_line(sp(a["path"]), int(a["line"]), a["content"]),
        "get_coverage":    lambda a: t_get_coverage(
                                         sp(a["path"]) if a.get("path") else Path(wr or "."), wr),
        "install_package": lambda a: t_install_package(a["package"], a.get("manager", ""), wr),
        "list_packages":   lambda a: t_list_packages(Path(wr or ".")),
    }


def execute_tool(name: str, tool_input: dict, workspace_root: str = "") -> str:
    """Single entry point for the advisor ReAct loop.
    Returns a JSON string ≤ 12 000 chars."""
    dispatch = _make_dispatch(workspace_root)
    fn = dispatch.get(name)
    if not fn:
        return json.dumps({"error": f"unknown tool '{name}'"})
    try:
        result = fn(tool_input or {})
        return json.dumps(result, default=str)[:12000]
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"{name} failed: {e}"})

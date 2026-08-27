"""Filesystem tools — Tier 1 (read/write/edit/list) and Tier 2 (apply_patch, insert_at_line)."""
from __future__ import annotations

import glob
import os
import re
import subprocess
from pathlib import Path
from typing import Optional


_READ_CAP = 20_000
_SEARCH_CAP = 200


# ── Tier 1 ───────────────────────────────────────────────────────────────────

def t_read_file(path: Path, start_line: Optional[int], end_line: Optional[int]) -> dict:
    if not path.exists():
        return {"error": f"File not found: {path}"}
    if not path.is_file():
        return {"error": f"Path is not a file: {path}"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"Cannot read file: {e}"}

    lines = text.splitlines(keepends=True)
    total = len(lines)

    if start_line is not None or end_line is not None:
        s = max(1, int(start_line or 1)) - 1
        e = min(total, int(end_line or total))
        lines = lines[s:e]
        text = "".join(lines)
        line_info = f"lines {s+1}-{e} of {total}"
    else:
        line_info = f"{total} lines"

    truncated = len(text) > _READ_CAP
    return {
        "path": str(path),
        "lines": line_info,
        "content": text[:_READ_CAP],
        "truncated": truncated,
    }


def t_write_file(path: Path, content: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "written": str(path),
        "chars": len(content),
        "lines": content.count("\n") + 1,
    }


def t_edit_file(path: Path, old_str: str, new_str: str) -> dict:
    if not path.exists():
        return {"error": f"File not found: {path}"}
    original = path.read_text(encoding="utf-8", errors="replace")
    if old_str not in original:
        # Show context around potential near-match to help model self-correct
        return {
            "error": f"old_str not found in {path.name}. "
                     "Check whitespace, indentation, and line endings."
        }
    count = original.count(old_str)
    updated = original.replace(old_str, new_str, 1)
    path.write_text(updated, encoding="utf-8")
    return {
        "edited": str(path),
        "occurrences_in_file": count,
        "replaced": 1,
        "note": "Replaced first occurrence only." if count > 1 else "OK",
    }


def t_search_files(pattern: str, search_type: str, root: Path) -> dict:
    root = Path(root)
    if search_type == "content":
        return _search_content(pattern, root)
    return _search_glob(pattern, root)


def _search_glob(pattern: str, root: Path) -> dict:
    matches = glob.glob(str(root / pattern), recursive=True)
    matches += glob.glob(pattern, recursive=True)
    # Deduplicate and sort
    seen: set[str] = set()
    results = []
    for m in matches:
        p = str(Path(m).resolve())
        if p not in seen:
            seen.add(p)
            results.append(m)
    results = results[:_SEARCH_CAP]
    return {"pattern": pattern, "type": "glob", "count": len(results), "matches": results}


def _search_content(pattern: str, root: Path) -> dict:
    results = []
    try:
        compiled = re.compile(pattern)
    except re.error:
        compiled = re.compile(re.escape(pattern))

    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for fname in files:
            fpath = Path(dirpath) / fname
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        results.append({
                            "file": str(fpath),
                            "line": i,
                            "text": line.strip()[:200],
                        })
                        if len(results) >= _SEARCH_CAP:
                            return {
                                "pattern": pattern, "type": "content",
                                "count": len(results),
                                "truncated": True,
                                "matches": results,
                            }
            except Exception:
                continue
    return {"pattern": pattern, "type": "content", "count": len(results), "matches": results}


def t_list_directory(root: Path, max_depth: int = 3) -> dict:
    root = Path(root)
    if not root.exists():
        return {"error": f"Path not found: {root}"}
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
            ".pytest_cache", ".mypy_cache", "htmlcov"}
    lines: list[str] = [str(root)]

    def _walk(path: Path, depth: int, prefix: str) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for i, entry in enumerate(entries):
            if entry.name in skip or entry.name.startswith("."):
                continue
            connector = "└── " if i == len(entries) - 1 else "├── "
            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                extension = "    " if i == len(entries) - 1 else "│   "
                _walk(entry, depth + 1, prefix + extension)
            else:
                size = entry.stat().st_size
                size_str = f"{size:,}B" if size < 1024 else f"{size//1024}KB"
                lines.append(f"{prefix}{connector}{entry.name}  ({size_str})")
            if len(lines) > 500:
                lines.append("... (truncated)")
                return

    _walk(root, 1, "")
    return {"root": str(root), "tree": "\n".join(lines), "entries": len(lines) - 1}


# ── Tier 2 ───────────────────────────────────────────────────────────────────

def t_apply_patch(path: Path, patch: str, workspace_root: str) -> dict:
    """Apply a unified diff patch via the system `patch` command."""
    if not path.exists():
        return {"error": f"File not found: {path}"}
    try:
        result = subprocess.run(
            ["patch", "-p1", str(path)],
            input=patch,
            capture_output=True,
            text=True,
            cwd=workspace_root or str(path.parent),
            timeout=30,
        )
        if result.returncode == 0:
            return {"patched": str(path), "output": result.stdout.strip()}
        return {"error": result.stderr.strip() or result.stdout.strip()}
    except FileNotFoundError:
        # `patch` not available — fall back to manual application
        return {"error": "patch command not found. Use edit_file for targeted edits instead."}
    except Exception as e:
        return {"error": f"apply_patch failed: {e}"}


def t_insert_at_line(path: Path, line: int, content: str) -> dict:
    if not path.exists():
        return {"error": f"File not found: {path}"}
    original = path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)
    insert_pos = max(0, min(line - 1, len(lines)))
    insert_lines = content.splitlines(keepends=True)
    if insert_lines and not insert_lines[-1].endswith("\n"):
        insert_lines[-1] += "\n"
    lines[insert_pos:insert_pos] = insert_lines
    path.write_text("".join(lines), encoding="utf-8")
    return {
        "inserted_at_line": line,
        "lines_inserted": len(insert_lines),
        "file": str(path),
        "new_total_lines": len(lines),
    }

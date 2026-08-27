import os
import re
import subprocess
from pathlib import Path

# Must match the value used in git_ops.py
_WORKSPACE_ROOT = Path(os.environ.get("AURA_WORKSPACE", "/workspace"))


def _clone_path(project_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
    return _WORKSPACE_ROOT / safe


def _resolve_project_dir(project_id: str) -> Path:
    """Return cloned repo path, trying DynamoDB first then env workspace."""
    # Fast path: check well-known path first
    p = _clone_path(project_id)
    if (p / ".git").exists():
        return p
    # Fall back to DynamoDB record
    try:
        from src.database import dynamo_client as db
        item = db.get_item("projects", {"projectId": project_id})
        if item and item.get("clonedPath"):
            candidate = Path(item["clonedPath"])
            if (candidate / ".git").exists():
                return candidate
    except Exception:
        pass
    raise FileNotFoundError(f"No cloned repo found for project '{project_id}'")


# ── Git file-system tools (available when a project repo is cloned) ───────────

def list_files(project_id: str, directory: str = ".") -> dict:
    """List files in the cloned repository, optionally within a sub-directory."""
    try:
        clone_dir = _resolve_project_dir(project_id)
        target = (clone_dir / directory).resolve()
        if not str(target).startswith(str(clone_dir)):
            return {"error": "Directory traversal not allowed"}
        if not target.exists():
            return {"error": f"Directory '{directory}' not found"}
        files = []
        for entry in sorted(target.rglob("*")):
            rel = entry.relative_to(clone_dir)
            parts = rel.parts
            if any(p.startswith(".git") or p in ("node_modules", "__pycache__") for p in parts):
                continue
            files.append({"path": str(rel).replace("\\", "/"), "type": "dir" if entry.is_dir() else "file", "size": entry.stat().st_size if entry.is_file() else 0})
        return {"files": files, "count": len(files), "root": directory}
    except FileNotFoundError as exc:
        return {"error": str(exc)}


def read_file(project_id: str, file_path: str) -> dict:
    """Read the contents of a file from the cloned repository."""
    try:
        clone_dir = _resolve_project_dir(project_id)
        target = (clone_dir / file_path).resolve()
        if not str(target).startswith(str(clone_dir)):
            return {"error": "Path traversal not allowed"}
        if not target.is_file():
            return {"error": f"File '{file_path}' not found"}
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"content": content, "path": file_path, "lines": content.count("\n") + 1}
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Cannot read file: {exc}"}


def write_file(project_id: str, file_path: str, content: str) -> dict:
    """Write or overwrite a file in the cloned repository."""
    try:
        clone_dir = _resolve_project_dir(project_id)
        target = (clone_dir / file_path).resolve()
        if not str(target).startswith(str(clone_dir)):
            return {"error": "Path traversal not allowed"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"success": True, "path": file_path, "size": len(content.encode())}
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Cannot write file: {exc}"}


def get_diff(project_id: str) -> dict:
    """Return the current git diff (all changes) in the cloned repository."""
    try:
        clone_dir = _resolve_project_dir(project_id)
        result = subprocess.run(["git", "-C", str(clone_dir), "diff", "HEAD"], capture_output=True, text=True, timeout=15)
        diff_text = result.stdout or ""
        status = subprocess.run(["git", "-C", str(clone_dir), "status", "--short"], capture_output=True, text=True, timeout=10)
        changed = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()]
        return {"diff": diff_text, "changedFiles": changed, "hasChanges": bool(diff_text or changed)}
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}


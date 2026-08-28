import os
import re
import subprocess
from pathlib import Path

# Must match the value used in git_ops.py
# .resolve(): a relative AURA_WORKSPACE (e.g. ./data/workspace) is otherwise
# interpreted against whatever cwd a subprocess happens to run in.
_WORKSPACE_ROOT = Path(os.environ.get("AURA_WORKSPACE", "/workspace")).resolve()


def _clone_path(project_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
    return _WORKSPACE_ROOT / safe


def _safe_target(clone_dir: Path, relative: str) -> Path:
    """Resolve `relative` inside `clone_dir`, refusing anything that escapes.

    `str().startswith()` is not a containment check: with a clone at
    /workspace/foo it accepts /workspace/foo-evil. `Path.relative_to` compares
    path components, which is what we actually mean.
    """
    target = (clone_dir / relative).resolve()
    try:
        target.relative_to(clone_dir.resolve())
    except ValueError as exc:
        raise PermissionError("Path traversal not allowed") from exc
    return target


# ── Pending changes (the approval gate) ──────────────────────────────────────
# write_file STAGES a change rather than writing it. The model gets a diff back
# and carries on; the operator applies or discards from the UI. Previously this
# was an unconditional overwrite with no preview, no undo and no UI feedback —
# the agent could silently rewrite a repo.
#
# Staged changes live on disk inside the clone's .git directory, NOT in a process
# dict. Two reasons: the agent stages from the WebSocket worker while the UI reads
# through a REST worker, and under `uvicorn --workers > 1` those are different
# processes; and .git/ is never committed, so `git status` stays clean.
_STAGE_DIRNAME = "aura-pending"


def _stage_dir(project_id: str) -> Path:
    d = _resolve_project_dir(project_id) / ".git" / _STAGE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stage_file(project_id: str, file_path: str) -> Path:
    import hashlib
    digest = hashlib.sha256(file_path.encode()).hexdigest()[:16]
    return _stage_dir(project_id) / f"{digest}.json"


def _pending_for(project_id: str) -> dict[str, str]:
    """All staged changes for a project, as {path: content}."""
    import json
    out: dict[str, str] = {}
    try:
        for f in sorted(_stage_dir(project_id).glob("*.json")):
            try:
                rec = json.loads(f.read_text())
                out[rec["path"]] = rec["content"]
            except Exception:  # noqa: BLE001 — a corrupt entry must not hide the rest
                continue
    except FileNotFoundError:
        pass
    return out


def _stage_write(project_id: str, file_path: str, content: str) -> None:
    import json
    _stage_file(project_id, file_path).write_text(
        json.dumps({"path": file_path, "content": content}), encoding="utf-8")


def _stage_pop(project_id: str, file_path: str) -> str | None:
    import json
    f = _stage_file(project_id, file_path)
    if not f.is_file():
        return None
    content = json.loads(f.read_text())["content"]
    f.unlink(missing_ok=True)
    return content


def _unified_diff(path: str, before: str, after: str) -> str:
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))


def list_pending(project_id: str) -> list[dict]:
    """Staged changes with their diffs, for the UI."""
    out = []
    try:
        clone_dir = _resolve_project_dir(project_id)
    except FileNotFoundError:
        return out
    for path, content in _pending_for(project_id).items():
        try:
            target = _safe_target(clone_dir, path)
            before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        except Exception:  # noqa: BLE001
            before = ""
        diff = _unified_diff(path, before, content)
        out.append({
            "path": path,
            "diff": diff,
            "additions": sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")),
            "deletions": sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")),
        })
    return out


def apply_pending(project_id: str, file_path: str) -> dict:
    """Write one staged change to disk."""
    try:
        clone_dir = _resolve_project_dir(project_id)
        target = _safe_target(clone_dir, file_path)
        content = _stage_pop(project_id, file_path)
        if content is None:
            return {"error": f"No staged change for '{file_path}'"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"success": True, "path": file_path}
    except (FileNotFoundError, PermissionError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cannot apply change: {exc}"}


def discard_pending(project_id: str, file_path: str) -> dict:
    """Drop a staged change without writing it."""
    try:
        if _stage_pop(project_id, file_path) is None:
            return {"error": f"No staged change for '{file_path}'"}
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    return {"success": True, "path": file_path}


def _resolve_project_dir(project_id: str) -> Path:
    """Return cloned repo path, trying DynamoDB first then env workspace."""
    # Fast path: check well-known path first
    p = _clone_path(project_id)
    if (p / ".git").exists():
        return p
    # Fall back to the DynamoDB record. `projects` is a COMPOSITE table
    # (projectId + userId), so get_item with the partition key alone raises a
    # ValidationException — query on the partition key instead.
    try:
        from src.database import dynamo_client as db
        rows = db.query_items("projects", "projectId", project_id, limit=1)
        if rows and rows[0].get("clonedPath"):
            candidate = Path(rows[0]["clonedPath"])
            if (candidate / ".git").exists():
                return candidate
    except Exception:  # noqa: BLE001
        pass
    raise FileNotFoundError(f"No cloned repo found for project '{project_id}'")


# ── Git file-system tools (available when a project repo is cloned) ───────────

def list_files(project_id: str, directory: str = ".") -> dict:
    """List files in the cloned repository, optionally within a sub-directory."""
    try:
        clone_dir = _resolve_project_dir(project_id)
        target = _safe_target(clone_dir, directory)
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
    except (FileNotFoundError, PermissionError) as exc:
        return {"error": str(exc)}


def read_file(project_id: str, file_path: str) -> dict:
    """Read the contents of a file from the cloned repository."""
    try:
        clone_dir = _resolve_project_dir(project_id)
        target = _safe_target(clone_dir, file_path)
        if not target.is_file():
            return {"error": f"File '{file_path}' not found"}
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"content": content, "path": file_path, "lines": content.count("\n") + 1}
    except (FileNotFoundError, PermissionError) as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Cannot read file: {exc}"}


def write_file(project_id: str, file_path: str, content: str) -> dict:
    """STAGE a change to a file in the cloned repository.

    Nothing is written to disk here. The change is held until the operator applies
    it from the UI. The tool result tells the model the change is staged, so it can
    continue reasoning without waiting on a human round-trip.
    """
    try:
        clone_dir = _resolve_project_dir(project_id)
        target = _safe_target(clone_dir, file_path)
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        if before == content:
            return {"staged": False, "path": file_path,
                    "message": "No change — the file already has this content."}
        _stage_write(project_id, file_path, content)
        diff = _unified_diff(file_path, before, content)
        return {
            "staged": True,
            "path": file_path,
            "size": len(content.encode()),
            "diff": diff[:4000],
            "message": ("Change staged for review. It is NOT yet written to disk — "
                        "the user must approve it in the UI."),
        }
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except PermissionError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cannot stage change: {exc}"}


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


import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Must match the value used in git_ops.py.
def _workspace_root() -> Path:
    """Where project working copies live.

    Read through Settings, NOT `os.environ`. pydantic-settings loads `src/.env` into
    the Settings object and never into the process environment, so an `os.environ`
    read silently ignores a configured `AURA_WORKSPACE` and falls back to
    `/workspace` — which exists in the container and cannot be created on a Mac,
    where the root volume is read-only. That is the whole bug.

    A function, not an import-time constant, so the value follows configuration
    rather than whatever the environment looked like when the module was first
    imported. The env fallback stays for callers that run without app settings.

    .resolve(): a relative path (./data/workspace) is otherwise interpreted against
    whatever cwd a subprocess happens to have.
    """
    try:
        from src.config_settings import get_settings
        configured = get_settings().aura_workspace
    except Exception:  # noqa: BLE001 — must still work without app settings
        configured = ""
    return Path(configured or os.environ.get("AURA_WORKSPACE", "/workspace")).resolve()


def _clone_path(project_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
    return _workspace_root() / safe


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


def pending_count(project_id: str) -> int:
    """How many changes are staged, WITHOUT resolving the project in DynamoDB.

    `list_pending` is the honest but expensive answer: it calls
    `_resolve_project_dir`, which on a cache miss issues a DynamoDB query per
    project, then reads and diffs every staged file. Ranking a hundred projects
    with it would mean a hundred queries and a hundred filesystem walks on every
    page load.

    Ranking does not need the diffs — only whether there is anything waiting. So
    this takes the well-known clone path only, and a project whose clone lives
    somewhere unusual simply reports 0 and loses its ranking boost. That is the
    right trade: a wrong sort order is recoverable, a page that takes ten
    seconds to load is not.
    """
    try:
        stage = _clone_path(project_id) / ".git" / _STAGE_DIRNAME
        if not stage.is_dir():
            return 0
        return sum(1 for entry in stage.iterdir() if entry.suffix == ".json")
    except Exception:                                         # noqa: BLE001
        return 0


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


def _stage_write(project_id: str, file_path: str, content: str,
                 session_id: str = "", user_id: str = "") -> None:
    """Stage a proposal, carrying who proposed it and when.

    The provenance is written HERE because this is the only moment it is known:
    the agent stages from the WebSocket worker, while apply/discard arrive later
    through a REST worker that knows the project and the path and nothing else.
    """
    import json
    from datetime import datetime, timezone
    _stage_file(project_id, file_path).write_text(json.dumps({
        "path": file_path,
        "content": content,
        "sessionId": session_id,
        "userId": user_id,
        "proposedAt": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")


def _stage_read(project_id: str, file_path: str) -> dict | None:
    """The staged record, without removing it."""
    import json
    f = _stage_file(project_id, file_path)
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:                                         # noqa: BLE001
        return None


def _stage_pop(project_id: str, file_path: str) -> str | None:
    import json
    f = _stage_file(project_id, file_path)
    if not f.is_file():
        return None
    content = json.loads(f.read_text())["content"]
    f.unlink(missing_ok=True)
    return content


def attach_commit(project_id: str, commit_sha: str = "", pr_url: str = "",
                  paths: list[str] | None = None) -> int:
    """Record which commit (or PR) carried this project's applied changes.

    Returns how many proposal rows were updated.

    The chain proposal -> file on disk -> commit -> PR was broken at its second link:
    `git_ops` computed the SHA and the PR URL, returned them in an HTTP response body
    and persisted neither, so there was no way to answer "which conversation produced
    this commit" — or the reverse — even though both halves were known at the time.

    Applies to APPLIED, UNCOMMITTED rows only. A commit sweeps up whatever is staged in
    the working tree, so the honest attribution is "every applied change that had not
    yet been attached to one", not a guess at which files this particular commit
    touched. When the caller does know the paths, it says so and only those are taken.

    Best-effort, like `record_decision` itself: a bookkeeping write must never be able
    to fail a commit the operator has already made.
    """
    if not project_id or not (commit_sha or pr_url):
        return 0
    try:
        from src.database import dynamo_client as db
        rows = db.query_items("devmate-proposals", "projectId", project_id, limit=500)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("attach_commit: could not read proposals for %s: %s", project_id, exc)
        return 0

    wanted = set(paths or [])
    updated = 0
    for row in rows:
        if str(row.get("decision") or "") != "applied":
            continue
        if row.get("commitSha") or row.get("prUrl"):
            continue
        if wanted and str(row.get("path") or "") not in wanted:
            continue
        patch = {}
        if commit_sha:
            patch["commitSha"] = str(commit_sha)[:64]
        if pr_url:
            patch["prUrl"] = str(pr_url)[:400]
        try:
            from src.database import dynamo_client as db
            db.update_item("devmate-proposals",
                           {"projectId": project_id,
                            "proposalId": str(row.get("proposalId") or "")},
                           patch)
            updated += 1
        except Exception as exc:                              # noqa: BLE001
            log.warning("attach_commit: could not update %s: %s",
                        row.get("proposalId"), exc)
    return updated


def record_decision(project_id: str, file_path: str, decision: str,
                    staged: dict | None, decided_by: str = "",
                    additions: int = 0, deletions: int = 0) -> None:
    """Record that a proposal was applied or discarded.

    Before this existed, `apply_pending` and `discard_pending` both unlinked the
    stage file and wrote nothing — so the two outcomes were byte-for-byte
    indistinguishable afterwards and "how much of the agent's advice do people
    take?" was unanswerable. Apply logged a line; discard logged nothing.

    Best-effort, and deliberately so: a DynamoDB hiccup must never stop a file
    change the operator asked for. The same reasoning as `_persist_token_usage`.
    """
    try:
        import hashlib
        from datetime import datetime, timezone
        from src.database import dynamo_client as db

        staged = staged or {}
        now = datetime.now(timezone.utc).isoformat()
        proposed_at = str(staged.get("proposedAt") or now)
        digest = hashlib.sha256(file_path.encode()).hexdigest()[:8]
        db.put_item("devmate-proposals", {
            "projectId": project_id,
            "proposalId": f"{proposed_at}#{digest}",
            "sessionId": str(staged.get("sessionId") or ""),
            "userId": str(staged.get("userId") or decided_by or ""),
            "path": file_path,
            "additions": int(additions),
            "deletions": int(deletions),
            "proposedAt": proposed_at,
            "decision": decision,
            "decidedAt": now,
            "decidedBy": decided_by,
        })
    except Exception as exc:                                  # noqa: BLE001
        log.warning("could not record %s of %s: %s", decision, file_path, exc)


def _unified_diff(path: str, before: str, after: str) -> str:
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))


def _diff_counts(diff: str) -> tuple[int, int]:
    """(additions, deletions) for a unified diff, ignoring the +++/--- headers."""
    adds = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return adds, dels


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
        adds, dels = _diff_counts(diff)
        out.append({"path": path, "diff": diff,
                    "additions": adds, "deletions": dels})
    return out


def apply_pending(project_id: str, file_path: str, decided_by: str = "") -> dict:
    """Write one staged change to disk, and record that it was taken."""
    try:
        clone_dir = _resolve_project_dir(project_id)
        target = _safe_target(clone_dir, file_path)
        # Read the staged record and measure the diff BEFORE popping: _stage_pop
        # unlinks the file, and after that there is nothing left to attribute.
        staged = _stage_read(project_id, file_path)
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        content = _stage_pop(project_id, file_path)
        if content is None:
            return {"error": f"No staged change for '{file_path}'"}
        adds, dels = _diff_counts(_unified_diff(file_path, before, content))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        record_decision(project_id, file_path, "applied", staged, decided_by, adds, dels)
        return {"success": True, "path": file_path}
    except (FileNotFoundError, PermissionError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Cannot apply change: {exc}"}


def discard_pending(project_id: str, file_path: str, decided_by: str = "") -> dict:
    """Drop a staged change without writing it, and record that it was refused.

    A discard used to leave exactly as much trace as an apply: none. Recording
    it is the whole point — advice nobody takes is the more interesting half.
    """
    try:
        staged = _stage_read(project_id, file_path)
        if _stage_pop(project_id, file_path) is None:
            return {"error": f"No staged change for '{file_path}'"}
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    record_decision(project_id, file_path, "discarded", staged, decided_by)
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


def write_file(project_id: str, file_path: str, content: str,
               session_id: str = "", user_id: str = "") -> dict:
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
        _stage_write(project_id, file_path, content, session_id, user_id)
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


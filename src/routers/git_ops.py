"""Git operations router — branch discovery, clone, file R/W, diff, commit, PR creation."""
from __future__ import annotations

import logging
import subprocess
import re
import tempfile
import os
import shutil
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from src.routers.auth import get_current_user

# Persistent workspace root for cloned repos (ECS-friendly; override via env var)
_WORKSPACE_ROOT = Path(os.environ.get("AURA_WORKSPACE", "/workspace")).resolve()

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/git", tags=["git-ops"])


def _inject_pat(url: str, token: str) -> str:
    """Inject a PAT into an HTTPS Git URL using the x-access-token format accepted by GitHub Enterprise."""
    if not token or not url.startswith("https://"):
        return url
    clean = url.replace("https://", "")
    return f"https://x-access-token:{token}@{clean}"


@router.get("/branches")
def list_branches(
    url: str = Query(..., description="Git repository HTTPS URL"),
    token: str = Query("", description="Personal access token"),
    _: dict = Depends(get_current_user),
):
    """List remote branches for a Git repository."""
    auth_url = _inject_pat(url, token)
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "--tags", auth_url],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=f"Cannot access repository: {result.stderr[:200]}")

        branches = []
        for line in result.stdout.strip().splitlines():
            if "\trefs/heads/" in line:
                branch = line.split("\trefs/heads/")[-1].strip()
                branches.append(branch)

        default_branch = "main" if "main" in branches else (branches[0] if branches else "main")
        return {"branches": branches, "defaultBranch": default_branch}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Repository connection timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Git not found on server")


class BranchCreate(BaseModel):
    repoUrl: str
    token: str = ""
    baseBranch: str = "main"
    newBranchName: str


@router.post("/branch", status_code=201)
def create_branch(body: BranchCreate, _: dict = Depends(get_current_user)):
    """Create a new branch from a base branch in a remote repository."""
    auth_url = _inject_pat(body.repoUrl, body.token)
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", body.baseBranch, auth_url, tmpdir],
                capture_output=True, text=True, timeout=60, check=True,
            )
            subprocess.run(
                ["git", "-C", tmpdir, "checkout", "-b", body.newBranchName],
                capture_output=True, text=True, check=True,
            )
            result = subprocess.run(
                ["git", "-C", tmpdir, "push", "origin", body.newBranchName],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise HTTPException(status_code=400, detail=f"Push failed: {result.stderr[:200]}")
            return {"success": True, "branch": body.newBranchName, "message": f"Branch '{body.newBranchName}' created from '{body.baseBranch}'"}
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=400, detail=f"Git operation failed: {e.stderr[:200] if e.stderr else str(e)}")
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=408, detail="Git operation timed out")


class PRCreate(BaseModel):
    repoUrl: str
    token: str
    baseBranch: str = "main"
    headBranch: str
    title: str
    body: str = ""
    changedFiles: list[str] = []


@router.post("/pr", status_code=201)
def create_pull_request(body: PRCreate, user: dict = Depends(get_current_user)):
    """Create a pull request via GitHub/GitLab REST API."""
    import urllib.request
    import json as json_lib

    url = body.repoUrl.rstrip("/")
    # Extract owner/repo from URL
    # Handles: https://github.com/owner/repo, https://github.com/owner/repo.git
    match = re.search(r'github\.com[:/](.+?)(?:\.git)?$', url)
    if match:
        repo_path = match.group(1)
        api_url = f"https://api.github.com/repos/{repo_path}/pulls"
        headers = {
            "Authorization": f"Bearer {body.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload = {
            "title": body.title,
            "body": body.body,
            "head": body.headBranch,
            "base": body.baseBranch,
        }
        try:
            req = urllib.request.Request(api_url, data=json_lib.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json_lib.loads(resp.read())
                return {"success": True, "prUrl": data.get("html_url"), "prNumber": data.get("number")}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            raise HTTPException(status_code=e.code, detail=f"GitHub API error: {detail}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # GitLab support
    match = re.search(r'gitlab[^/]*/(.+?)(?:\.git)?$', url)
    if match:
        repo_path = match.group(1).replace("/", "%2F")
        gitlab_host = re.match(r'(https?://[^/]+)', url).group(1)
        api_url = f"{gitlab_host}/api/v4/projects/{repo_path}/merge_requests"
        headers = {
            "PRIVATE-TOKEN": body.token,
            "Content-Type": "application/json",
        }
        payload = {
            "title": body.title,
            "description": body.body,
            "source_branch": body.headBranch,
            "target_branch": body.baseBranch,
        }
        try:
            req = urllib.request.Request(api_url, data=json_lib.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json_lib.loads(resp.read())
                return {"success": True, "prUrl": data.get("web_url"), "prNumber": data.get("iid")}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=400, detail="Unsupported Git provider. Only GitHub and GitLab are supported.")


# ── Persistent clone helpers ──────────────────────────────────────────────────

def _clone_path(project_id: str) -> Path:
    """Return the persistent clone directory for a project."""
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id or "")
    if not safe:
        # `_WORKSPACE_ROOT / ""` is the workspace root itself, so a blank id would
        # target every project's clone directory at once.
        raise HTTPException(status_code=400, detail="projectId is required")
    return _WORKSPACE_ROOT / safe


def _require_clone(project_id: str) -> Path:
    """Return clone path or raise 404 if not cloned yet."""
    p = _clone_path(project_id)
    if not (p / ".git").exists():
        raise HTTPException(status_code=404, detail=f"No cloned repo found for project '{project_id}'. Clone it first via POST /api/git/clone.")
    return p


# ── Clone ─────────────────────────────────────────────────────────────────────

class CloneRequest(BaseModel):
    repoUrl: str
    branch: str = "main"
    token: str = ""
    projectId: str


@router.post("/clone", status_code=201)
def clone_repo(body: CloneRequest, user: dict = Depends(get_current_user)):
    """Clone a repository to a persistent workspace directory on the server."""
    target = _clone_path(body.projectId)
    auth_url = _inject_pat(body.repoUrl, body.token)

    # If already cloned, do a fast-forward pull instead
    if (target / ".git").exists():
        try:
            # Re-apply auth token to origin URL so the pull can authenticate
            if body.token:
                subprocess.run(
                    ["git", "-C", str(target), "remote", "set-url", "origin", auth_url],
                    capture_output=True, text=True, timeout=10,
                )
            subprocess.run(
                ["git", "-C", str(target), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )
            return {"success": True, "clonedPath": str(target), "message": "Repo already cloned — pulled latest changes."}
        except Exception:
            shutil.rmtree(target, ignore_errors=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "50", "--branch", body.branch, auth_url, str(target)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=f"Clone failed: {result.stderr[:400]}")

        # Persist clonedPath to DynamoDB project record (best-effort).
        # `projects` is a COMPOSITE table (projectId + userId) — updating with the
        # partition key alone is rejected by DynamoDB, so this silently never
        # persisted. _resolve_project_dir happens to check the well-known path
        # first, which is why the tools still worked and the bug stayed hidden.
        try:
            from src.database import dynamo_client as db
            db.update_item(
                "projects",
                {"projectId": body.projectId, "userId": user["userId"]},
                {"clonedPath": str(target), "clonedBranch": body.branch},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist clonedPath for %s: %s", body.projectId, exc)

        return {"success": True, "clonedPath": str(target), "message": f"Cloned '{body.branch}' to {target}"}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Clone timed out")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── File listing ──────────────────────────────────────────────────────────────

@router.get("/files")
def list_files(
    projectId: str = Query(...),
    directory: str = Query(".", description="Relative path within the repo"),
    _: dict = Depends(get_current_user),
):
    """List files in the cloned repository directory."""
    clone_dir = _require_clone(projectId)
    target_dir = (clone_dir / directory).resolve()

    # Security: must stay inside clone root
    if not str(target_dir).startswith(str(clone_dir)):
        raise HTTPException(status_code=400, detail="Directory traversal not allowed")

    if not target_dir.exists():
        raise HTTPException(status_code=404, detail=f"Directory '{directory}' not found in repo")

    files = []
    for entry in sorted(target_dir.rglob("*")):
        rel = entry.relative_to(clone_dir)
        parts = rel.parts
        if any(p.startswith(".git") for p in parts):
            continue
        if any(p in ("node_modules", "__pycache__", ".mypy_cache") for p in parts):
            continue
        files.append({
            "path": str(rel).replace("\\", "/"),
            "type": "dir" if entry.is_dir() else "file",
            "size": entry.stat().st_size if entry.is_file() else 0,
        })

    return {"files": files, "root": str(clone_dir)}


# ── File read ─────────────────────────────────────────────────────────────────

@router.get("/file")
def read_file(
    projectId: str = Query(...),
    filePath: str = Query(..., description="Relative file path within the repo"),
    _: dict = Depends(get_current_user),
):
    """Read the contents of a file in the cloned repository."""
    clone_dir = _require_clone(projectId)
    target = (clone_dir / filePath).resolve()

    if not str(target).startswith(str(clone_dir)):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"File '{filePath}' not found")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read file: {exc}")

    return {"content": content, "path": filePath, "size": target.stat().st_size}


# ── File write ────────────────────────────────────────────────────────────────

class FileWrite(BaseModel):
    projectId: str
    filePath: str
    content: str


@router.post("/file")
def write_file(body: FileWrite, _: dict = Depends(get_current_user)):
    """Write or overwrite a file in the cloned repository."""
    clone_dir = _require_clone(body.projectId)
    target = (clone_dir / body.filePath).resolve()

    if not str(target).startswith(str(clone_dir)):
        raise HTTPException(status_code=400, detail="Path traversal not allowed")

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(body.content, encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cannot write file: {exc}")

    return {"success": True, "path": body.filePath, "size": len(body.content.encode())}


# ── Git diff ──────────────────────────────────────────────────────────────────

@router.get("/diff")
def get_diff(
    projectId: str = Query(...),
    _: dict = Depends(get_current_user),
):
    """Return the current git diff (staged + unstaged) in the cloned repo."""
    clone_dir = _require_clone(projectId)

    result = subprocess.run(
        ["git", "-C", str(clone_dir), "diff", "HEAD"],
        capture_output=True, text=True, timeout=15,
    )
    diff_text = result.stdout or ""

    # Collect changed file names
    status_result = subprocess.run(
        ["git", "-C", str(clone_dir), "status", "--short"],
        capture_output=True, text=True, timeout=10,
    )
    changed = [line[3:].strip() for line in status_result.stdout.splitlines() if line.strip()]

    return {"diff": diff_text, "changedFiles": changed, "hasChanges": bool(diff_text or changed)}


# ── Commit & push ─────────────────────────────────────────────────────────────

class CommitRequest(BaseModel):
    projectId: str
    message: str
    newBranch: str = ""   # if set, creates a new branch before committing


@router.post("/commit", status_code=201)
def commit_and_push(body: CommitRequest, user: dict = Depends(get_current_user)):
    """Stage all changes, commit, and push to origin."""
    clone_dir = _require_clone(body.projectId)

    def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-C", str(clone_dir)] + cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise HTTPException(status_code=400, detail=f"git {cmd[0]} failed: {r.stderr[:300]}")
        return r

    try:
        # Configure git identity if not set
        subprocess.run(["git", "-C", str(clone_dir), "config", "user.email", user.get("email", "aura@aura.com")], capture_output=True)
        subprocess.run(["git", "-C", str(clone_dir), "config", "user.name", user.get("username", "AURA")], capture_output=True)

        if body.newBranch:
            _run(["checkout", "-b", body.newBranch])

        _run(["add", "-A"])

        # Check if there's anything to commit
        status = subprocess.run(["git", "-C", str(clone_dir), "status", "--short"], capture_output=True, text=True)
        if not status.stdout.strip():
            return {"success": True, "message": "Nothing to commit — working tree clean", "commitSha": "", "branch": body.newBranch or ""}

        commit_result = _run(["commit", "-m", body.message])
        sha_result = subprocess.run(["git", "-C", str(clone_dir), "rev-parse", "HEAD"], capture_output=True, text=True)
        commit_sha = sha_result.stdout.strip()

        branch_result = subprocess.run(["git", "-C", str(clone_dir), "branch", "--show-current"], capture_output=True, text=True)
        current_branch = branch_result.stdout.strip()

        push_result = subprocess.run(
            ["git", "-C", str(clone_dir), "push", "--set-upstream", "origin", current_branch],
            capture_output=True, text=True, timeout=60,
        )
        if push_result.returncode != 0:
            raise HTTPException(status_code=400, detail=f"Push failed: {push_result.stderr[:300]}")

        return {"success": True, "commitSha": commit_sha, "branch": current_branch, "message": "Committed and pushed"}
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Git operation timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Pending changes — the approval gate ──────────────────────────────────────
# DevMate's write_file stages a change instead of writing it. These endpoints let
# the operator review the diff and decide. Before this, the agent overwrote repo
# files the instant it emitted a tool_use block, with no preview and no undo.

class PendingActionRequest(BaseModel):
    projectId: str
    path: str


@router.get("/pending/{project_id}")
def get_pending_changes(project_id: str, user: dict = Depends(get_current_user)):
    """Changes DevMate has proposed for this project but not yet written."""
    from src.services.advisor import tools as advisor_tools
    return {"changes": advisor_tools.list_pending(project_id)}


@router.post("/pending/apply")
def apply_pending_change(body: PendingActionRequest,
                         user: dict = Depends(get_current_user)):
    """Write one staged change to disk."""
    from src.services.advisor import tools as advisor_tools
    result = advisor_tools.apply_pending(body.projectId, body.path)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    log.info("User %s applied agent change %s in %s",
             user.get("username"), body.path, body.projectId)
    return result


@router.post("/pending/discard")
def discard_pending_change(body: PendingActionRequest,
                           user: dict = Depends(get_current_user)):
    """Drop a staged change without writing it."""
    from src.services.advisor import tools as advisor_tools
    result = advisor_tools.discard_pending(body.projectId, body.path)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ── Folder upload (the wizard's "Local Folder" picker) ────────────────────────
#
# A browser cannot hand the server a filesystem path: <input webkitdirectory>
# yields File objects whose only location hint is the relative `webkitRelativePath`,
# and `value` is deliberately spoofed as "C:\fakepath\...". Typing a path into a
# text box is worse than useless once the backend runs in Fargate, where the
# operator's /Users/... simply does not exist.
#
# So the client reads the folder in-browser and posts the files here; the server
# rebuilds the tree under the project's workspace directory. That directory is
# then `git init`-ed, because _resolve_project_dir gates on `.git` existing —
# without it DevMate's file tools never attach and QualityMind cannot run tests.

_UPLOAD_MAX_BYTES = 60 * 1024 * 1024
_UPLOAD_MAX_FILES = 4000
_UPLOAD_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", ".turbo", "target", "vendor",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode",
    "coverage", ".gradle", "bin", "obj",
}
_UPLOAD_SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".class", ".o", ".a",
    ".exe", ".zip", ".tar", ".gz", ".jar", ".war", ".png", ".jpg",
    ".jpeg", ".gif", ".ico", ".pdf", ".mp4", ".mov", ".woff", ".woff2",
}


def _safe_label(label: str) -> str:
    """A single path segment — this becomes a directory name under the project."""
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", (label or "").strip())[:40].strip("_")
    if not safe:
        raise HTTPException(status_code=400, detail="label is required (e.g. 'backend')")
    return safe


def _safe_rel(rel: str) -> Path | None:
    """Validate a browser-supplied relative path, or None if it must be skipped.

    `webkitRelativePath` is attacker-controllable in exactly the way a zip entry
    is, so this is the zip-slip check: reject anything absolute, anything with a
    `..` component, and Windows drive letters. Returning None (skip) rather than
    raising keeps one junk entry from failing an otherwise good upload.
    """
    rel = (rel or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or re.match(r"^[a-zA-Z]:", rel):
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if any(p in _UPLOAD_SKIP_DIRS for p in parts[:-1]):
        return None
    name = parts[-1]
    if name in _UPLOAD_SKIP_DIRS or Path(name).suffix.lower() in _UPLOAD_SKIP_SUFFIXES:
        return None
    return Path(*parts)


def _git_init_workspace(root: Path, message: str) -> None:
    """Make `root` a git repo so _resolve_project_dir accepts it.

    Committing with -c rather than relying on global config: the container has no
    ~/.gitconfig, and `git commit` fails outright without an identity.
    """
    ident = ["-c", "user.name=Aura", "-c", "user.email=aura@local"]
    try:
        if not (root / ".git").exists():
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)],
                           capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "-C", str(root)] + ident + ["add", "-A"],
                       capture_output=True, text=True, timeout=120)
        subprocess.run(["git", "-C", str(root)] + ident +
                       ["commit", "-q", "--allow-empty", "-m", message],
                       capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001 — the files are on disk either way
        log.warning("git init/commit failed for %s: %s", root, exc)


@router.post("/upload-folder", status_code=201)
async def upload_folder(
    projectId: str = Form(...),
    label: str = Form(...),
    paths: list[str] = Form(...),
    files: list[UploadFile] = File(...),
    user: dict = Depends(get_current_user),
):
    """Rebuild an uploaded folder under the project workspace and commit it.

    Re-uploading the same label replaces that subtree only, so `backend` and
    `frontend` can be uploaded independently and in any order.
    """
    safe_label = _safe_label(label)
    root = _clone_path(projectId)
    dest = root / safe_label

    if len(paths) != len(files):
        raise HTTPException(
            status_code=400,
            detail=f"paths/files length mismatch ({len(paths)} vs {len(files)})")
    if len(files) > _UPLOAD_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files ({len(files)}) — maximum {_UPLOAD_MAX_FILES}. "
                   "Exclude build output and dependency directories.")

    # Replace rather than merge: a stale file from a previous upload would
    # otherwise survive and be analysed as if it were still part of the folder.
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    written = skipped = total = 0
    for rel_raw, upload in zip(paths, files):
        rel = _safe_rel(rel_raw)
        if rel is None:
            skipped += 1
            continue
        raw = await upload.read()
        total += len(raw)
        if total > _UPLOAD_MAX_BYTES:
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(
                status_code=413,
                detail=f"Folder exceeds {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB. "
                       "Exclude build output and dependency directories.")
        target = dest / rel
        try:
            target.relative_to(dest)          # defence in depth after _safe_rel
        except ValueError:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        written += 1

    if written == 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail=f"No usable files in '{label}' — all {skipped} entries were "
                   "build output, binaries, or dependency directories.")

    _git_init_workspace(root, f"Upload folder '{safe_label}' ({written} files)")

    # Archive for audit and re-extraction. EFS already holds the working copy, so
    # this is best-effort — a missing bucket must not fail the upload.
    try:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(dest.rglob("*")):
                if f.is_file():
                    zf.write(f, str(f.relative_to(dest)))
        from src.storage import s3_client
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        s3_client.put_object(
            "uploads", f"folders/{projectId}/{safe_label}-{stamp}.zip", buf.getvalue())
    except Exception as exc:  # noqa: BLE001
        log.warning("Folder archive to S3 skipped for %s/%s: %s", projectId, safe_label, exc)

    # Composite key — the partition key alone is rejected by DynamoDB. This is the
    # same trap that stopped /clone persisting clonedPath.
    try:
        from src.database import dynamo_client as db
        db.update_item(
            "projects",
            {"projectId": projectId, "userId": user["userId"]},
            {"clonedPath": str(root), "clonedBranch": "main"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist clonedPath for %s: %s", projectId, exc)

    return {
        "success": True,
        "label": safe_label,
        "localPath": str(dest),
        "projectPath": str(root),
        "fileCount": written,
        "skippedCount": skipped,
        "bytes": total,
        "message": f"Uploaded {written} file(s) to {safe_label}"
                   + (f", skipped {skipped}" if skipped else ""),
    }

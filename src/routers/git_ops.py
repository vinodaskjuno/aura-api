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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.routers.auth import get_current_user

# Persistent workspace root for cloned repos (ECS-friendly; override via env var)
_WORKSPACE_ROOT = Path(os.environ.get("AURA_WORKSPACE", "/workspace"))

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
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
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

        # Persist clonedPath to DynamoDB project record (best-effort)
        try:
            from src.database import dynamo_client as db
            db.update_item("projects", {"projectId": body.projectId}, {"clonedPath": str(target), "clonedBranch": body.branch})
        except Exception:
            pass

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

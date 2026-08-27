"""Shell tools — Tier 1 (run_bash) + Tier 2 (run_tests, run_linter, git_*, install_package, list_packages)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

_STDOUT_CAP = 8_000
_STDERR_CAP = 2_000
_GIT_CAP = 10_000


def _run(cmd: list[str] | str, cwd: str, timeout: int = 30,
         shell: bool = False) -> tuple[str, str, int]:
    """Run a subprocess and return (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=cwd or ".", timeout=timeout, shell=shell,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s", 1
    except FileNotFoundError as e:
        return "", f"Command not found: {e}", 127
    except Exception as e:
        return "", str(e), 1


# ── Tier 1 ───────────────────────────────────────────────────────────────────

def t_run_bash(command: str, workspace_root: str, timeout: int = 30) -> dict:
    stdout, stderr, code = _run(command, cwd=workspace_root or ".", timeout=timeout, shell=True)
    return {
        "command": command,
        "exit_code": code,
        "stdout": stdout[:_STDOUT_CAP],
        "stderr": stderr[:_STDERR_CAP],
        "stdout_truncated": len(stdout) > _STDOUT_CAP,
        "stderr_truncated": len(stderr) > _STDERR_CAP,
    }


# ── Tier 2 ───────────────────────────────────────────────────────────────────

def t_run_tests(path: Path, extra_args: str, workspace_root: str) -> dict:
    """Auto-detect pytest vs jest and run tests."""
    wr = workspace_root or str(path)
    # Detect runner
    has_pytest = (Path(wr) / "pytest.ini").exists() or \
                 (Path(wr) / "pyproject.toml").exists() or \
                 list(Path(wr).glob("**/*.py"))
    has_jest = (Path(wr) / "jest.config.js").exists() or \
               (Path(wr) / "jest.config.ts").exists() or \
               (Path(wr) / "package.json").exists()

    if has_pytest:
        args = ["python", "-m", "pytest", str(path), "--tb=short", "-q"]
        if extra_args:
            args += extra_args.split()
        runner = "pytest"
    elif has_jest:
        args = ["npx", "jest", str(path), "--no-coverage"]
        if extra_args:
            args += extra_args.split()
        runner = "jest"
    else:
        return {"error": "No test runner detected (pytest or jest). Ensure pytest or jest is installed."}

    stdout, stderr, code = _run(args, cwd=wr, timeout=120)
    return {
        "runner": runner,
        "exit_code": code,
        "passed": code == 0,
        "output": (stdout + stderr)[:_STDOUT_CAP],
        "truncated": len(stdout + stderr) > _STDOUT_CAP,
    }


def t_run_linter(path: Path, linter: str, workspace_root: str) -> dict:
    wr = workspace_root or str(path.parent)
    ext = path.suffix.lower() if path.is_file() else ""

    # Auto-detect linter
    if not linter:
        if ext in (".py", "") and (Path(wr) / "mypy.ini").exists() or \
           (Path(wr) / "setup.cfg").exists() or (Path(wr) / "pyproject.toml").exists():
            linter = "mypy"
        elif ext in (".py", ""):
            linter = "pylint"
        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            linter = "eslint"
        else:
            linter = "mypy"  # default

    if linter == "mypy":
        args = ["python", "-m", "mypy", str(path), "--show-error-codes", "--no-error-summary"]
    elif linter == "pylint":
        args = ["python", "-m", "pylint", str(path), "--output-format=text"]
    elif linter == "eslint":
        args = ["npx", "eslint", str(path), "--format=compact"]
    else:
        return {"error": f"Unknown linter '{linter}'. Use mypy, pylint, or eslint."}

    stdout, stderr, code = _run(args, cwd=wr, timeout=60)
    output = (stdout + stderr)[:_STDOUT_CAP]
    return {
        "linter": linter,
        "path": str(path),
        "exit_code": code,
        "clean": code == 0,
        "output": output,
        "truncated": len(stdout + stderr) > _STDOUT_CAP,
    }


def t_git_diff(path: Optional[Path], workspace_root: str) -> dict:
    wr = workspace_root or "."
    args = ["git", "diff", "HEAD"]
    if path:
        args += ["--", str(path)]
    stdout, stderr, code = _run(args, cwd=wr, timeout=30)
    if code != 0:
        return {"error": stderr or "git diff failed"}
    diff = stdout[:_GIT_CAP]
    return {
        "diff": diff,
        "truncated": len(stdout) > _GIT_CAP,
        "empty": not stdout.strip(),
    }


def t_git_log(limit: int, path: Optional[Path], workspace_root: str) -> dict:
    wr = workspace_root or "."
    args = ["git", "log", f"--max-count={limit}",
            "--pretty=format:%H|%an|%ad|%s", "--date=short"]
    if path:
        args += ["--", str(path)]
    stdout, stderr, code = _run(args, cwd=wr, timeout=30)
    if code != 0:
        return {"error": stderr or "git log failed"}
    commits = []
    for line in stdout.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "hash": parts[0][:8],
                "author": parts[1],
                "date": parts[2],
                "message": parts[3],
            })
    return {"commits": commits, "count": len(commits)}


def t_git_blame(path: Path, line: int, workspace_root: str) -> dict:
    wr = workspace_root or str(path.parent)
    args = ["git", "blame", f"-L{line},{line}", "--porcelain", str(path)]
    stdout, stderr, code = _run(args, cwd=wr, timeout=30)
    if code != 0:
        return {"error": stderr or "git blame failed"}
    info: dict = {"file": str(path), "line": line}
    for bline in stdout.splitlines():
        if bline.startswith("author "):
            info["author"] = bline[7:]
        elif bline.startswith("author-time "):
            import datetime
            ts = int(bline[12:])
            info["date"] = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        elif bline.startswith("summary "):
            info["commit_message"] = bline[8:]
        elif len(bline) == 40 and all(c in "0123456789abcdef" for c in bline):
            info["commit"] = bline[:8]
    return info


def t_install_package(package: str, manager: str, workspace_root: str) -> dict:
    wr = workspace_root or "."
    if not manager:
        manager = "npm" if (Path(wr) / "package.json").exists() else "pip"
    if manager == "pip":
        args = ["python", "-m", "pip", "install", package]
    elif manager == "npm":
        args = ["npm", "install", package]
    else:
        return {"error": f"Unknown package manager '{manager}'. Use pip or npm."}
    stdout, stderr, code = _run(args, cwd=wr, timeout=120)
    return {
        "manager": manager,
        "package": package,
        "exit_code": code,
        "success": code == 0,
        "output": (stdout + stderr)[:_STDOUT_CAP],
    }


def t_list_packages(workspace_root: Path) -> dict:
    result: dict = {}
    req = workspace_root / "requirements.txt"
    if req.exists():
        result["requirements_txt"] = req.read_text(encoding="utf-8").splitlines()
    pkg = workspace_root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            result["package_json"] = {
                "dependencies": data.get("dependencies", {}),
                "devDependencies": data.get("devDependencies", {}),
            }
        except Exception:
            result["package_json"] = {"error": "Could not parse package.json"}
    if not result:
        return {"message": "No requirements.txt or package.json found in workspace root"}
    return result

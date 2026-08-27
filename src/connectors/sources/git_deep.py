"""Git deep connector — extracts commit history, PRs, and code authorship."""
from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Any
from ..base import AbstractConnector, SyncResult


class GitDeepConnector(AbstractConnector):
    """
    Config keys:
      repo_path: str              local git repository path
      branch: str                 default HEAD
      max_commits: int            default 500
      github_token: str | None    for PR/issue enrichment
      github_repo: str | None     e.g. 'owner/repo'
    """

    def test_connection(self) -> tuple[bool, str]:
        repo = self.config.get("repo_path", ".")
        try:
            result = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--git-dir"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0, result.stdout.strip() or result.stderr.strip()
        except Exception as exc:
            return False, str(exc)

    def sync(self) -> SyncResult:
        result = SyncResult()
        commits = self._fetch_commits()
        result.entities_added = len(commits)
        return result

    def get_metadata(self) -> list[dict[str, Any]]:
        return self._fetch_commits()[:5]

    def _fetch_commits(self) -> list[dict[str, Any]]:
        repo = self.config.get("repo_path", ".")
        branch = self.config.get("branch", "HEAD")
        max_c = self.config.get("max_commits", 500)
        fmt = "%H%x1f%an%x1f%ae%x1f%ai%x1f%s"
        try:
            out = subprocess.run(
                ["git", "-C", repo, "log", branch, f"--max-count={max_c}",
                 f"--format={fmt}"],
                capture_output=True, text=True, timeout=30,
            )
            commits = []
            for line in out.stdout.strip().splitlines():
                parts = line.split("\x1f")
                if len(parts) == 5:
                    commits.append({
                        "hash": parts[0], "author": parts[1],
                        "email": parts[2], "date": parts[3], "message": parts[4],
                    })
            return commits
        except Exception:
            return []

"""Read the actual source tree, so the strategy is grounded in files that exist.

The strategy agent was given the knowledge graph and nothing else. That works when a
parser has already turned the source into graph nodes — but for an unparsed platform
it means the agent is asked to plan a migration of an application it cannot see, and
the honest response to that is the one it gave: refuse to list components and ask for
file paths instead.

    "the knowledge graph contains zero WorkFusion process definitions,
     no source files, and no parsed artifacts"

So the generic path reads the tree directly. A parser, when one exists, makes this
sharper — it will not make it unnecessary, because the whole premise of "any stack to
any stack" is working on stacks nobody wrote a parser for.

What this deliberately does NOT do is send the whole repository. A prompt stuffed with
every file crowds out the ones that matter and costs tokens to do it, so files are
ranked by whether the profile says they identify the source platform, and excerpts are
capped.
"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Enough of a file to see its shape without pasting the repository into a prompt.
EXCERPT_CHARS = 6000
MAX_EXCERPTS = 12
MAX_LISTED = 400
MAX_FILE_BYTES = 500_000

# Never worth reading: build output, dependencies, binaries.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "target", "build", "dist",
    ".venv", "venv", ".idea", ".vscode", "out", "bin", "obj",
}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".gz",
    ".jar", ".war", ".class", ".pyc", ".so", ".dll", ".exe", ".woff", ".woff2",
    ".mp4", ".lock",
}


def project_root(project_id: str) -> Path | None:
    """Where this project's working copy lives, or None."""
    try:
        from src.database.dynamo_client import scan_items
        for row in scan_items("projects", limit=500):
            if row.get("projectId") == project_id:
                path = row.get("clonedPath") or ""
                if path and Path(path).is_dir():
                    return Path(path)
                return None
    except Exception as exc:  # noqa: BLE001
        log.warning("could not resolve workspace for %s: %s", project_id, exc)
    return None


def _interesting(rel: str, patterns: tuple[str, ...]) -> bool:
    """Does this file match one of the profile's source-identifying globs?"""
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(f"/{rel}", p)
               for p in patterns)


def inventory(project_id: str, detect: tuple[str, ...] = ()) -> dict:
    """A compact picture of the source tree for the strategy prompt.

    Returns file paths, a count by extension, and excerpts of the files most likely
    to define the application's behaviour. Empty dict when there is no working copy —
    the caller then falls back to the graph, and the agent says plainly that it could
    not see the source rather than inventing components.
    """
    root = project_root(project_id)
    if root is None:
        log.info("no workspace on disk for %s — strategy will rely on the graph", project_id)
        return {}

    files: list[str] = []
    by_ext: dict[str, int] = {}
    candidates: list[tuple[int, str, Path]] = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue

        rel = str(path.relative_to(root))
        if len(files) < MAX_LISTED:
            files.append(rel)
        by_ext[path.suffix.lower() or "(none)"] = by_ext.get(path.suffix.lower() or "(none)", 0) + 1

        # Rank: files the profile says identify this platform first, then other
        # source, then everything else. Without this the excerpt budget goes to
        # whatever rglob happened to reach first — usually a README.
        if detect and _interesting(rel, detect):
            rank = 0
        elif path.suffix.lower() in (".xml", ".bpmn", ".groovy", ".java", ".py",
                                     ".js", ".ts", ".yaml", ".yml", ".json", ".sql"):
            rank = 1
        else:
            rank = 2
        candidates.append((rank, rel, path))

    excerpts = []
    for _rank, rel, path in sorted(candidates, key=lambda c: (c[0], c[1]))[:MAX_EXCERPTS]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpts.append({
            "path": rel,
            "truncated": len(text) > EXCERPT_CHARS,
            "content": text[:EXCERPT_CHARS],
        })

    return {
        "root": str(root),
        "fileCount": sum(by_ext.values()),
        "byExtension": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])[:20]),
        "files": files,
        "excerpts": excerpts,
        # Said out loud so the agent knows the listing is partial rather than
        # concluding the application is smaller than it is.
        "listingTruncated": sum(by_ext.values()) > len(files),
    }

"""Ship a project's working copy to whoever is going to test it.

A self-hosted runner executes runs on a developer machine, and `appserver` starts the
application under test from a working copy on that machine's filesystem. So a project
analysed inside a deployed container is untestable from a runner: its code lives on the
EFS `/workspace` volume and nowhere else.

The obvious fix is "clone it" — and it does not work. A project created by UPLOADING
code has no git remote recorded anywhere: its `Repository` nodes have `url = None`, and
its connectors record `repoUrl` as a container path like
`/workspace/<projectId>/backend`. There is nothing to clone from.

So Aura ships its own copy instead. That is better anyway: the knowledge graph the plan
comes from was built from exactly this code, and re-cloning a branch that has moved on
would produce failures for cases the plan never described.

Packaged without dependencies or history — `node_modules`, `.venv` and `.git` are the
bulk of a checkout and the runner installs its own (see qatest/provision.py). test1 is
592 KB of source and about 40 MB of node_modules.
"""
from __future__ import annotations

import hashlib
import io
import logging
import tarfile
from pathlib import Path

log = logging.getLogger(__name__)

BUCKET = "test-artifacts"

#: Never shipped. Dependencies are reinstalled on the runner, history is not needed to
#: run anything, and build output would shadow a fresh build.
EXCLUDE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", ".next", "target", "vendor",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store",
}

#: A working copy larger than this is refused rather than shipped. A repo that big
#: almost always means something that should have been excluded was not, and silently
#: pushing hundreds of megabytes through S3 on every run is not a good surprise.
MAX_BYTES = 200 * 1024 * 1024

URL_TTL_S = 3600


def _skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = set(Path(info.name).parts)
    if parts & EXCLUDE:
        return None
    # Symlinks could point anywhere on the source filesystem; a runner extracting one
    # would either break or read something it should not.
    if info.issym() or info.islnk():
        return None
    if not (info.isfile() or info.isdir()):
        return None
    # Ownership is meaningless on the far side and would make the archive
    # non-reproducible, which defeats the content hash below.
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def local_root(project_id: str) -> Path | None:
    """Where this process keeps the project's working copy, if it has one."""
    from src.config_settings import get_settings

    base = Path(get_settings().aura_workspace or "/workspace")
    root = (base / project_id).resolve()
    return root if root.is_dir() else None


def package(project_id: str) -> tuple[bytes, str] | None:
    """Tar-gzip the working copy. Returns (bytes, sha256) or None if there is none."""
    root = local_root(project_id)
    if root is None:
        return None

    buffer = io.BytesIO()
    # mtime=0 and a fixed gzip mtime so identical source produces an identical
    # archive — that is what makes the hash usable as a cache key on the runner.
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as tar:
        tar.add(str(root), arcname=".", filter=_skip, recursive=True)

    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        log.warning("QA workspace for %s is %.1f MB, over the %.0f MB limit — not shipped",
                    project_id, len(data) / 1e6, MAX_BYTES / 1e6)
        return None
    return data, hashlib.sha256(data).hexdigest()


def publish(project_id: str) -> dict | None:
    """Package the working copy, store it, and return a presigned URL for the runner.

    A PRESIGNED GET, deliberately: it carries its own authorisation, so the runner needs
    no S3 permission for this object and the per-run session policy stays scoped to the
    run's own evidence prefix.

    Keyed by content hash, so an unchanged project is re-uploaded but the runner can skip
    the download entirely when it already has that hash extracted.
    """
    from src.storage import s3_client

    packaged = package(project_id)
    if packaged is None:
        return None
    data, digest = packaged

    key = f"{project_id}/_workspace/{digest}.tar.gz"
    try:
        s3_client.put_object(BUCKET, key, data, "application/gzip")
        url = s3_client.presigned_url(BUCKET, key, expires=URL_TTL_S)
    except Exception as exc:                                  # noqa: BLE001
        # The run can still proceed against an explicit app_url, so this must not be
        # fatal — the claim simply carries no workspace and the runner says why.
        log.warning("QA workspace for %s could not be published: %s", project_id, exc)
        return None

    log.info("QA workspace for %s published: %.1f KB, sha %s",
             project_id, len(data) / 1024, digest[:12])
    return {"url": url, "sha256": digest, "bytes": len(data)}

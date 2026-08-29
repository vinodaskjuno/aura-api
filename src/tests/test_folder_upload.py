"""Folder upload — the wizard's "Local Folder" picker.

The browser cannot give the server a filesystem path, so the folder is posted as
multipart and rebuilt server-side. `webkitRelativePath` is browser-supplied and
therefore untrusted in exactly the way a zip entry is, so the traversal cases
below are the point of this file, not an afterthought.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.routers import git_ops
from src.routers.auth import get_current_user
from src.services.auth_service import ROLE_PERMISSIONS

client = TestClient(app)

DEV = {"userId": "u2", "username": "dev", "role": "user_dev",
       "permissions": ROLE_PERMISSIONS["user_dev"]}


@pytest.fixture(autouse=True)
def _auth_and_workspace(tmp_path, monkeypatch):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: DEV
    # _WORKSPACE_ROOT is bound at import, so patch the module attribute.
    monkeypatch.setattr(git_ops, "_WORKSPACE_ROOT", tmp_path / "ws")
    yield
    if previous is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previous


def _post(project_id: str, label: str, tree: list[tuple[str, bytes]]):
    return client.post(
        "/api/git/upload-folder",
        data={"projectId": project_id, "label": label,
              "paths": [p for p, _ in tree]},
        files=[("files", (p.split("/")[-1], body)) for p, body in tree],
    )


BACKEND = [
    ("app/main.py", b"from fastapi import FastAPI\napp = FastAPI()\n"),
    ("app/services/pricing.py", b"TIERS = []\n"),
    ("requirements.txt", b"fastapi\n"),
]


def test_rebuilds_the_tree_and_reports_real_counts(fake_dynamo):
    r = _post("p1", "backend", BACKEND)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["fileCount"] == 3
    assert body["label"] == "backend"

    dest = git_ops._WORKSPACE_ROOT / "p1" / "backend"
    assert (dest / "app" / "main.py").read_bytes() == BACKEND[0][1]
    assert (dest / "app" / "services" / "pricing.py").exists()
    assert (dest / "requirements.txt").exists()


def test_workspace_is_git_backed_so_devmate_tools_attach(fake_dynamo):
    """_resolve_project_dir gates on `.git`; without it no file tool is offered."""
    _post("p2", "backend", BACKEND)
    root = git_ops._WORKSPACE_ROOT / "p2"
    assert (root / ".git").is_dir()

    from src.services.advisor import tools
    tools._WORKSPACE_ROOT = git_ops._WORKSPACE_ROOT
    assert tools._resolve_project_dir("p2") == root


def test_multiple_folders_coexist_under_one_project(fake_dynamo):
    _post("p3", "backend", BACKEND)
    _post("p3", "frontend", [("src/App.tsx", b"export default () => null\n"),
                             ("package.json", b'{"name":"f"}\n')])
    root = git_ops._WORKSPACE_ROOT / "p3"
    assert (root / "backend" / "app" / "main.py").exists()
    assert (root / "frontend" / "src" / "App.tsx").exists()


def test_reupload_replaces_that_label_only(fake_dynamo):
    _post("p4", "backend", BACKEND)
    _post("p4", "frontend", [("index.html", b"<html></html>")])
    _post("p4", "backend", [("app/main.py", b"# rewritten\n")])

    root = git_ops._WORKSPACE_ROOT / "p4"
    # The stale file is gone — merging would leave it to be analysed as current.
    assert not (root / "backend" / "requirements.txt").exists()
    assert (root / "backend" / "app" / "main.py").read_bytes() == b"# rewritten\n"
    # The sibling label is untouched.
    assert (root / "frontend" / "index.html").exists()


# ── Traversal: webkitRelativePath is untrusted ───────────────────────────────

@pytest.mark.parametrize("evil", [
    "../../../etc/passwd",
    "app/../../escape.py",
    "/etc/passwd",
    "C:\\Windows\\system32\\evil.dll",
    "..",
])
def test_traversal_attempts_never_escape_the_destination(evil, fake_dynamo, tmp_path):
    r = _post("p5", "backend", [(evil, b"pwned"), ("ok.py", b"fine\n")])
    assert r.status_code == 201, r.text
    assert r.json()["fileCount"] == 1          # only ok.py
    assert r.json()["skippedCount"] == 1

    root = git_ops._WORKSPACE_ROOT
    escaped = [p for p in root.parent.rglob("*")
               if p.is_file() and p.read_bytes() == b"pwned"]
    assert escaped == [], f"traversal wrote outside the destination: {escaped}"


def test_dependency_and_build_output_are_filtered(fake_dynamo):
    r = _post("p6", "backend", [
        ("node_modules/react/index.js", b"junk"),
        ("__pycache__/main.cpython-312.pyc", b"junk"),
        ("dist/bundle.js", b"junk"),
        (".venv/lib/thing.py", b"junk"),
        ("app/main.py", b"real\n"),
    ])
    assert r.status_code == 201
    assert r.json()["fileCount"] == 1
    assert r.json()["skippedCount"] == 4
    root = git_ops._WORKSPACE_ROOT / "p6" / "backend"
    assert not (root / "node_modules").exists()
    assert (root / "app" / "main.py").exists()


def test_all_junk_is_a_clear_400_not_an_empty_success(fake_dynamo):
    r = _post("p7", "backend", [("node_modules/a.js", b"x"), ("dist/b.js", b"y")])
    assert r.status_code == 400
    assert "build output" in r.json()["detail"]
    assert not (git_ops._WORKSPACE_ROOT / "p7" / "backend").exists()


# ── Argument validation ──────────────────────────────────────────────────────

def test_paths_files_mismatch_is_rejected(fake_dynamo):
    r = client.post("/api/git/upload-folder",
                    data={"projectId": "p8", "label": "backend",
                          "paths": ["a.py", "b.py"]},
                    files=[("files", ("a.py", b"x"))])
    assert r.status_code == 400
    assert "mismatch" in r.json()["detail"]


def test_label_cannot_escape_the_project_directory(fake_dynamo):
    r = _post("p9", "../../evil", BACKEND)
    assert r.status_code == 201
    assert r.json()["label"] == "evil"
    assert (git_ops._WORKSPACE_ROOT / "p9" / "evil").is_dir()


def test_blank_project_id_is_refused(fake_dynamo):
    """`_WORKSPACE_ROOT / ""` is the workspace root — every project at once."""
    assert _post("", "backend", BACKEND).status_code == 400


def test_oversized_upload_is_refused_and_leaves_nothing_behind(fake_dynamo, monkeypatch):
    monkeypatch.setattr(git_ops, "_UPLOAD_MAX_BYTES", 1024)
    r = _post("p10", "backend", [("big.py", b"x" * 2048)])
    assert r.status_code == 413
    assert "MB" in r.json()["detail"]
    assert not (git_ops._WORKSPACE_ROOT / "p10" / "backend").exists()

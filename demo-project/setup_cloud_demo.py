#!/usr/bin/env python3
"""Create the aura-cloud-demo project in a running Aura, ready to test.

Four steps, in the order Aura needs them, because none of the existing helpers does the
whole job: `setup_demo.py` writes DynamoDB rows directly and never clones or analyses, so
the project it makes has no code on disk and QualityMind can plan against it but never
execute it.

    create project -> add connector -> clone the code -> analyse

The code is UPLOADED rather than cloned, so the same command works against a laptop
backend and a deployed one — a deployed API cannot read a path on your disk.

    export AURA_PASSWORD=…                       # once per shell
    python demo-project/setup_cloud_demo.py --api http://localhost:8000 --user admin

The password is read from --password, then $AURA_PASSWORD, then a no-echo prompt. It is
never defaulted to a literal: a credential in a tracked file outlives every rotation.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "aura-cloud-demo"


def call(api: str, path: str, token: str = "", body: dict | None = None,
         method: str = "") -> dict:
    """One JSON request. Raises with the server's own message, which is usually the
    actionable one — a bare HTTPError says nothing about what was wrong."""
    url = f"{api.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"))
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read().decode() or "{}"
            return json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise SystemExit(f"\n{method or 'POST'} {path} failed ({exc.code}): {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"\nCould not reach {url}: {exc.reason}")


#: Never shipped. Dependencies are reinstalled on the runner and history is not needed
#: to run anything — the same exclusions qatest/workspace.py applies when it packages a
#: working copy for a runner.
UPLOAD_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
               ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store"}


def upload_folder(api: str, token: str, project_id: str, root: Path,
                  label: str) -> dict:
    """Stream a directory to the API as multipart form data.

    The remote path. `git clone file:///…` only works when the API can read that path
    itself, which is true for a backend on this laptop and false for one on Fargate —
    it would try to clone a directory that does not exist over there. This sends the
    bytes instead, so it works wherever the API is.

    Built by hand rather than with `requests` so the script keeps to the standard
    library, like the rest of it.
    """
    import mimetypes
    import uuid as _uuid

    files = [f for f in sorted(root.rglob("*"))
             if f.is_file() and not (set(f.relative_to(root).parts) & UPLOAD_SKIP)]
    if not files:
        raise SystemExit(f"nothing to upload from {root}")

    boundary = f"----aura{_uuid.uuid4().hex}"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode())

    field("projectId", project_id)
    field("label", label)
    for f in files:
        # `paths` and `files` are positional partners: the Nth path names the Nth file,
        # which is what lets the server rebuild the tree. POSIX separators regardless of
        # platform, because the far side joins them onto its own workspace path.
        field("paths", f.relative_to(root).as_posix())
    for f in files:
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
            f"filename=\"{f.name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
        parts.append(f.read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/git/upload-folder", data=b"".join(parts), method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read().decode() or "{}"
            return json.loads(raw) if raw.strip().startswith("{") else {}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"\nupload-folder failed ({exc.code}): "
                         f"{exc.read().decode()[:400]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--user", default="admin")
    # Not required, and never defaulted to a literal. A password written into a tracked
    # file lives in git history permanently — rotating it later does not remove it — and
    # it is the exact pattern this demo's own IA-5(1) control exists to flag.
    ap.add_argument("--password", default="",
                    help="defaults to $AURA_PASSWORD; prompts if neither is set")
    ap.add_argument("--name", default="Aura Cloud Demo")
    args = ap.parse_args()

    args.password = args.password or os.environ.get("AURA_PASSWORD", "")
    if not args.password:
        if not sys.stdin.isatty():
            raise SystemExit(
                "no password: pass --password, or set AURA_PASSWORD. Refusing to prompt "
                "because stdin is not a terminal — in CI, set the variable.")
        # getpass, not input(): it does not echo and does not reach shell history.
        args.password = getpass.getpass(f"Password for {args.user}: ")

    if not (APP / "backend" / "app" / "main.py").exists():
        raise SystemExit(f"demo app not found at {APP}")


    print(f"▶ signing in to {args.api} as {args.user}")
    auth = call(args.api, "/auth/login",
                body={"username": args.user, "password": args.password})
    token = auth.get("access_token") or auth.get("token") or ""
    if not token:
        raise SystemExit(f"no token in the login response: {list(auth)}")

    # Created with NO repos. The connector has to name a path inside the workspace, and
    # that path does not exist until the clone below has run — registering it first
    # produces a connector with an empty localPath, which analysis silently finds
    # nothing in. That is exactly what an earlier version of this script did: it
    # reported success and left a project with a Project node and nothing else.
    print("▶ creating the project")
    project = call(args.api, "/api/projects", token, {
        "name": args.name,
        "description": "Exercises S3, DynamoDB, SQS and SNS against local Floci "
                       "emulators, so a test run has cloud resources to show.",
        "repos": [],
    })
    project_id = project.get("projectId") or project.get("id") or ""
    if not project_id:
        raise SystemExit(f"no projectId in the response: {list(project)}")
    print(f"  projectId = {project_id}")

    # ALWAYS upload, never clone. `git clone file:///…` only works when the API can read
    # this disk — true for a laptop backend, false for one on Fargate — so it needed a
    # second code path AND a git repo inside the demo app. That nested repo made the outer
    # checkout record the demo as a gitlink, which stores no contents: a fresh clone of
    # aura-api got an empty directory. Uploading works everywhere, so the fork and the
    # repo both go.
    print("▶ uploading the code")
    uploaded = upload_folder(args.api, token, project_id, APP / "backend", "backend")
    # `localPath` is where the server rebuilt the tree. Asking beats assuming: the
    # workspace root is /workspace in a container and ./data/workspace on a laptop, so a
    # computed path would produce a connector pointing nowhere.
    backend = uploaded.get("localPath") or ""
    if not backend:
        raise SystemExit(f"upload-folder returned no localPath: {uploaded}")
    print(f"  uploaded {uploaded.get('fileCount', '?')} file(s) to {backend}")

    # Now the connector, pointing at the BACKEND directory inside the clone — the shape
    # the analyser actually reads: repoType "local", and repoUrl AND localPath both set
    # to an absolute path that exists. Anything else analyses to an empty graph, and an
    # empty graph means no dependencies, so no emulator, so nothing to demonstrate.
    print("▶ registering the backend as a local connector")
    call(args.api, f"/api/projects/{project_id}/connectors", token, {
        "repoType": "local", "sourceType": "local",
        "repoUrl": backend, "localPath": backend, "branch": "main"})

    print("▶ analysing — this builds the graph the test plan comes from")
    call(args.api, f"/api/projects/{project_id}/analyse", token, {})

    # Verify rather than claim. "Analysed" with an empty graph looks like success and
    # fails three steps later in the UI, where the cause is no longer visible.
    facts = call(args.api, f"/api/qa/projects/{project_id}/plan", token, method="GET")
    clouds = facts.get("clouds") or []
    cases = facts.get("totalCases", 0)
    if not clouds:
        raise SystemExit(
            f"\nAnalysis produced no cloud dependencies for {project_id}.\n"
            f"Without them no Floci emulator starts and there is nothing to show.\n"
            f"Check that {backend}/requirements.txt lists boto3 and that the analyser "
            f"read it.")
    print(f"  clouds = {clouds}   planned cases = {cases}")

    print(f"""
✓ ready.

  Project : {args.name}
  Id      : {project_id}

Next, in the UI:
  1. DevMate     — press Start on the Floci control to bring up the emulators.
  2. QualityMind — run the tests. They will adopt the emulators you started.
  3. The run's "Cloud resources" panel lists what the tests touched.
  4. Runner tab  — Inspect the still-running emulator to see the same rows live.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())

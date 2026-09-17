"""Prepare a runner machine to run a project's application.

Runs on the RUNNER, before `service.execute`. Three steps, and the third is the one that
actually costs time:

  1. download the working copy Aura shipped (qatest/workspace.py) and extract it
  2. `npm ci` / `npm install` where there is a package.json
  3. create a per-project venv and `pip install -r requirements.txt`

Step 3 exists because `appserver` starts an API with the interpreter running the process
and does not install anything, so a project needing a package the runner happens not to
have fails at readiness with nothing useful in the log. Step 2 exists because
`appserver` explicitly refuses to run `npm install` itself — it marks the app blocked,
which is the right call for a synchronous local run and the wrong one when nobody is
watching.

Both are cached on the LOCKFILE hash, not the workspace hash: source changes on every
commit, dependencies change rarely, and a cold `npm ci` is minutes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

log = logging.getLogger("qa-runner.provision")

INSTALL_TIMEOUT_S = 900
DOWNLOAD_TIMEOUT_S = 120

#: Directories that are dependency output, keyed by the lockfile that determines them.
_NODE_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "package.json")
_PY_LOCKS = ("requirements.txt", "requirements-dev.txt", "pyproject.toml")


def workspace_root(project_id: str) -> Path:
    from src.config_settings import get_settings

    base = Path(get_settings().aura_workspace or "./data/workspace")
    return (base / project_id).resolve()


def _stamp(path: Path) -> Path:
    return path / ".aura-provisioned"


def _lock_hash(directory: Path, names: tuple[str, ...]) -> str:
    """Hash whichever lockfiles exist, so the cache key tracks dependencies only."""
    h = hashlib.sha256()
    found = False
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            h.update(name.encode())
            h.update(candidate.read_bytes())
            found = True
    return h.hexdigest()[:16] if found else ""


def fetch(project_id: str, workspace: dict, emit=None) -> Path | None:
    """Download and extract the shipped working copy. Returns its root, or None.

    Skips the download when the same content hash is already extracted — the common
    case for repeated runs, and the reason `publish` keys on content.
    """
    def say(message: str) -> None:
        log.info("  %s", message)
        if emit:
            emit({"type": "provision", "message": message})

    root = workspace_root(project_id)
    digest = workspace.get("sha256", "")
    marker = root / ".aura-workspace-sha"

    if digest and marker.is_file() and marker.read_text().strip() == digest:
        say(f"working copy already current ({digest[:12]})")
        return root

    import httpx

    say(f"fetching working copy ({workspace.get('bytes', 0) / 1024:.0f} KB)")
    try:
        response = httpx.get(workspace["url"], timeout=DOWNLOAD_TIMEOUT_S,
                             follow_redirects=True)
        response.raise_for_status()
    except Exception as exc:                                  # noqa: BLE001
        say(f"could not fetch the working copy: {type(exc).__name__}: {exc}")
        return None

    # Replace the SOURCE but keep dependency directories: they are excluded from the
    # archive and reinstalling them every run is exactly what the cache avoids.
    if root.exists():
        for child in root.iterdir():
            if child.name in ("node_modules", ".venv") or child.name.startswith(".aura-"):
                continue
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
    root.mkdir(parents=True, exist_ok=True)

    try:
        import io
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
            _safe_extract(tar, root)
    except Exception as exc:                                  # noqa: BLE001
        say(f"could not extract the working copy: {type(exc).__name__}: {exc}")
        return None

    marker.write_text(digest)
    say(f"working copy ready at {root}")
    return root


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract, refusing any member that would escape `dest`.

    The archive is produced by Aura's own packager, which already drops symlinks and
    absolute paths — but an extractor that trusts its input is a path-traversal bug
    waiting for the day something else writes the archive.
    """
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"archive member escapes the destination: {member.name}")
    tar.extractall(dest)  # noqa: S202 — every member checked above


def _run(command: list[str], cwd: Path, say) -> bool:
    from src.qatest import toolpath

    say(f"$ {' '.join(command[:4])}… in {cwd.name}/")
    try:
        # Augmented, so the "not on PATH" message below is only ever printed when the
        # tool is genuinely absent rather than merely installed somewhere this
        # process cannot see.
        result = subprocess.run(command, cwd=str(cwd), capture_output=True,
                                text=True, timeout=INSTALL_TIMEOUT_S,
                                env=toolpath.env())
    except subprocess.TimeoutExpired:
        say(f"timed out after {INSTALL_TIMEOUT_S}s: {' '.join(command[:3])}")
        return False
    except FileNotFoundError:
        say(f"{command[0]} is not on PATH")
        return False
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        say(f"failed ({result.returncode}): {' / '.join(tail)[:300]}")
        return False
    return True


def _app_dirs(root: Path) -> list[Path]:
    """The directories that might hold an installable app.

    The root and its immediate children — and, when the root holds exactly ONE directory
    and nothing installable of its own, that directory's children too.

    That last step exists because uploading a FOLDER through the UI keeps the folder, so
    the app lands at `<root>/<name>/backend` rather than `<root>/backend`. Without it
    `install` walks right past the project and installs nothing, and the failure is
    invisible: `appserver` falls back to `sys.executable` when a project has no venv
    (appserver.py:149-150), so the app boots on the AGENT's own interpreter and works
    right up until a project needs a dependency the agent happens not to have.

    Limited to a single wrapper, like `appserver.detect`: with two sibling directories
    there is nothing to disambiguate them, and descending would install trees that are
    not the app.
    """
    def children(base: Path) -> list[Path]:
        return [d for d in sorted(base.iterdir()) if d.is_dir()
                and not d.name.startswith(".")
                and d.name not in ("node_modules", "dist", "build")]

    if not root.is_dir():
        return []
    found = [root] + children(root)
    installable = [d for d in found
                   if (d / "package.json").is_file() or (d / "requirements.txt").is_file()]
    if not installable and len(found) == 2:
        # found == [root, the single wrapper]
        return found + children(found[1])
    return found


def install(root: Path, emit=None) -> list[str]:
    """Install dependencies for every app directory under `root`. Returns problems.

    Best-effort by design: a failed install is reported and the run continues, because
    a partially testable app beats no run at all — `appserver` will mark whatever
    cannot start as blocked and the report will say so.
    """
    def say(message: str) -> None:
        log.info("  %s", message)
        if emit:
            emit({"type": "provision", "message": message})

    problems: list[str] = []
    candidates = _app_dirs(root)

    for directory in candidates:
        # ── Node ─────────────────────────────────────────────────────────────
        package_json = directory / "package.json"
        if package_json.is_file():
            key = _lock_hash(directory, _NODE_LOCKS)
            stamp = directory / ".aura-node-deps"
            fresh = (stamp.is_file() and stamp.read_text().strip() == key
                     and (directory / "node_modules").is_dir())
            if fresh:
                say(f"node_modules cached for {directory.name}/")
            else:
                # `npm ci` is right when there is a lockfile — reproducible, and it
                # refuses rather than silently resolving a different tree. Without one
                # it errors, so fall back.
                has_lock = (directory / "package-lock.json").is_file()
                command = ["npm", "ci"] if has_lock else ["npm", "install"]
                if _run(command, directory, say):
                    stamp.write_text(key)
                elif has_lock and _run(["npm", "install"], directory, say):
                    # `npm ci` fails on a lockfile out of sync with package.json, which
                    # is common in a working repo and not worth failing a test run over.
                    stamp.write_text(key)
                else:
                    problems.append(f"npm install failed in {directory.name}/")

        # ── Python ───────────────────────────────────────────────────────────
        requirements = next((directory / n for n in ("requirements.txt",)
                             if (directory / n).is_file()), None)
        if requirements:
            key = _lock_hash(directory, _PY_LOCKS)
            venv = directory / ".venv"
            stamp = directory / ".aura-py-deps"
            fresh = (stamp.is_file() and stamp.read_text().strip() == key
                     and (venv / "bin" / "python").is_file())
            if fresh:
                say(f"venv cached for {directory.name}/")
                continue
            if not (venv / "bin" / "python").is_file():
                if not _run([sys.executable, "-m", "venv", str(venv)], directory, say):
                    problems.append(f"could not create a venv in {directory.name}/")
                    continue
            python = str(venv / "bin" / "python")
            # uvicorn explicitly: appserver runs `-m uvicorn`, and a project can depend
            # on fastapi without listing the server that serves it.
            if _run([python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                    directory, say) and \
               _run([python, "-m", "pip", "install", "--quiet", "-r",
                     str(requirements), "uvicorn"], directory, say):
                stamp.write_text(key)
            else:
                problems.append(f"pip install failed in {directory.name}/")

    return problems


#: What a sidecar installs. Deliberately OpenInference rather than the GenAI-only
#: instrumentors: `aiobs/ingest.py` reads OpenInference's spellings (`input.value`,
#: `llm.token_count.prompt`) alongside `gen_ai.*`, and those are the ones that carry
#: the prompt and the response. Simplifying to the GenAI set would produce span trees
#: with no payloads, which is most of the value gone.
_OTEL_SIDECAR = (
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-instrumentation",
    "opentelemetry-instrumentation-fastapi",
    "openinference-instrumentation-anthropic",
    "openinference-instrumentation-openai",
)

#: Source markers that mean the project already has tracing of its own. Instrumenting
#: on top produces two exporters fighting over one tracer provider.
_OWN_TRACING = ("set_tracer_provider", "TracerProvider(", "logfire", "opik",
                "langsmith", "traceloop", "phoenix", "LANGCHAIN_TRACING_V2")

#: Env that means the same thing. If any is already set, the project is pointed
#: somewhere and it is not our business to repoint it.
_OWN_OTEL_ENV = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                 "OTEL_TRACES_EXPORTER", "OTEL_SDK_DISABLED")

SIDECAR_DIR = ".aura-otel"


def _already_instrumented(directory: Path) -> str:
    """Why we must NOT instrument this app, or "" if we may."""
    for name in _OWN_OTEL_ENV:
        if os.environ.get(name):
            return f"{name} is already set in this environment"
    env_file = directory / ".env"
    if env_file.is_file():
        try:
            body = env_file.read_text(errors="replace")
            for name in _OWN_OTEL_ENV:
                if name in body:
                    return f"{name} is set in {directory.name}/.env"
        except OSError:
            pass

    venv_python = directory / ".venv" / "bin" / "python"
    if venv_python.is_file():
        try:
            probe = subprocess.run(
                [str(venv_python), "-c", "import opentelemetry.sdk"],
                capture_output=True, timeout=30)
            if probe.returncode == 0:
                # THE IMPORTANT ONE. A sidecar on PYTHONPATH precedes site-packages,
                # so shipping a newer opentelemetry-api would shadow the project's own
                # pinned copy and break it in a way that points nowhere near Aura.
                return "this project already has the OpenTelemetry SDK installed"
        except Exception:                                     # noqa: BLE001
            pass

    for source in list(directory.rglob("*.py"))[:400]:
        try:
            body = source.read_text(errors="replace")
        except OSError:
            continue
        for marker in _OWN_TRACING:
            if marker in body:
                return f"this project sets up its own tracing ({marker})"
    return ""


def instrument(root: Path, env: dict | None = None) -> tuple[Path | None, str]:
    """Build the OpenTelemetry sidecar for this project. Returns (path, why-not).

    A SIDECAR DIRECTORY, NEVER THE PROJECT'S VENV. `opentelemetry-bootstrap -a install`
    resolves against the project's own pins and writes into their environment — the
    breakage is permanent, and `.aura-py-deps` would still read "fresh" so the next
    run would not repair it. `pip install --target` keeps the app on its own
    interpreter and its own site-packages, with the instrumentation riding on
    PYTHONPATH for exactly one process.

    Named `.aura-otel` because `fetch` preserves every child starting with `.aura-`;
    anything else would be deleted by the next refresh.
    """
    root = Path(root)
    directories = [d for d in _app_dirs(root)
                   if (d / "requirements.txt").is_file() or (d / "pyproject.toml").is_file()]
    if not directories:
        return None, "no Python application here to instrument"

    directory = directories[0]
    refusal = _already_instrumented(directory)
    if refusal:
        return None, refusal

    sidecar = root / SIDECAR_DIR
    key = ",".join(_OTEL_SIDECAR)
    stamp = sidecar / ".stamp"
    if stamp.is_file() and stamp.read_text().strip() == key:
        return sidecar, ""

    python = str(directory / ".venv" / "bin" / "python")
    if not Path(python).is_file():
        python = sys.executable
    sidecar.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(
            [python, "-m", "pip", "install", "--quiet", "--target", str(sidecar),
             *_OTEL_SIDECAR],
            capture_output=True, text=True, timeout=600, cwd=str(directory))
    except Exception as exc:                                  # noqa: BLE001
        return None, f"could not install the tracing sidecar: {exc}"[:300]
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip()[-300:]
        return None, f"could not install the tracing sidecar: {tail}"
    stamp.write_text(key)
    return sidecar, ""


def prepare(project_id: str, workspace: dict | None, emit=None) -> list[str]:
    """Fetch and install. Returns problems; an empty list means ready.

    REFUSES WHILE AN APP SESSION IS LIVE. `fetch` rmtree's the source tree and
    `install` can re-run `npm ci`, which deletes node_modules — both underneath a dev
    server that is serving out of that very directory. Every caller reaches here
    unconditionally (`run_one`, `_command_populate`, `_command_app_start`), so the
    guard belongs here rather than at each of them.

    Skipping is the right answer rather than stopping the session: the copy on disk is
    the one the running app was started from, so it is exactly the copy a run should
    test. Saying so is the point — a silent skip would leave someone wondering why
    their new commit is not being picked up.
    """
    if not workspace or not workspace.get("url"):
        return []                      # nothing shipped; app_url runs are unaffected

    try:
        from src.qatest import appsession
        if appsession.get(project_id) is not None:
            return ["this project's app is running locally, so the working copy was "
                    "left alone — refreshing it would replace the source underneath "
                    "the running app. Stop the app to pick up newer code."]
    except Exception:                  # noqa: BLE001 — never block on the guard itself
        pass

    root = fetch(project_id, workspace, emit)
    if root is None:
        return ["the working copy could not be fetched"]
    return install(root, emit)

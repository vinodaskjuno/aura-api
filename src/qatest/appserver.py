"""Start the application under test from the project's own code, then stop it.

So that a run is "test this project", not "test whatever is at this URL". Asking a
person for a URL puts the burden of starting the app, choosing a port, and matching
the right half (API or UI) on them — and gets it wrong silently when they point an
API plan at a frontend.

Detection is deliberately shallow and explicit. It recognises the two shapes AURA's
own analysis already understands — a Python ASGI app and a Node dev server — and
reports anything else as undetected rather than guessing at a start command. A wrong
guess here spawns a process that never serves, and the run then fails for a reason
that looks nothing like the cause.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 60
STOP_GRACE_S = 5

# Directories that never contain an application worth starting.
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
         ".next", "target", "vendor", ".pytest_cache"}


@dataclass
class AppSpec:
    """One runnable application found in a repository."""
    kind: str                 # "api" | "ui"
    name: str
    directory: Path
    command: list[str]
    port: int
    env: dict[str, str]
    blocked: str = ""         # why it cannot start, when it cannot

    @property
    def url(self) -> str:
        # 127.0.0.1, NOT "localhost". The app is bound to 127.0.0.1, but a browser
        # resolving "localhost" may reach ::1 first — and if anything else is
        # listening there on the same port it is tested instead, silently. Caught
        # exactly that: a run reported a pass against AURA's own dev server on
        # [::1]:5174 while the application under test sat on 127.0.0.1:5174.
        return f"http://127.0.0.1:{self.port}"


def port_free(port: int) -> bool:
    """Deliberately WITHOUT SO_REUSEADDR.

    With it set, the bind succeeds on macOS even while another server is listening,
    so the check reports a busy port as free — it claimed 5174 was available while
    AURA's own dev server was serving on it.
    """
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _vite_ports(directory: Path) -> tuple[int | None, int | None]:
    """(dev server port, port its /api proxy targets) from a vite config.

    Read because an application must be started the way it is MEANT to run. This
    frontend proxies /api to a fixed port, so starting the API on an arbitrary free
    port leaves the UI unable to reach it — the page loads, every request 500s, and
    the run reports a failure that is entirely the harness's doing.
    """
    import re as _re
    for name in ("vite.config.ts", "vite.config.js", "vite.config.mjs"):
        path = directory / name
        if not path.exists():
            continue
        try:
            text = path.read_text("utf-8", "replace")
        except OSError:
            continue
        own = _re.search(r"\bport\s*:\s*(\d{2,5})", text)
        target = _re.search(r"target\s*:\s*['\"]https?://[^:'\"]+:(\d{2,5})", text)
        return (int(own.group(1)) if own else None,
                int(target.group(1)) if target else None)
    return (None, None)


def free_port() -> int:
    """An OS-assigned free port.

    Binding to 0 and releasing leaves a small race before the child binds it, but it
    beats a fixed port: 5174 is the demo frontend's configured port AND the one AURA's
    own dev server uses, so a fixed choice collides on the very machine this runs on.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _asgi_target(directory: Path) -> str | None:
    """`module:attr` for a Python ASGI app, or None.

    Checks the conventional locations rather than importing anything — importing to
    find out would execute the project's code before the run has decided to start it.
    """
    for rel in ("app/main.py", "main.py", "src/main.py", "app.py"):
        path = directory / rel
        if not path.exists():
            continue
        try:
            text = path.read_text("utf-8", "replace")
        except OSError:
            continue
        if "FastAPI(" in text or "Flask(" in text or "= FastAPI" in text:
            module = rel[:-3].replace("/", ".")
            return f"{module}:app"
    return None


def detect(root: Path) -> list[AppSpec]:
    """Find the applications in a repository. Deepest-first is not needed: a repo
    holds at most one backend and one frontend in the shapes handled here."""
    found: list[AppSpec] = []
    root = Path(root)

    candidates = [root] + [d for d in sorted(root.iterdir())
                           if d.is_dir() and d.name not in _SKIP] if root.exists() else []

    # A first pass over the frontend, because its config says which port the API is
    # expected on — and the API has to be started there for the two to talk.
    ui_port: int | None = None
    api_expected: int | None = None
    for directory in candidates:
        if (directory / "package.json").exists():
            ui_port, api_expected = _vite_ports(directory)
            if ui_port or api_expected:
                break

    for directory in candidates:
        # ── Python ASGI ──────────────────────────────────────────────────────
        target = _asgi_target(directory)
        if target and not any(a.kind == "api" for a in found):
            port = (api_expected if api_expected and port_free(api_expected)
                    else free_port())
            blocked = ""
            if api_expected and port != api_expected:
                blocked = (f"port {api_expected} is in use, and the UI proxies to it. "
                           f"Free it, or the UI cannot reach the API.")
            found.append(AppSpec(
                kind="api", name=directory.name or "api", directory=directory,
                # sys.executable, not a bare "python": the interpreter running AURA
                # already has fastapi and uvicorn, and a project venv may not exist.
                command=[sys.executable, "-m", "uvicorn", target,
                         "--port", str(port), "--host", "127.0.0.1"],
                port=port, env={}, blocked=blocked))

        # ── Node dev server ──────────────────────────────────────────────────
        pkg = directory / "package.json"
        if pkg.exists() and not any(a.kind == "ui" for a in found):
            try:
                scripts = json.loads(pkg.read_text("utf-8")).get("scripts", {})
            except (OSError, json.JSONDecodeError):
                scripts = {}
            if "dev" in scripts:
                port = ui_port if (ui_port and port_free(ui_port)) else free_port()
                blocked = ""
                if not (directory / "node_modules").exists():
                    # Say so rather than running npm install: that can take minutes,
                    # and a run that appears to hang is worse than one that explains.
                    blocked = (f"node_modules is missing. Run `npm install` in "
                               f"{directory.name}/ first.")
                found.append(AppSpec(
                    kind="ui", name=directory.name or "ui", directory=directory,
                    # --host 127.0.0.1 explicitly: vite's default host is "localhost",
                    # which on macOS often resolves to ::1 first, so the server binds
                    # IPv6 while a 127.0.0.1 readiness probe waits out its full timeout
                    # on a server that started in 400ms.
                    command=["npm", "run", "dev", "--", "--port", str(port),
                             "--strictPort", "--host", "127.0.0.1"],
                    port=port, env={}, blocked=blocked))

    return found


def _wait_ready(port: int, timeout: int = READY_TIMEOUT_S,
                proc: subprocess.Popen | None = None) -> bool:
    """Wait for the app to answer. Any HTTP status counts — a 404 at `/` still means
    the server is up, and requiring 200 would hang on an API with no root route."""
    deadline = time.monotonic() + timeout
    # ONLY 127.0.0.1 — the address every app here is explicitly told to bind.
    #
    # Probing ::1 as well seemed harmless and was not: a DIFFERENT server on the other
    # stack satisfies the check. AURA's own dev server listens on [::1]:5174, so a
    # probe for the demo UI on port 5174 was answered by AURA, the app was marked
    # ready before it had bound, and the run then failed with CONNECTION_REFUSED on
    # the address it actually tested. A false ready is worse than a slow one.
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False          # it exited; no point waiting out the timeout
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(0.4)
    return False


class RunningApps:
    """Starts the detected applications and guarantees they are stopped.

    Teardown is in __exit__ because a leaked dev server holds its port and keeps a
    watcher running; the next run then fails in a way that points nowhere near here.
    """

    def __init__(self, specs: list[AppSpec], extra_env: dict[str, str] | None = None):
        self.specs = specs
        self.extra_env = extra_env or {}
        self.procs: dict[str, subprocess.Popen] = {}
        self.started: list[AppSpec] = []
        self.failures: list[tuple[AppSpec, str]] = []
        self.logs: dict[str, Path] = {}
        # Held for the child's lifetime. Letting the handle fall out of scope lets
        # CPython close it, and the child's output then lands on the terminal instead
        # of the log — which is how a dev server's banner ended up interleaved with
        # the run's own progress output.
        self._handles: list = []

    def __enter__(self) -> "RunningApps":
        for spec in self.specs:
            if spec.blocked:
                self.failures.append((spec, spec.blocked))
                continue
            try:
                self._start(spec)
            except Exception as exc:  # noqa: BLE001 — one app failing is data
                self.failures.append((spec, str(exc)))
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _start(self, spec: AppSpec) -> None:
        import tempfile

        env = {**os.environ, **self.extra_env, **spec.env}
        log_path = Path(tempfile.gettempdir()) / f"qatest-{spec.kind}-{spec.port}.log"
        handle = log_path.open("w")
        self._handles.append(handle)
        self.logs[spec.kind] = log_path

        proc = subprocess.Popen(
            spec.command, cwd=str(spec.directory), env=env,
            stdout=handle, stderr=subprocess.STDOUT,
            # Own process group, so stopping the dev server also stops the children
            # it spawns — npm leaves a node process behind otherwise.
            start_new_session=True)
        self.procs[spec.kind] = proc

        if not _wait_ready(spec.port, proc=proc):
            tail = ""
            with contextlib.suppress(OSError):
                tail = log_path.read_text("utf-8", "replace")[-400:]
            self._kill(proc)
            self.procs.pop(spec.kind, None)
            self.failures.append(
                (spec, f"did not answer on :{spec.port} within {READY_TIMEOUT_S}s. {tail}"))
            return

        self.started.append(spec)
        log.info("qatest: started %s app on %s", spec.kind, spec.url)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        import signal
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    def stop(self) -> None:
        for proc in self.procs.values():
            self._kill(proc)
        self.procs.clear()
        for handle in self._handles:
            with contextlib.suppress(Exception):
                handle.close()
        self._handles.clear()

    def url_for(self, kind: str) -> str:
        for spec in self.started:
            if spec.kind == kind:
                return spec.url
        return ""


def project_root(project_id: str) -> Path | None:
    """Where this project's code is checked out, or None."""
    return locate(project_id)[0]


def locate(project_id: str) -> tuple[Path | None, list[str]]:
    """(working copy, the places that were checked).

    The paths are returned so a failure can name them. "No working copy found" alone
    is unactionable — a project cloned in the deployed environment has an absolute
    /workspace path recorded that does not exist on a laptop, and the fix depends
    entirely on which path was missing.
    """
    # Reuse the advisor's resolver rather than re-deriving the path: it already
    # handles the AURA_WORKSPACE env var, the id sanitising, and the relative-path
    # trap where a subprocess's cwd changes what "./data/workspace" means.
    from src.services.advisor.tools import _clone_path

    checked: list[str] = []

    candidate = _clone_path(project_id)
    checked.append(str(candidate))
    if candidate.exists():
        return candidate, checked

    try:
        from src.database import dynamo_client as db
        rows = db.query_items("projects", "projectId", project_id, limit=1)
        recorded = rows[0].get("clonedPath") if rows else ""
        if recorded:
            path = Path(recorded)
            checked.append(f"{recorded} (recorded on the project)")
            if path.exists():
                return path, checked
    except Exception as exc:  # noqa: BLE001
        log.debug("qatest: project path lookup failed: %s", exc)

    return None, checked

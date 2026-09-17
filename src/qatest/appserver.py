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
import re
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
#: How many single wrapper directories `detect` will step through. One covers a folder
#: upload; two covers a folder uploaded from inside another. Past that, a tree that deep
#: with nothing runnable in it is not a layout worth guessing at.
_MAX_UNWRAP = 2

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
    #: A compose stack rather than a process. Started with `compose up -d` and torn
    #: down with `compose down -v`, because killing a process group leaves the
    #: containers running — and a leaked container holds the port for the next run.
    compose: bool = False
    #: Compose stacks are slow. Airflow initialises a database on first boot, which
    #: is minutes, and the default 60s would fail every one of them.
    ready_timeout: int = READY_TIMEOUT_S
    #: A path that answers 2xx once the stack is genuinely ready. A compose container
    #: binds its port long before the application inside it can serve.
    health_path: str = "/"

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


def _interpreter(directory: Path) -> str:
    """The project's own interpreter if it has one, else the one running AURA."""
    candidate = directory / ".venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else sys.executable


#: Compose filenames, in the order docker and podman look for them.
_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml",
                  "compose.yml", "compose.yaml")


def compose_command() -> list[str] | None:
    """`podman compose`, `docker compose`, or `docker-compose` — whichever is here.

    podman first: the emulators already run under it, so a machine set up for
    QualityMind has it, and preferring it avoids requiring Docker Desktop as well.
    """
    from src.qatest import toolpath

    for exe, sub in (("podman", "compose"), ("docker", "compose")):
        binary = toolpath.which(exe)
        if not binary:
            continue
        try:
            probe = subprocess.run([binary, sub, "version"], capture_output=True,
                                   text=True, timeout=20, env=toolpath.env())
        except Exception:                                     # noqa: BLE001
            # A dangling shim — which() succeeds, exec fails. Unguarded, this raised
            # straight out of detect_compose and killed application detection for the
            # whole repository, for a probe whose only job is to answer yes or no.
            continue
        if probe.returncode == 0:
            return [binary, sub]
    standalone = toolpath.which("docker-compose")
    return [standalone] if standalone else None


def detect_compose(root: Path) -> AppSpec | None:
    """A compose stack in this repository, if there is one.

    This is what makes a migrated project testable: the converter ships a
    `docker-compose.yml` beside the generated DAGs, and a run starts it rather than
    reporting that nothing here serves HTTP.
    """
    from src.migration import runtime as mig_runtime

    root = Path(root)
    for directory in [root] + [d for d in sorted(root.iterdir())
                               if d.is_dir() and d.name not in _SKIP] \
            if root.exists() else []:
        for name in _COMPOSE_FILES:
            compose_file = directory / name
            if not compose_file.is_file():
                continue

            body = compose_file.read_text(errors="replace")
            stack = _stack_for(body, mig_runtime)
            command = compose_command()
            blocked = "" if command else (
                "neither `podman compose` nor `docker compose` is available on this "
                "machine, so the stack cannot be started")
            return AppSpec(
                kind="api", name=directory.name or "stack", directory=directory,
                command=(command or ["docker", "compose"]) + ["up", "-d"],
                port=stack.port if stack else _first_published_port(body) or 8080,
                env={}, blocked=blocked, compose=True,
                ready_timeout=stack.start_timeout_s if stack else 180,
                health_path=stack.health_path if stack else "/")
    return None


def _stack_for(compose_body: str, mig_runtime):
    """Match a compose file to a known runtime, for its port and readiness path.

    Falls back to reading the published port out of the file, so a hand-written
    compose stack still works — it simply has no health path to check beyond `/`.
    """
    for target, stack in mig_runtime.RUNTIMES.items():
        if target in compose_body.lower():
            return stack
    return None


def _first_published_port(compose_body: str) -> int | None:
    match = re.search(r'^\s*-\s*"?(\d{2,5}):\d{2,5}"?\s*$', compose_body, re.M)
    return int(match.group(1)) if match else None


def detect(root: Path, _unwrapped: int = 0) -> list[AppSpec]:
    """Find the applications in a repository. Deepest-first is not needed: a repo
    holds at most one backend and one frontend in the shapes handled here.

    Searches the root and its immediate children. `_unwrapped` counts how many single
    wrapper directories have been stepped through — see the tail of this function.
    """
    found: list[AppSpec] = []
    root = Path(root)

    # A compose stack wins outright. When a project ships one it IS the application —
    # starting a loose uvicorn beside it would test a different thing from the one the
    # author packaged, on a port the stack may already hold.
    stack = detect_compose(root)
    if stack:
        return [stack]

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
                # A project venv if one exists, else sys.executable. The fallback is
                # the original behaviour and still right for a local run: the
                # interpreter running AURA already has fastapi and uvicorn.
                #
                # The venv matters on a self-hosted runner, where qatest/provision.py
                # creates one and pip-installs the project's requirements into it.
                # Without this the app would start on AURA's interpreter and fail at
                # import on any dependency AURA happens not to have.
                command=[_interpreter(directory), "-m", "uvicorn", target,
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
                    # Still not installed HERE: that can take minutes, and a run that
                    # appears to hang is worse than one that explains. A self-hosted
                    # runner installs it beforehand (qatest/provision.py), so reaching
                    # this message there means the install failed and said why.
                    blocked = (f"node_modules is missing. Run `npm install` in "
                               f"{directory.name}/ first, or start the run from a "
                               f"runner, which installs it.")
                found.append(AppSpec(
                    kind="ui", name=directory.name or "ui", directory=directory,
                    # --host 127.0.0.1 explicitly: vite's default host is "localhost",
                    # which on macOS often resolves to ::1 first, so the server binds
                    # IPv6 while a 127.0.0.1 readiness probe waits out its full timeout
                    # on a server that started in 400ms.
                    command=["npm", "run", "dev", "--", "--port", str(port),
                             "--strictPort", "--host", "127.0.0.1"],
                    port=port, env={}, blocked=blocked))

    # Uploading a FOLDER through the UI keeps the folder itself, so the app lands one
    # level below where a clone would have put it — `<workspace>/my-app/backend` rather
    # than `<workspace>/backend`. The loops above look at the root and its immediate
    # children only, so the whole project reads as healthy (the analyser walks the tree
    # recursively, so the graph, the cloud dependencies and the case list all come out
    # correct) right up until a run needs something to start, and then reports "no
    # runnable application found" about code that is plainly there.
    #
    # Step through that wrapper — but ONLY when it is the single directory present.
    # Searching two levels unconditionally would let `services/api` and `tools/api` both
    # match and make the choice of app arbitrary; with exactly one candidate there is
    # nothing to be ambiguous about. Bounded, because a chain of single directories
    # would otherwise recurse as deep as the tree goes.
    if not found and _unwrapped < _MAX_UNWRAP and root.exists():
        children = [d for d in sorted(root.iterdir())
                    if d.is_dir() and d.name not in _SKIP]
        if len(children) == 1:
            return detect(children[0], _unwrapped + 1)

    return found

def _wait_healthy(port: int, path: str = "/", timeout: int = 300) -> bool:
    """Wait for a 2xx on `path`.

    Stricter than `_wait_ready` on purpose. A compose container publishes its port as
    soon as it starts, so the socket answers minutes before the application does —
    Airflow returns 503 on /health for the whole of its first database migration. A
    socket check would call that ready and every case would then fail against a
    server that was still booting.
    """
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}{path}"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if 200 <= resp.status < 300:
                    return True
        except urllib.error.HTTPError as exc:
            if 200 <= exc.code < 300:
                return True
        except Exception:                                     # noqa: BLE001
            pass
        time.sleep(3)
    return False


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


def instrumented(spec: AppSpec, sidecar) -> AppSpec:
    """`spec` re-pointed through OpenTelemetry auto-instrumentation, where that is
    possible. Returns the spec UNCHANGED when it is not, so a caller can compare and
    report honestly rather than claiming tracing it did not install.

    Deliberately separate from `detect`, which is shared with QA runs: silently
    instrumenting the app under test would change what is being tested — extra ASGI
    middleware, extra latency, a different exception path — and a test run must
    exercise the application, not the application plus our tracing.

    Two cases it refuses, both permanently:

      COMPOSE. `spec.command` is `[compose…, "up", "-d"]`, so the process is compose,
      not the app. Instrumenting would mean editing the user's docker-compose.yml, and
      Aura must not rewrite a project's own deployment description. Since
      `detect_compose` wins outright, this is exactly the case most people mean by
      "run my project locally" — so the caller has to say so rather than showing an
      empty traces panel.

      NODE. `npm run dev` is a bundler; the model calls in a frontend happen in the
      browser, not in that process. Making it real would need `npm install` into the
      project, rewriting package-lock.json — a source mutation that would then fight
      `npm ci` on the next `provision.install`.
    """
    from dataclasses import replace

    if spec.compose or not spec.command:
        return spec
    binary = str(spec.command[0])
    if "python" not in binary.lower():
        return spec

    # The MODULE, not the `opentelemetry-instrument` console script: a script installed
    # by `pip --target` carries an absolute shebang pointing at whichever interpreter
    # did the install, which is not the interpreter this app runs on.
    command = [binary, "-m", "opentelemetry.instrumentation.auto_instrumentation",
               *spec.command]
    env = {**spec.env, "PYTHONPATH": _prepend_path(str(sidecar), spec.env.get("PYTHONPATH", ""))}
    return replace(spec, command=command, env=env)


def _prepend_path(head: str, tail: str) -> str:
    return f"{head}{os.pathsep}{tail}" if tail else head


class RunningApps:
    """Starts the detected applications and guarantees they are stopped.

    Teardown is in __exit__ because a leaked dev server holds its port and keeps a
    watcher running; the next run then fails in a way that points nowhere near here.
    """

    def __init__(self, specs: list[AppSpec], extra_env: dict[str, str] | None = None,
                 *, destroy_volumes: bool = True, compose_project: str = ""):
        self.specs = specs
        self.extra_env = extra_env or {}
        # `down -v` deletes NAMED VOLUMES — the stack's database. Correct for a test
        # run, whose stack is disposable and whose next run must start from nothing.
        # Catastrophic for "run my project locally", where the volume is the developer's
        # own data. The default stays True so every existing caller behaves exactly as
        # before, and the long-lived session opts out.
        self.destroy_volumes = destroy_volumes
        # Explicit `-p`, so teardown provably targets the stack WE started. Without it
        # compose derives a project name from the directory, which is the same name a
        # stack the developer started by hand would have — and `down` would take theirs.
        self.compose_project = compose_project
        self.procs: dict[str, subprocess.Popen] = {}
        self._composed: list[AppSpec] = []
        self.started: list[AppSpec] = []
        self.failures: list[tuple[AppSpec, str]] = []
        self.logs: dict[str, Path] = {}
        # Held for the child's lifetime. Letting the handle fall out of scope lets
        # CPython close it, and the child's output then lands on the terminal instead
        # of the log — which is how a dev server's banner ended up interleaved with
        # the run's own progress output.
        self._handles: list = []

    def start_all(self) -> "RunningApps":
        """Start every spec. Split out of `__enter__` so a caller that owns the
        lifetime itself — a long-lived dev session, which cannot sit inside a `with`
        spanning many poll iterations — reuses this code rather than copying it.
        The guaranteed-teardown contract still holds for every `with` user below."""
        for spec in self.specs:
            if spec.blocked:
                self.failures.append((spec, spec.blocked))
                continue
            try:
                self._start(spec)
            except Exception as exc:  # noqa: BLE001 — one app failing is data
                self.failures.append((spec, str(exc)))
        return self

    def __enter__(self) -> "RunningApps":
        return self.start_all()

    def __exit__(self, *_exc) -> None:
        self.stop()

    def _start(self, spec: AppSpec) -> None:
        if spec.compose:
            self._start_compose(spec)
            return
        import tempfile

        from src.qatest import toolpath

        # toolpath first, so `npm run dev` finds node wherever it was installed.
        env = {**toolpath.env(), **self.extra_env, **spec.env}
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

    def _start_compose(self, spec: AppSpec) -> None:
        """`compose up -d`, then wait for the application inside to answer.

        Detached on purpose: compose forks the containers and returns, so there is no
        process to hold. Teardown is `compose down -v` — killing a process group would
        leave them running, and a leaked container holds the port for the next run.
        """
        from src.qatest import toolpath

        command = self._compose_command(spec)
        result = subprocess.run(command, cwd=str(spec.directory),
                                capture_output=True, text=True, timeout=600,
                                env=toolpath.env())
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-400:]
            self.failures.append((spec, f"`{' '.join(command)}` failed: {tail}"))
            return

        self._composed.append(spec)
        # The port binds long before the application inside is ready, so probe the
        # health path rather than the socket. Airflow answers 503 on /health while it
        # is still migrating its database.
        if not _wait_healthy(spec.port, spec.health_path, spec.ready_timeout):
            self.failures.append(
                (spec, f"the stack started but did not become healthy on "
                       f":{spec.port}{spec.health_path} within {spec.ready_timeout}s. "
                       f"`{' '.join(spec.command[:2])} logs` on the runner will say why."))
            return

        self.started.append(spec)
        log.info("qatest: compose stack ready on %s", spec.url)

    def _compose_command(self, spec: AppSpec) -> list[str]:
        """`spec.command` with an explicit project name spliced in, when we have one.

        The name goes immediately after the compose binary and before `up`, which is
        where every compose implementation expects a global flag.
        """
        if not self.compose_project:
            return list(spec.command)
        head = spec.command[:-2]                  # the binary, without "up -d"
        return head + ["-p", self.compose_project] + spec.command[-2:]

    def _stop_compose(self) -> None:
        for spec in self._composed:
            from src.qatest import toolpath

            head = spec.command[:-2]
            if self.compose_project:
                head = head + ["-p", self.compose_project]
            down = head + ["down"] + (["-v"] if self.destroy_volumes else [])
            with contextlib.suppress(Exception):
                subprocess.run(down, cwd=str(spec.directory),
                               capture_output=True, timeout=180,
                               env=toolpath.env())
            log.info("qatest: compose stack stopped (%s, volumes %s)", spec.name,
                     "removed" if self.destroy_volumes else "kept")
        self._composed.clear()

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
        # Containers first: they hold the published ports, and the next run needs them.
        self._stop_compose()
        for proc in self.procs.values():
            self._kill(proc)
        self.procs.clear()
        for handle in self._handles:
            with contextlib.suppress(Exception):
                handle.close()
        self._handles.clear()

    def stop_one(self, kind: str) -> bool:
        """Stop just one of the started apps. Returns whether anything was stopped.

        Compose is all-or-nothing here: a stack is one spec, so stopping "the api"
        of a compose project would mean reaching inside it, which this module has no
        business doing.
        """
        proc = self.procs.pop(kind, None)
        if proc is None:
            return False
        self._kill(proc)
        self.started = [spec for spec in self.started if spec.kind != kind]
        return True

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
    import re

    checked: list[str] = []

    def consider(path: Path | None, label: str) -> Path | None:
        if not path:
            return None
        text = f"{path}{label}"
        if text not in checked:
            checked.append(text)
        return path if path.exists() else None

    # 1. The configured workspace. Read from Settings, not os.environ, so the value
    #    in src/.env takes effect — advisor/tools.py reads the raw env var, which
    #    pydantic-settings never populates, and therefore always resolved /workspace.
    try:
        from src.config_settings import get_settings
        root = Path(get_settings().aura_workspace or "/workspace").resolve()
    except Exception:  # noqa: BLE001 — a probe must work without app settings
        root = Path(os.environ.get("AURA_WORKSPACE", "/workspace")).resolve()
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", project_id)
    found = consider(root / safe, "")
    if found:
        return found, checked

    # 2. The path recorded when the project was cloned or uploaded.
    recorded = ""
    try:
        from src.database import dynamo_client as db
        rows = db.query_items("projects", "projectId", project_id, limit=1)
        recorded = str(rows[0].get("clonedPath") or "") if rows else ""
    except Exception as exc:  # noqa: BLE001
        log.debug("qatest: project path lookup failed: %s", exc)
    if recorded:
        found = consider(Path(recorded), " (recorded on the project)")
        if found:
            return found, checked

    # 3. A local connector's own path, and its PARENT — two connectors for one repo
    #    typically name backend/ and frontend/ inside a single checkout, which is the
    #    directory to start from.
    for path in _connector_paths(project_id):
        found = consider(path.parent, " (parent of a connector path)")
        if found:
            return found, checked
        found = consider(path, " (a connector path)")
        if found:
            return found, checked

    return None, checked


def _connector_paths(project_id: str) -> list[Path]:
    """Local paths this project's connectors name.

    Worth checking, and worth REPORTING when missing: a connector pointing at
    /workspace/<id>/backend with nothing there means the code was uploaded into a
    different environment, which is otherwise invisible from a failed run.
    """
    try:
        from src.database import dynamo_client as db
        out = []
        for c in db.scan_items("connectors", limit=500):
            if c.get("projectId") != project_id:
                continue
            raw = c.get("localPath") or c.get("local_path") or ""
            if raw:
                out.append(Path(str(raw)))
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("qatest: connector lookup failed: %s", exc)
        return []

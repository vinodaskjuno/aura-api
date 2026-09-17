"""A developer's project, running locally and staying up until they stop it.

This is the difference between `app-populate` — which boots the app, lets its startup
create cloud resources, and stops it again inside one `with` block — and "run my
project locally", where the app has to outlive the poll iteration that started it.

RUNS ONLY ON THE RUNNER. Deliberately its own module rather than more state on
`appserver`, which the Fargate API also imports: a module global holding live `Popen`
handles has no meaning in a web worker, and putting one there invites a future caller
to reach for it from the server side, where it would silently always be empty.

OWNERSHIP IS THE HARD PART, AND IT IS NOT LIKE CONTAINERS. `emulators` can ask podman
"what is on port 4566" and compare the answer against `MANAGED_PREFIXES`, because a
container name is a durable, externally readable ownership token. A pid is not: pids
are reused, and "port 8000 is held by pid 4711" says nothing about who started it. So
ownership is proved twice before anything is ever killed:

  1. `AURA_APP_SESSION=<sessionId>` is exported into the child, and read back out of
     `/proc` (Linux) or `ps eww` (macOS). A process carrying our session id is ours.
  2. A session file records the pid, the port and the id we expect to find.

If neither proves it, the session is REPORTED AND ABANDONED, never killed. Killing a
process we cannot identify is how a feature like this destroys someone's unrelated
work, and the cost of the alternative is a stale row on a panel.

THE SESSION FILE IS NAMED `.aura-app-session.json` AND THE PREFIX IS LOAD-BEARING.
`provision.fetch` deletes every child of the working copy except `node_modules`,
`.venv` and anything starting with `.aura-`. Named anything else, the file would be
deleted by the next refresh while the process it describes was still running.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Exported into the child and matched on the way back. See rule 1 above.
MARKER_ENV = "AURA_APP_SESSION"

SESSION_FILE = ".aura-app-session.json"

#: Explicit compose project name, so `down` provably targets our stack and never one
#: the developer started by hand in the same directory.
COMPOSE_PREFIX = "aura-dev-app"

#: Live sessions on this machine, keyed by projectId. One per project: two copies of
#: the same app would fight over the same port and the same working copy.
_SESSIONS: dict[str, "AppSession"] = {}
_LOCK = threading.Lock()


def compose_project_for(project_id: str) -> str:
    # Compose project names are restricted to lowercase alphanumerics, dashes and
    # underscores; a projectId is user-supplied, so it is squeezed into that shape
    # rather than trusted.
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(project_id).lower())
    return f"{COMPOSE_PREFIX}-{safe}"[:60]


def _session_file(project_id: str) -> Path:
    from src.qatest import provision
    return provision.workspace_root(project_id) / SESSION_FILE


def _port_answers(port: int, timeout: float = 0.3) -> bool:
    """Is anything listening? Copies `agent._floci_ui`'s bounded probe deliberately —
    `appserver._wait_ready` loops for up to 60s, which would block the poll loop."""
    if not port:
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _marker_of(pid: int) -> str:
    """The AURA_APP_SESSION of a running process, or "" if it cannot be read.

    "" means UNPROVEN, not "not ours" — a hardened kernel, a different user or a
    platform without either mechanism all land here, and the caller must treat it as
    a refusal to act rather than permission to kill.
    """
    if not _pid_alive(pid):
        return ""
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
        except OSError:
            return ""
        for entry in raw.split(b"\0"):
            if entry.startswith(MARKER_ENV.encode() + b"="):
                return entry.split(b"=", 1)[1].decode("utf-8", "replace")
        return ""
    # macOS and the BSDs: `ps eww` prints the environment after the command.
    try:
        import subprocess
        out = subprocess.run(["ps", "eww", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5)
        for token in (out.stdout or "").split():
            if token.startswith(MARKER_ENV + "="):
                return token.split("=", 1)[1]
    except Exception:                                         # noqa: BLE001
        return ""
    return ""


class AppSession:
    """One project's app, running. Holds the `RunningApps` it started."""

    def __init__(self, project_id: str, session_id: str, apps: Any,
                 *, instrumented: bool = False, env_fingerprint: str = "",
                 instrumentation_error: str = "") -> None:
        self.project_id = project_id
        self.session_id = session_id
        self.apps = apps
        self.started_at = time.time()
        self.instrumented = instrumented
        self.instrumentation_error = instrumentation_error
        self.env_fingerprint = env_fingerprint
        self.adopted = False

    # ── wire form ───────────────────────────────────────────────────────────

    #: Console lines carried on each state report. Small on purpose: this rides the
    #: regular 15s POST instead of the runner's single command slot, which
    #: `useContainerLogs` already contends for and which two followers would starve.
    LOG_TAIL_LINES = 40
    LOG_TAIL_CHARS = 200

    def _log_tail(self, spec) -> list[str]:
        path = getattr(self.apps, "logs", {}).get(spec.kind)
        if not path:
            return []
        try:
            lines = Path(path).read_text("utf-8", "replace").splitlines()
        except OSError:
            return []
        return [line[:self.LOG_TAIL_CHARS] for line in lines[-self.LOG_TAIL_LINES:]]

    def describe(self, *, probe: bool = True, logs: bool = True) -> list[dict]:
        """One row per started app, for the runner's state report."""
        rows: list[dict] = []
        for spec in getattr(self.apps, "started", []) or []:
            proc = getattr(self.apps, "procs", {}).get(spec.kind)
            rows.append({
                "logTail": self._log_tail(spec) if logs else [],
                "projectId": self.project_id,
                "sessionId": self.session_id,
                "kind": spec.kind,
                "name": spec.name,
                "url": spec.url,
                "port": int(spec.port or 0),
                "pid": int(getattr(proc, "pid", 0) or 0),
                "compose": bool(spec.compose),
                "startedAt": self.started_at,
                "instrumented": self.instrumented,
                "instrumentationError": self.instrumentation_error,
                "healthy": _port_answers(spec.port) if probe else True,
            })
        return rows

    def record(self) -> None:
        """Write the session file. Best-effort: a missing file costs us adoption
        after a restart, and that is strictly better than failing the start."""
        rows = self.describe(probe=False, logs=False)
        payload = {
            "sessionId": self.session_id,
            "projectId": self.project_id,
            "startedAt": self.started_at,
            "composeProject": compose_project_for(self.project_id),
            "envFingerprint": self.env_fingerprint,
            "instrumented": self.instrumented,
            "apps": rows,
        }
        try:
            path = _session_file(self.project_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename, so a reader never sees half a file.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(path)
        except Exception as exc:                              # noqa: BLE001
            log.warning("could not record app session for %s: %s", self.project_id, exc)

    def forget(self) -> None:
        with contextlib.suppress(Exception):
            _session_file(self.project_id).unlink()

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.apps.stop()
        self.forget()


def start(project_id: str, specs: list, extra_env: dict[str, str], *,
          instrumented: bool = False, instrumentation_error: str = "",
          env_fingerprint: str = "") -> AppSession:
    """Start the app and keep it. The caller has already detected and instrumented."""
    from src.qatest import appserver

    session_id = secrets.token_hex(8)
    env = {**(extra_env or {}), MARKER_ENV: session_id}
    apps = appserver.RunningApps(
        specs, extra_env=env,
        # The developer's own volumes. `down -v` is right for a disposable test
        # stack and wrong for the database they have been seeding all morning.
        destroy_volumes=False,
        compose_project=compose_project_for(project_id),
    )
    apps.start_all()
    session = AppSession(project_id, session_id, apps,
                         instrumented=instrumented,
                         instrumentation_error=instrumentation_error,
                         env_fingerprint=env_fingerprint)
    with _LOCK:
        _SESSIONS[project_id] = session
    session.record()
    return session


def get(project_id: str) -> AppSession | None:
    with _LOCK:
        return _SESSIONS.get(project_id)


def active_project_ids() -> list[str]:
    with _LOCK:
        return list(_SESSIONS.keys())


def stop(project_id: str) -> bool:
    with _LOCK:
        session = _SESSIONS.pop(project_id, None)
    if session is None:
        # Nothing live in this process. A file may still describe one from a previous
        # life — `sweep` is what deals with that, and it refuses to kill what it
        # cannot prove is ours.
        return False
    session.stop()
    return True


def stop_all() -> None:
    """Every session on this machine. Wired to atexit and the signal handlers.

    Without this, `start_new_session=True` — which `appserver` sets so stopping a dev
    server also stops the children npm spawns — means Ctrl-C on the runner leaves
    every app running. That was invisible while every session lived inside a `with`.
    """
    with _LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        with contextlib.suppress(Exception):
            session.stop()


def describe_all() -> list[dict]:
    with _LOCK:
        sessions = list(_SESSIONS.values())
    rows: list[dict] = []
    for session in sessions:
        with contextlib.suppress(Exception):
            rows.extend(session.describe())
    return rows


def sweep(project_ids: list[str] | None = None) -> list[str]:
    """Reconcile session files left by a previous life of this process.

    Returns human-readable notes about anything that could not be reclaimed, so the
    runner can log them rather than silently leaving ports held.

    Three outcomes per file, and only the first two touch anything:
      - the process is gone, or the port is dead  -> delete the file
      - the process is alive and proves it is ours -> report it as an orphan we can
        stop on request (we cannot re-adopt the Popen handle, but we can identify it)
      - the process is alive and does NOT prove it -> leave everything alone and say so
    """
    notes: list[str] = []
    from src.qatest import provision

    candidates = project_ids if project_ids is not None else _known_project_dirs()
    for project_id in candidates:
        path = provision.workspace_root(project_id) / SESSION_FILE
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:                                     # noqa: BLE001
            with contextlib.suppress(Exception):
                path.unlink()
            continue

        expected = str(data.get("sessionId") or "")
        live, unproven = [], []
        for row in data.get("apps") or []:
            pid, port = int(row.get("pid") or 0), int(row.get("port") or 0)
            if not _pid_alive(pid) or not _port_answers(port):
                continue
            if _marker_of(pid) == expected and expected:
                live.append(row)
            else:
                unproven.append(row)

        if not live and not unproven:
            with contextlib.suppress(Exception):
                path.unlink()
            continue
        for row in live:
            notes.append(
                f"{project_id}: an app from a previous session is still running on "
                f"port {row.get('port')} (pid {row.get('pid')}); stop it from DevMate")
        for row in unproven:
            notes.append(
                f"{project_id}: port {row.get('port')} is held by pid {row.get('pid')}, "
                f"which Aura cannot prove it started — left alone")
    return notes


def _known_project_dirs() -> list[str]:
    """Project ids with a working copy on this machine."""
    from src.qatest import provision
    try:
        base = provision.workspace_root("_").parent
        return [d.name for d in base.iterdir() if d.is_dir()]
    except Exception:                                         # noqa: BLE001
        return []


def env_fingerprint(env: dict[str, str]) -> str:
    """A stable digest of the env an app was started with.

    A QA run that adopts a live session is testing an app pointed at whatever
    emulators the session was given. If those differ from what the run wants, the
    result is a green run against the wrong backend — worse than a refusal.
    """
    import hashlib
    items = sorted((k, v) for k, v in (env or {}).items() if k != MARKER_ENV)
    digest = hashlib.sha256(repr(items).encode()).hexdigest()
    return digest[:16]

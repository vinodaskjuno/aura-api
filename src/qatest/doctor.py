"""Can this machine run a QualityMind test, and if not, exactly what do I type?

Three callers need the same answer and used to compute their own, badly:

  * the agent's startup preflight, which checked two things
  * `/capabilities`, which reported `browser: true` from an import check and so could
    claim Chromium on a machine where Chromium would not launch
  * the Runner panel, which had no way to say anything beyond a tick or a cross

So the checks live here, each producing a `Finding` that carries a severity, a plain
explanation, and the exact command for THIS platform. The UI renders the command; it
never runs it. `--setup` runs it only after a human has read it and agreed.

Deliberately importable from the Fargate router: nothing here touches httpx, boto3 or
Settings, and with no tools installed it makes zero subprocess calls.

**Honesty about platforms.** The macOS commands are verified on a real machine. The
Linux ones are conventional but untested here. The Windows ones are provisional and
say so in the output — a table lookup proving it returns a string is not evidence that
the string works.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

from src.qatest import toolpath

#: Severity. `blocks` means a run cannot execute at all; `degrades` means some cases
#: will report `unemulated` or `blocked`; `info` is context that makes a later error
#: legible. Only `blocks` stops the agent starting.
BLOCKS, DEGRADES, INFO = "blocks", "degrades", "info"

#: Verbatim from aura-infra/DEPLOYMENT.md. A drift test asserts they stay identical —
#: two different recommended machine sizes is worse than either one.
MACHINE_INIT = "podman machine init --cpus 4 -m 8192 --now"

_WINDOWS_CAVEAT = ("windows support is provisional — these commands have not been "
                   "verified on this platform; please report what actually happened")


@dataclass(frozen=True)
class Finding:
    check: str                       # stable id, e.g. "podman.machine"
    ok: bool
    severity: str = BLOCKS
    title: str = ""
    detail: str = ""
    remedy: tuple[str, ...] = ()     # exact commands, in order
    fixable: bool = False            # --setup has a recipe here (False => print only)
    version: str = ""
    doc: str = ""

    def as_dict(self) -> dict:
        return {"check": self.check, "ok": self.ok, "severity": self.severity,
                "title": self.title, "detail": self.detail,
                "remedy": list(self.remedy), "fixable": self.fixable}


@dataclass
class Diagnosis:
    platform: str
    arch: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok and f.severity == BLOCKS]

    @property
    def degraded(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok and f.severity == DEGRADES]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def find(self, check: str) -> Finding | None:
        return next((f for f in self.findings if f.check == check), None)

    def version_of(self, check: str) -> str:
        found = self.find(check)
        return found.version if found else ""

    def as_dict(self, failures_only: bool = True, limit: int = 6) -> dict:
        """The shape reported to the server.

        Failures only and capped, because every runner's findings live in ONE DynamoDB
        item under a 400 KB limit. The passing checks are already implied by the chips.
        """
        chosen = [f for f in self.findings if not f.ok] if failures_only else self.findings
        chosen = [f for f in chosen if f.severity in (BLOCKS, DEGRADES)][:limit]
        return {"ok": self.ok,
                "platform": f"{self.platform}/{self.arch}",
                "checkedAt": _now(),
                "findings": [f.as_dict() for f in chosen]}

    def lines(self) -> list[str]:
        """One ASCII line per check. Never ✓ — cp1252 stdout on Windows raises
        UnicodeEncodeError, which turns a diagnostic into a crash."""
        out = []
        for f in self.findings:
            mark = "[ok]" if f.ok else ("[!!]" if f.severity == BLOCKS else "[--]")
            suffix = f" ({f.version})" if f.version else ""
            out.append(f"  {mark} {f.check:<22} {f.title}{suffix}")
        return out


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Cache ─────────────────────────────────────────────────────────────────────
#
# Load-bearing, not an optimisation. `report_state` builds the machine state twice per
# cycle when a command is pending, and `/capabilities` is hit on every QA page load —
# without this each render shells out to `podman info`.

_cache: dict[bool, tuple[float, Diagnosis]] = {}
_launch: tuple[bool, str] | None = None


def invalidate() -> None:
    _cache.clear()


def record_launch(ok: bool, why: str = "") -> None:
    """Feed a real run's browser launch back in.

    A run launches Chromium anyway, so its outcome is free information — which is what
    lets the shallow path stay cheap without going stale about the one check that
    costs seconds.
    """
    global _launch
    _launch = (ok, why)
    invalidate()


def diagnose(deep: bool = False, ttl_s: float = 60.0) -> Diagnosis:
    """Everything worth knowing about this machine.

    `deep=True` launches Chromium — 1-3s and a real process — and belongs only in
    preflight and `--doctor`, never on the state-report path.
    """
    hit = _cache.get(deep)
    if hit and (time.monotonic() - hit[0]) < ttl_s:
        return hit[1]

    import platform as _platform

    diag = Diagnosis(platform=toolpath.platform_name(), arch=_platform.machine())
    for check in (_check_python, _check_deps, _check_podman, _check_browser,
                  _check_compose, _check_disk, _check_workspace):
        try:
            diag.findings.extend(check(deep))
        except Exception as exc:                              # noqa: BLE001
            # The doctor must never be the thing that crashes — it is what people run
            # when something is already wrong.
            diag.findings.append(Finding(
                check=getattr(check, "__name__", "check").lstrip("_"), ok=False,
                severity=INFO, title="this check could not run",
                detail=f"{type(exc).__name__}: {exc}"))

    _cache[deep] = (time.monotonic(), diag)
    return diag


# ── Checks ────────────────────────────────────────────────────────────────────

def _check_python(_deep: bool) -> list[Finding]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    ok = sys.version_info >= (3, 10)
    return [Finding("python.version", ok, BLOCKS,
                    "Python 3.10 or newer" if ok else f"Python {version} is too old",
                    "" if ok else "The runner uses `X | None` annotations at runtime.",
                    remedy=() if ok else ("install Python 3.12 and recreate the venv",),
                    version=version)]


def _check_deps(_deep: bool) -> list[Finding]:
    try:
        import httpx                                          # noqa: F401
        return [Finding("deps.httpx", True, BLOCKS, "runner dependencies present")]
    except ImportError as exc:
        return [Finding("deps.httpx", False, BLOCKS,
                        "the runner's dependencies are missing",
                        f"{exc}. This usually means the wrong virtualenv — without it "
                        f"the agent fails with a bare traceback on its first request.",
                        remedy=(f"{_py()} -m pip install -r src/requirements.txt",),
                        fixable=True)]


def _check_podman(_deep: bool) -> list[Finding]:
    from src.qatest import emulators

    binary = emulators.podman_path()
    if not binary:
        return [Finding("podman.present", False, BLOCKS,
                        "podman is not installed",
                        "Floci cloud emulators run as podman containers. Without it a "
                        "project that uses AWS, Azure, GCP or OCI reports its cases as "
                        "`not emulated` rather than testing them.",
                        remedy=_podman_install_remedy(),
                        fixable=toolpath.platform_name() != "windows",
                        doc="QA_RUNNER.md#0-one-time-setup")]

    found = [Finding("podman.present", True, BLOCKS, "podman found", binary)]

    ready, why = emulators.podman_ready()
    version = _podman_version()
    if ready:
        found.append(Finding("podman.working", True, BLOCKS, "podman is usable",
                             version=version))
        found.extend(_check_machine_size())
        return found

    machine = _machine_finding(why)
    found.append(Finding("podman.working", False, BLOCKS, machine.title, machine.detail,
                         remedy=machine.remedy, fixable=machine.fixable,
                         version=version))
    return found


def _machine_finding(why: str) -> Finding:
    """Tell "no machine" apart from "machine stopped" — different commands."""
    if toolpath.platform_name() == "linux":
        return Finding("podman.working", False, BLOCKS,
                       "podman is installed but not usable", why,
                       remedy=("podman info",
                               "sudo usermod --add-subuids 100000-165535 "
                               "--add-subgids 100000-165535 $USER && podman system migrate"),
                       fixable=False)

    machines = _machines()
    if machines is None:
        return Finding("podman.working", False, BLOCKS,
                       "podman is installed but not usable", why,
                       remedy=("podman machine start",), fixable=True)
    if not machines:
        return Finding("podman.working", False, BLOCKS,
                       "podman has no virtual machine yet",
                       "On macOS and Windows podman runs containers inside a VM, which "
                       "has to be created once. This downloads several GB and takes a "
                       "few minutes.",
                       remedy=(MACHINE_INIT,), fixable=True,
                       doc="DEPLOYMENT.md")
    return Finding("podman.working", False, BLOCKS,
                   "the podman machine is not running",
                   "podman is installed, but its virtual machine is stopped — so every "
                   "podman command fails. This is the most common cause of a runner "
                   "that looks healthy and then fails mid-run.",
                   remedy=("podman machine start",), fixable=True)


def _check_machine_size() -> list[Finding]:
    if toolpath.platform_name() == "linux":
        return []
    machines = _machines() or []
    running = next((m for m in machines if m.get("Running")), None)
    if not running:
        return []
    cpus = int(running.get("CPUs") or 0)
    memory_gb = int(running.get("Memory") or 0) / (1024 ** 3)
    if cpus >= 4 and memory_gb >= 7.5:
        return [Finding("podman.machine.size", True, DEGRADES,
                        f"machine has {cpus} CPUs, {memory_gb:.0f} GiB")]
    return [Finding("podman.machine.size", False, DEGRADES,
                    f"the podman machine is small ({cpus} CPUs, {memory_gb:.1f} GiB)",
                    "Emulators and the application under test share it. Below 4 CPUs "
                    "and 8 GiB, runs get slow and time out.",
                    # Print only: resizing stops the machine, and recreating one
                    # destroys the images the user already pulled.
                    remedy=("podman machine stop",
                            "podman machine set --cpus 4 --memory 8192",
                            "podman machine start"),
                    fixable=False)]


def _machines() -> list[dict] | None:
    """`podman machine list` as data. None when it cannot be read at all."""
    from src.qatest import emulators

    code, out = emulators._run(["machine", "list", "--format", "json"], timeout=20)
    if code != 0:
        return None
    try:
        parsed = json.loads(out)
    except ValueError:
        return None
    return parsed if isinstance(parsed, list) else None


def _podman_version() -> str:
    from src.qatest import emulators

    code, out = emulators._run(["version", "--format", "{{.Client.Version}}"], timeout=15)
    return out.strip() if code == 0 else ""


def _check_browser(deep: bool) -> list[Finding]:
    from src.qatest.runner import _playwright_available

    ok, why = _playwright_available()
    if not ok:
        return [Finding("browser.package", False, BLOCKS,
                        "the playwright package is not installed", why,
                        remedy=(f"{_py()} -m pip install playwright",
                                f"{_py()} -m playwright install chromium"),
                        fixable=True)]

    found = [Finding("browser.package", True, BLOCKS, "playwright installed")]

    # The binary check, not the launch: ~200ms, no process spawned. This is what the
    # `browser` flag reports, replacing an import check that could claim Chromium on a
    # machine where it would not start.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        present = bool(path) and os.path.exists(path)
        version = _browser_build(path or "")
    except Exception as exc:                                  # noqa: BLE001
        present, version, path = False, "", str(exc)

    if not present:
        return found + [Finding("browser.binary", False, BLOCKS,
                                "the Chromium browser itself is not installed",
                                "The playwright package is present but its browser is "
                                "not — a run would claim a test and then fail to start.",
                                remedy=(f"{_py()} -m playwright install chromium",),
                                fixable=True)]
    found.append(Finding("browser.binary", True, BLOCKS, "Chromium installed",
                         version=version))

    if not deep:
        if _launch and not _launch[0]:
            found.append(Finding("browser.launch", False, BLOCKS,
                                 "Chromium would not launch on the last run", _launch[1],
                                 remedy=_browser_launch_remedy(), fixable=False))
        return found

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(args=["--no-sandbox"]).close()
        found.append(Finding("browser.launch", True, BLOCKS, "Chromium launches"))
    except Exception as exc:                                  # noqa: BLE001
        found.append(Finding("browser.launch", False, BLOCKS,
                             "Chromium will not launch",
                             f"{type(exc).__name__}: {str(exc)[:300]}",
                             remedy=_browser_launch_remedy(), fixable=False))
    return found


def _browser_build(path: str) -> str:
    """The playwright build id out of the executable path.

    Taking a fixed number of parent directories does not work: on macOS the binary is
    buried inside an .app bundle (`…/chromium-1234/chrome-mac-arm64/Google Chrome for
    Testing.app/Contents/MacOS/…`), so counting upwards returns "Contents". The
    `chromium-NNNN` segment is the stable part on every platform.
    """
    for part in path.replace("\\", "/").split("/"):
        if part.startswith("chromium-"):
            return part.split("-", 1)[1]
    return ""


def _check_compose(_deep: bool) -> list[Finding]:
    from src.qatest import appserver

    command = appserver.compose_command()
    if command:
        return [Finding("compose.provider", True, DEGRADES,
                        f"compose available ({' '.join(command[-2:])})")]
    return [Finding("compose.provider", False, DEGRADES,
                    "no compose provider is installed",
                    "Only needed to start a migrated project's own stack (a converted "
                    "Airflow deployment, for instance). Everything else runs without it.",
                    remedy=_compose_remedy(), fixable=toolpath.platform_name() != "windows")]


def _check_disk(_deep: bool) -> list[Finding]:
    try:
        free_gb = shutil.disk_usage(os.path.expanduser("~")).free / (1024 ** 3)
    except OSError:
        return []
    if free_gb >= 10:
        return [Finding("disk.host", True, DEGRADES, f"{free_gb:.0f} GB free")]
    return [Finding("disk.host", False, BLOCKS if free_gb < 2 else DEGRADES,
                    f"only {free_gb:.1f} GB free on this machine",
                    "A run downloads the project's working copy, its dependencies and "
                    "a ~150 MB browser cache, and podman images are larger again.",
                    remedy=("free up disk space",), fixable=False)]


def _check_workspace(_deep: bool) -> list[Finding]:
    try:
        from src.config_settings import get_settings
        base = get_settings().aura_workspace or "./data/workspace"
    except Exception:                                         # noqa: BLE001
        base = "./data/workspace"
    path = os.path.abspath(os.path.expanduser(base))
    parent = path if os.path.isdir(path) else os.path.dirname(path) or "."
    writable = os.access(parent, os.W_OK)
    return [Finding("workspace.writable", writable, DEGRADES,
                    "workspace writable" if writable else "the workspace is not writable",
                    path if writable else
                    f"{path} cannot be written, so a project's code cannot be fetched.",
                    remedy=() if writable else (f"mkdir -p {path}",), fixable=False)]


# ── Per-platform remedies ─────────────────────────────────────────────────────

def _py() -> str:
    """The interpreter that will actually run the agent — never a bare `python`."""
    return sys.executable or "python"


def _podman_install_remedy() -> tuple[str, ...]:
    system = toolpath.platform_name()
    if system == "darwin":
        if toolpath.which("brew"):
            return ("brew install podman", MACHINE_INIT)
        return ("install Homebrew from https://brew.sh, then:",
                "brew install podman", MACHINE_INIT)
    if system == "windows":
        return ("winget install -e --id RedHat.Podman", MACHINE_INIT)
    manager, install = _linux_manager()
    return (f"{install} podman",) if manager else (
        "install podman with your distribution's package manager",)


def _compose_remedy() -> tuple[str, ...]:
    system = toolpath.platform_name()
    if system == "darwin":
        # Verified: podman 5.x looks for a `docker-compose` BINARY. The python
        # `podman-compose` package is not what it probes for, which is why the old
        # advice in QA_MIND_TESTING.md did not work.
        return ("brew install docker-compose",)
    if system == "windows":
        return ("winget install -e --id Docker.DockerDesktop",)
    manager, install = _linux_manager()
    return (f"{install} podman-compose",) if manager else (
        "install podman-compose with your distribution's package manager",)


def _browser_launch_remedy() -> tuple[str, ...]:
    if toolpath.platform_name() == "linux":
        manager, _ = _linux_manager()
        if manager in ("apt-get", "dnf", "yum"):
            return (f"{_py()} -m playwright install-deps chromium",)
        # No recipe for pacman/alpine on purpose: Chromium's own error text names the
        # missing libraries more accurately than a table we would have to maintain.
        return ("install the system libraries named in the error above",)
    return (f"{_py()} -m playwright install chromium",)


def _linux_manager() -> tuple[str, str]:
    """(manager, install prefix) for this distribution."""
    for manager, install in (
        ("apt-get", "sudo apt-get update && sudo apt-get install -y"),
        ("dnf", "sudo dnf install -y"),
        ("yum", "sudo yum install -y"),
        ("pacman", "sudo pacman -S --needed"),
        ("zypper", "sudo zypper install -y"),
        ("apk", "sudo apk add"),
    ):
        if toolpath.which(manager):
            return manager, install
    return "", ""


def platform_caveat() -> str:
    """A warning to print where the commands are not verified. Empty when they are."""
    return _WINDOWS_CAVEAT if toolpath.platform_name() == "windows" else ""

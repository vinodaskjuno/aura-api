"""Where a command-line tool actually lives.

`shutil.which` answers "is it on THIS process's PATH", which is a different question
from "is it installed". The two diverge constantly on a developer machine: the podman
.pkg installs to `/opt/podman/bin`, Homebrew to `/opt/homebrew/bin`, and neither is on
the PATH of a process launched from anywhere other than an interactive login shell.

The shell scripts in aura-infra have known this all along — `release.sh`, `deploy.sh`
and `reset-dev.sh` each open with

    export PATH="/opt/podman/bin:$HOME/.local/bin:$PATH"

while the Python side used a bare `shutil.which("podman")` and refused to start on a
machine where those same scripts work. This module is that line, in Python, in one
place, so the emulators, the compose probe, the app server and the doctor cannot
disagree about whether a tool exists.

Candidates are appended AFTER the caller's PATH, never prepended: if a user has chosen
a particular podman, that choice wins. The one exception is `AURA_QA_PATH`, which is
prepended precisely because it is an explicit override.

Deliberately dependency-free — no settings, no logging config, nothing from boto3 — so
that the doctor can import it and the Fargate router can import the doctor.
"""
from __future__ import annotations

import os
import shutil
import sys

#: Explicit override, prepended. Mirrors the CONTAINER_CLI escape hatch in deploy.sh:
#: a machine with a tool somewhere unusual should not need a code change.
OVERRIDE_ENV = "AURA_QA_PATH"

#: Per-platform install locations that are not reliably on PATH. Kept as literals
#: rather than probed, because the point is to look where a package manager PUT
#: something without asking the shell.
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/opt/podman/bin",        # the podman .pkg — verified location on a dev Mac
        "/opt/homebrew/bin",      # Homebrew, Apple silicon
        "/opt/homebrew/sbin",
        "/usr/local/bin",         # Homebrew, Intel
        "~/.local/bin",
    ),
    "linux": (
        "/usr/local/bin",
        "/usr/bin",
        "~/.local/bin",
        "/home/linuxbrew/.linuxbrew/bin",
        "/var/lib/flatpak/exports/bin",
    ),
    "windows": (
        r"%ProgramFiles%\RedHat\Podman",
        r"%ProgramFiles%\Docker\Docker\resources\bin",
        r"%LOCALAPPDATA%\Microsoft\WindowsApps",
    ),
}


def platform_name() -> str:
    """"darwin" | "linux" | "windows". One indirection, so tests can fake a platform."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def extra_path_dirs() -> list[str]:
    """Candidate directories for this platform that actually exist, in order."""
    out: list[str] = []
    for raw in _CANDIDATES.get(platform_name(), ()):
        expanded = os.path.expandvars(os.path.expanduser(raw))
        # `%ProgramFiles%` stays literal on a non-Windows box, and a directory that is
        # not there is not a candidate — an empty PATH segment means "current
        # directory" to some tools, which is its own small hazard.
        if "%" in expanded or not expanded:
            continue
        if os.path.isdir(expanded) and expanded not in out:
            out.append(expanded)
    return out


def augmented_path(base: str | None = None) -> str:
    """The caller's PATH plus the candidates, de-duplicated, order preserved."""
    parts: list[str] = []

    def add(value: str) -> None:
        for chunk in value.split(os.pathsep):
            chunk = chunk.strip()
            if chunk and chunk not in parts:
                parts.append(chunk)

    override = os.environ.get(OVERRIDE_ENV, "")
    if override:
        add(override)                       # explicit, so it goes first
    add(base if base is not None else os.environ.get("PATH", ""))
    for directory in extra_path_dirs():
        add(directory)
    return os.pathsep.join(parts)


def which(name: str) -> str | None:
    """Absolute path to `name`, searching the install locations as well as PATH."""
    return shutil.which(name, path=augmented_path())


def env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The current environment with PATH augmented, for handing to subprocess.

    Needed as well as an absolute path, not instead of it: podman execs helpers
    (`gvproxy`, `vfkit`) out of its own directory, so calling an absolute podman with
    an unaugmented PATH still fails to start a machine.
    """
    merged = {**os.environ, "PATH": augmented_path()}
    if extra:
        merged.update(extra)
    return merged

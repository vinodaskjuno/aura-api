"""Finding a tool that is installed but not on this process's PATH.

The bug this fixes, verified on a real machine: podman's .pkg installs to
`/opt/podman/bin`, aura-infra's shell scripts export that directory, and the Python
side did a bare `shutil.which` — so the runner refused to start on a machine where
`release.sh` works.
"""
from __future__ import annotations

import os

import pytest

from src.qatest import toolpath


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "user-bin"))
    monkeypatch.delenv(toolpath.OVERRIDE_ENV, raising=False)
    (tmp_path / "user-bin").mkdir()
    return tmp_path


def test_a_candidate_directory_is_only_used_when_it_exists(monkeypatch, tmp_path):
    """An empty PATH segment means "the current directory" to some tools — a small
    hazard worth never creating."""
    monkeypatch.setattr(toolpath, "platform_name", lambda: "linux")
    monkeypatch.setattr(toolpath, "_CANDIDATES",
                        {"linux": (str(tmp_path / "here"), str(tmp_path / "gone"))})
    (tmp_path / "here").mkdir()

    dirs = toolpath.extra_path_dirs()
    assert dirs == [str(tmp_path / "here")]
    assert "" not in toolpath.augmented_path().split(os.pathsep)


def test_the_users_own_path_comes_first(clean_env, monkeypatch):
    """A deliberate choice of tool must win over a location we guessed at."""
    monkeypatch.setattr(toolpath, "platform_name", lambda: "linux")
    monkeypatch.setattr(toolpath, "_CANDIDATES", {"linux": (str(clean_env / "extra"),)})
    (clean_env / "extra").mkdir()

    parts = toolpath.augmented_path().split(os.pathsep)
    assert parts.index(str(clean_env / "user-bin")) < parts.index(str(clean_env / "extra"))


def test_the_override_is_prepended(clean_env, monkeypatch):
    """AURA_QA_PATH is explicit, so it outranks even the user's PATH — the same
    escape-hatch idiom as CONTAINER_CLI in deploy.sh."""
    monkeypatch.setenv(toolpath.OVERRIDE_ENV, str(clean_env / "override"))
    assert toolpath.augmented_path().split(os.pathsep)[0] == str(clean_env / "override")


def test_no_duplicate_segments(clean_env, monkeypatch):
    monkeypatch.setattr(toolpath, "platform_name", lambda: "linux")
    monkeypatch.setattr(toolpath, "_CANDIDATES",
                        {"linux": (str(clean_env / "user-bin"),)})
    parts = toolpath.augmented_path().split(os.pathsep)
    assert len(parts) == len(set(parts))


def test_which_finds_a_tool_outside_the_path(clean_env, monkeypatch):
    """The headline case: installed, not on PATH, must still be found."""
    import shutil
    import stat

    hidden = clean_env / "hidden-bin"
    hidden.mkdir()
    binary = hidden / "podman"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(toolpath, "platform_name", lambda: "linux")
    monkeypatch.setattr(toolpath, "_CANDIDATES", {"linux": (str(hidden),)})

    assert shutil.which("podman") is None, "fixture did not isolate PATH"
    assert toolpath.which("podman") == str(binary)


def test_env_carries_the_augmented_path(clean_env, monkeypatch):
    """An absolute path is not enough on its own: podman execs gvproxy and vfkit out
    of its own directory, so the child needs to see it too."""
    monkeypatch.setattr(toolpath, "platform_name", lambda: "linux")
    monkeypatch.setattr(toolpath, "_CANDIDATES", {"linux": (str(clean_env / "extra"),)})
    (clean_env / "extra").mkdir()

    assert str(clean_env / "extra") in toolpath.env()["PATH"]
    assert toolpath.env({"X": "1"})["X"] == "1"


def test_windows_placeholders_are_not_leaked_on_other_platforms(monkeypatch):
    """`%ProgramFiles%` does not expand off Windows; a literal percent sign in PATH
    would be a segment that can never match."""
    monkeypatch.setattr(toolpath, "platform_name", lambda: "windows")
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert all("%" not in d for d in toolpath.extra_path_dirs())


def test_platform_name_covers_the_three_we_support(monkeypatch):
    for value, expected in (("darwin", "darwin"), ("win32", "windows"),
                            ("linux", "linux"), ("freebsd13", "linux")):
        monkeypatch.setattr(toolpath.sys, "platform", value)
        assert toolpath.platform_name() == expected

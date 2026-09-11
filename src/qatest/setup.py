"""Walk a user through fixing what the doctor found, one step at a time.

The safety argument for this whole feature is one sentence: **a human read this exact
command and agreed to it.** Everything here exists to keep that true.

  * It runs only when the user types `--setup`. Nothing triggers it.
  * It takes no instruction from the Aura server. `--api`/`--key` are accepted only so
    it can REPORT progress outward; the response is never read. The server can watch an
    install it did not and cannot start.
  * Every command is printed in full before the prompt, and the prompt defaults to No.
  * It never runs `sudo` itself. Where root is needed it prints the command and hands
    over, because Aura should not be the thing that asked for your password.
  * There is no `--yes`. A flag whose purpose is to skip the reading would be pasted
    into CI and run as root, and then the sentence above is no longer true.

Windows is print-only in this release. Detection is safe to ship untested — the worst
case is that it says "cannot tell" — but executing an unverified installer is not.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone

from src.qatest import doctor, toolpath

#: Lines of console kept and sent to the UI. Bounded because the whole console is
#: resent on each report, for the same reason the run console is.
LOG_KEEP = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Reporter:
    """Sends progress to Aura, if asked to. Outbound only.

    Whether Aura is watching changes nothing about what runs — every prompt and every
    refusal is identical with and without it. This exists so a user can see the install
    on the Runner panel instead of only in a terminal.
    """

    def __init__(self, api: str, key: str, name: str) -> None:
        self.enabled = bool(api and key)
        self.name = name
        self.log: list[dict] = []
        self._http = None
        if not self.enabled:
            return
        try:
            import httpx
            self._http = httpx.Client(
                base_url=api.rstrip("/"),
                headers={"x-api-key": key,
                         "X-Aura-Runner-Protocol": "2",
                         "X-Aura-Runner-Name": name},
                timeout=15.0)
        except Exception:                                     # noqa: BLE001
            self.enabled = False

    def say(self, text: str) -> None:
        self.log.append({"at": _now(), "text": text[:200]})
        del self.log[:-LOG_KEEP]

    def send(self, *, step: str, index: int, total: int, active: bool = True) -> None:
        if not self.enabled or self._http is None:
            return
        try:
            from src.qatest.agent import _machine_state

            body = _machine_state()
            body["setup"] = {"active": active, "step": step, "index": index,
                             "total": total, "log": list(self.log)}
            # The response carries `commands`. It is deliberately NOT read: this path
            # reports outward and takes no instruction. That is what keeps an install
            # something a person started.
            self._http.post("/api/qa/runner/state", json=body)
        except Exception:                                     # noqa: BLE001
            pass      # a watching UI is a convenience; losing it must not stop a setup

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:                                 # noqa: BLE001
                pass


def _needs_root(command: str) -> bool:
    return command.strip().startswith("sudo ")


def _ask(prompt: str) -> str:
    """Default No. An empty answer must never be taken as consent."""
    try:
        return (input(prompt).strip().lower() or "n")
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def run_setup(dry_run: bool = False, api: str = "", key: str = "",
              name: str = "runner") -> int:
    windows = toolpath.platform_name() == "windows"
    # Windows commands are unverified, so the whole run is print-only there.
    print_only = dry_run or windows

    reporter = _Reporter(api, key, name)
    try:
        return _loop(reporter, print_only, windows)
    finally:
        reporter.close()


def _loop(reporter: _Reporter, print_only: bool, windows: bool) -> int:
    doctor.invalidate()
    diag = doctor.diagnose(deep=True)

    print(f"\nQualityMind runner setup — {diag.platform}/{diag.arch}\n")
    for line in diag.lines():
        print(line)

    caveat = doctor.platform_caveat()
    if caveat:
        print(f"\n  NOTE: {caveat}")
    if windows:
        print("  Every step below is printed for you to run yourself.")
    elif print_only:
        print("\n  Dry run — nothing will be executed.")

    todo = [f for f in diag.blocking + diag.degraded if f.remedy]
    if not todo:
        print("\n  Nothing to fix. This machine is ready.\n")
        reporter.say("nothing to fix — the machine is ready")
        reporter.send(step="ready", index=0, total=0, active=False)
        return 0

    if reporter.enabled:
        print(f"\n  Progress is being reported to Aura as '{reporter.name}'.")

    total = len(todo)
    for index, finding in enumerate(todo, start=1):
        print(f"\n── {index} of {total} — {finding.title} "
              f"{'(blocks runs)' if finding.severity == doctor.BLOCKS else '(limits runs)'}")
        if finding.detail:
            print(f"   {finding.detail}")
        print()
        for command in finding.remedy:
            print(f"   $ {command}")

        reporter.say(f"{index}/{total} {finding.title}")
        reporter.send(step=finding.title, index=index, total=total)

        if not finding.fixable or print_only:
            reason = ("this platform is unverified" if windows
                      else "dry run" if print_only
                      else "run this yourself — Aura will not do it for you")
            print(f"\n   [--] not run ({reason})")
            reporter.say(f"printed only: {reason}")
            continue

        answer = _ask("\n   Run this now? [y/N/s/q] ")
        if answer == "q":
            print("\n   Stopped. Remaining problems are listed above.\n")
            reporter.say("stopped by the user")
            reporter.send(step="stopped", index=index, total=total, active=False)
            return 1
        if answer != "y":
            print("   [--] skipped")
            reporter.say(f"skipped: {finding.title}")
            continue

        ok = _run_steps(finding.remedy, reporter)
        doctor.invalidate()
        recheck = doctor.diagnose(deep=True).find(finding.check)
        if recheck is not None and recheck.ok:
            print("   [ok] fixed")
            reporter.say(f"fixed: {finding.title}")
        elif ok:
            print("   [!!] the command finished but the check still fails")
            reporter.say(f"still failing after the command: {finding.title}")
        else:
            print("   [!!] the command failed — see the output above")
            reporter.say(f"command failed: {finding.title}")

    doctor.invalidate()
    final = doctor.diagnose(deep=True)
    print()
    for line in final.lines():
        print(line)
    if final.ok:
        print("\n  Ready. Start the runner with:"
              "\n    python -m src.qatest.agent --api <aura-url> --key gw-…\n")
    else:
        print(f"\n  {len(final.blocking)} problem(s) still block a run.\n")

    reporter.say("ready" if final.ok else
                 f"{len(final.blocking)} problem(s) remain")
    reporter.send(step="finished", index=len(todo), total=len(todo), active=False)
    return 0 if final.ok else 1


def _run_steps(commands: tuple[str, ...], reporter: _Reporter) -> bool:
    for command in commands:
        if _needs_root(command):
            # Aura must not be the thing that asked for your password. Also no `sudo -n`
            # probe: silently inspecting someone's sudoers is its own surprise.
            print(f"\n   This one needs root. Run it in your own shell:\n"
                  f"     {command}")
            reporter.say(f"needs root, handed over: {command[:120]}")
            _ask("   Press Enter when it is done (or q to stop) ")
            continue
        if not command.strip() or command.strip().startswith(("install ", "free up")):
            # Prose, not a command — the doctor uses these where there is no single
            # line we can honestly offer.
            print(f"   (do this yourself: {command})")
            continue

        print(f"\n   $ {command}")
        reporter.say(f"$ {command[:160]}")
        try:
            # Inherited, NOT captured: `podman machine init` prints a progress bar and
            # a captured one looks hung for several minutes.
            result = subprocess.run(command, shell=True, env=toolpath.env())
        except Exception as exc:                              # noqa: BLE001
            print(f"   could not run it: {exc}")
            reporter.say(f"could not run it: {exc}")
            return False
        if result.returncode != 0:
            reporter.say(f"exited {result.returncode}")
            return False
    return True

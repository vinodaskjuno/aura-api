"""The self-hosted QA runner.

Run this on a machine that has podman, Chromium and the project's cloned working copy —
typically a developer laptop. It claims runs queued from the Aura UI, executes them
locally, and uploads evidence to the same S3 bucket the Results tab reads. The button in
a deployed environment goes live without any AWS compute existing.

    python -m src.qatest.agent --api https://aura-dev-….elb.amazonaws.com --key gw-…

Why a poll loop and not a push: polling needs no inbound port, no public hostname and no
NAT traversal on the machine running it, and it survives a laptop sleeping — it simply
stops claiming. The alternative would have required dispatch permissions
(`ecs:RunTask` / `lambda:InvokeFunction`) that nothing in this deployment has.

What this deliberately does NOT do:

  * talk to Neo4j — it cannot; the plan arrives with the claim
  * write to the knowledge graph — the API does that from the stored report
  * hold long-lived AWS credentials — each claim carries scoped, 1-hour ones
  * run two runs at once — the floci emulators publish FIXED host ports
    (-p 4566:4566), so two concurrent runs needing the same cloud would collide
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

log = logging.getLogger("qa-runner")

POLL_SECONDS = 5
#: Heartbeat at most this often. Every step would be a request per navigation.
HEARTBEAT_MIN_INTERVAL_S = 10


def _preflight() -> list[str]:
    """Everything that must be true before claiming anything. Returns problems.

    Checks Chromium by LAUNCHING it, not by importing playwright. `_playwright_available`
    only does `import playwright`, so a machine with the package and no browser binary
    passes it and then fails at `chromium.launch()` — after a run has been claimed and
    marked running, which is the worst possible moment to find out.
    """
    from src.qatest.emulators import podman_available
    from src.qatest.runner import _playwright_available

    problems = []
    if not podman_available():
        problems.append("podman is not on PATH")

    ok, why = _playwright_available()
    if not ok:
        problems.append(why)
    else:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                pw.chromium.launch(args=["--no-sandbox"]).close()
        except Exception as exc:                              # noqa: BLE001
            problems.append(f"chromium will not launch ({type(exc).__name__}: "
                            f"{str(exc)[:120]}). Try: playwright install chromium")
    return problems


class Client:
    """The three calls the runner makes. Thin on purpose."""

    def __init__(self, api: str, key: str, name: str) -> None:
        import httpx

        self.name = name
        self._http = httpx.Client(
            base_url=api.rstrip("/"),
            headers={"x-api-key": key},
            timeout=30.0,
        )

    def claim(self) -> dict | None:
        response = self._http.get("/api/qa/runner/next")
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

    def heartbeat(self, run_id: str, project_id: str, phase: str,
                  counts: dict | None = None) -> None:
        try:
            self._http.post(f"/api/qa/runner/{run_id}/heartbeat",
                            params={"projectId": project_id, "phase": phase},
                            json={"phase": phase, **(counts or {})})
        except Exception as exc:                              # noqa: BLE001
            # A dropped heartbeat must not abort a run that is otherwise fine. The
            # reaper's window is minutes, so a few failures are survivable.
            log.debug("heartbeat failed: %s", exc)

    def finish(self, run_id: str, project_id: str, report: dict) -> None:
        response = self._http.post(f"/api/qa/runner/{run_id}/finish",
                                   params={"projectId": project_id},
                                   json={"report": report})
        response.raise_for_status()


def _apply_credentials(creds: dict | None) -> None:
    """Put the claim's scoped credentials where boto3 will find them.

    This is the whole reason evidence.py, s3_client.py and the DynamoDB index need no
    changes: they keep using boto3's default credential chain, and the process env is
    part of that chain.

    None means the API minted nothing — normal for local development, where the
    developer's own credentials are already configured.
    """
    if not creds or not creds.get("accessKeyId"):
        # CLEAR anything a previous run left behind. Inheriting them would be both
        # wrong and quietly dangerous: the last run's session policy is scoped to the
        # last run's S3 prefix, so this run would fail with AccessDenied naming a
        # session id that is not its own — which is exactly how confusing that is to
        # debug.
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            os.environ.pop(var, None)
        _reset_aws_clients()
        log.info("no scoped credentials in the claim; using the ambient AWS config")
        return

    os.environ["AWS_ACCESS_KEY_ID"] = creds["accessKeyId"]
    os.environ["AWS_SECRET_ACCESS_KEY"] = creds["secretAccessKey"]
    os.environ["AWS_SESSION_TOKEN"] = creds["sessionToken"]
    if creds.get("region"):
        os.environ["AWS_DEFAULT_REGION"] = creds["region"]

    # Setting the env is NOT enough, and this was found the hard way on dev: the run
    # executed, then every upload failed with InvalidAccessKeyId.
    #
    # Two reasons, both in src/storage/s3_client.py:
    #   1. _get_client() passes settings.aws_access_key_id EXPLICITLY when it is set,
    #      which overrides the environment entirely. A developer machine with stale keys
    #      in .env therefore ignores the scoped credentials it was just handed.
    #   2. _client and _account_id_cache are module-level caches, so a client built
    #      before this point would keep the old identity for the life of the process.
    from src.config_settings import get_settings

    settings = get_settings()
    settings.aws_access_key_id = ""
    settings.aws_secret_access_key = ""
    if creds.get("region"):
        settings.s3_region = creds["region"]
    _reset_aws_clients()

    log.info("using scoped credentials for this run, expiring %s",
             creds.get("expiresAt", "?"))


def _reset_aws_clients() -> None:
    """Force every AWS client to resolve credentials again.

    Setting the environment is not sufficient, and this took a second dev run to find.
    `boto3.client()` uses the module-level DEFAULT_SESSION when one exists, and a
    session CACHES the credentials it resolved — so the second run kept writing with
    the first run's assumed-role session, whose policy is scoped to the first run's S3
    prefix. The symptom was an AccessDenied naming `qa-runner-<previous run id>` while
    uploading the current run's report.

    Aura's own module-level caches have to go too: s3_client._client,
    s3_client._account_id_cache and dynamo_client._resource all outlive a run.
    """
    import boto3

    from src.database import dynamo_client
    from src.storage import s3_client

    boto3.DEFAULT_SESSION = None
    s3_client._client = None
    s3_client._account_id_cache = ""
    dynamo_client._resource = None


def run_one(client: Client, job: dict) -> str:
    """Execute one claimed run. Returns its final status."""
    from src.qatest.service import execute

    run_id, project_id = job["runId"], job["projectId"]
    _apply_credentials(job.get("credentials"))

    last_beat = [0.0]
    tally = {"totalPassed": 0, "totalFailed": 0, "totalSkipped": 0,
             "totalCases": len(job.get("cases") or [])}

    def on_event(event: dict) -> None:
        phase = event.get("type", "")

        # Count as they happen, so the UI can show 4/12 rather than just "running".
        if phase == "step":
            key = {"passed": "totalPassed", "failed": "totalFailed",
                   "skipped": "totalSkipped", "unemulated": "totalSkipped"}.get(
                       event.get("status", ""))
            if key:
                tally[key] += 1

        # Log every event — this is the operator's only view of a remote run — but rate
        # limit the network call. A step always beats: the counts are the point, and a
        # 10-second stale count on a 5-second run would never be seen at all.
        log.info("  %-9s %s", phase, str(event.get("message") or "")[:110])
        now = time.monotonic()
        if phase in ("step", "done", "error") or \
                now - last_beat[0] >= HEARTBEAT_MIN_INTERVAL_S:
            last_beat[0] = now
            client.heartbeat(run_id, project_id, phase, dict(tally))

    log.info("running %s for project %s (%d cases)",
             run_id, project_id, len(job.get("cases") or []))

    # Fetch Aura's copy of the code and install its dependencies BEFORE the run. A
    # project analysed inside a deployed container exists nowhere on this machine, and
    # `appserver` starts the app under test from the local filesystem — so without this
    # every run of such a project ends as "No working copy found".
    #
    # Reported through the same event stream as the run itself, so a first run that
    # spends three minutes in `npm ci` looks busy rather than hung.
    from src.qatest import provision

    problems = provision.prepare(project_id, job.get("workspace"), on_event)
    for problem in problems:
        log.warning("  provisioning: %s", problem)

    try:
        report = execute(
            project_id,
            app_url=job.get("appUrl", ""),
            run_id=run_id,
            ran_by=job.get("ranBy", "") or client.name,
            exploratory=bool(job.get("exploratory")),
            # The API does the write-back; graph_writeback needs Neo4j, which is not
            # reachable from here.
            write_graph=False,
            on_event=on_event,
            cases=job.get("cases"),
            clouds=job.get("clouds"),
        )
    except Exception as exc:                                  # noqa: BLE001
        # Report the failure rather than dying silently — otherwise the run sits at
        # `running` until the reaper gets it, minutes later, with no explanation.
        log.exception("run %s failed", run_id)
        report = {"runId": run_id, "projectId": project_id, "status": "unavailable",
                  "reason": f"the runner failed: {type(exc).__name__}: {exc}"[:400]}

    client.finish(run_id, project_id, report)
    status = report.get("status", "?")
    log.info("finished %s: %s (%s passed, %s failed, %s skipped)", run_id, status,
             report.get("totalPassed"), report.get("totalFailed"),
             report.get("totalSkipped"))
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.qatest.agent",
        description="Execute QualityMind runs queued from an Aura environment.")
    parser.add_argument("--api", required=True,
                        help="Aura base URL, e.g. https://aura-dev-….elb.amazonaws.com")
    parser.add_argument("--key", default=os.getenv("AURA_QA_RUNNER_KEY", ""),
                        help="gateway key (gw-…); defaults to $AURA_QA_RUNNER_KEY")
    parser.add_argument("--name", default=os.uname().nodename if hasattr(os, "uname")
                        else "runner", help="label shown in the UI")
    parser.add_argument("--once", action="store_true",
                        help="claim at most one run, then exit (useful in CI)")
    parser.add_argument("--poll", type=int, default=POLL_SECONDS,
                        help=f"seconds between polls (default {POLL_SECONDS})")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    if not args.key:
        print("ERROR: no key. Pass --key gw-… or set AURA_QA_RUNNER_KEY.\n"
              "Mint one from the Aura UI with the tool label 'qa-runner'.",
              file=sys.stderr)
        return 2

    problems = _preflight()
    if problems:
        print("ERROR: this machine cannot execute runs:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    client = Client(args.api, args.key, args.name)
    log.info("runner %r ready, polling %s every %ss", args.name, args.api, args.poll)

    while True:
        try:
            job = client.claim()
        except Exception as exc:                              # noqa: BLE001
            # Includes a revoked key (401) and the API being redeployed. Keep polling —
            # the operator's fix is to restore the key, not to restart the agent.
            log.warning("claim failed: %s", exc)
            job = None

        if job:
            run_one(client, job)
            if args.once:
                return 0
        elif args.once:
            log.info("nothing queued")
            return 0
        else:
            time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())

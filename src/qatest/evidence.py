"""Write a run's evidence to S3, and read it back.

S3 is the source of truth for a run. That is what lets a test executed on a laptop be
reviewed from the deployed UI: both point at the same
`aura-<accountId>-test-artifacts` bucket, so the deployed backend needs no ability to
RUN a test in order to SHOW one.

Layout, one prefix per run:

    {projectId}/{runId}/report.json               the summary — the only object a reader needs first
    {projectId}/{runId}/steps.jsonl               one line per step, with its screenshot key
    {projectId}/{runId}/screenshots/step-0001.png one per step, ordered stable names
    {projectId}/{runId}/specs/<name>              what actually ran, so the run is reproducible
    {projectId}/{runId}/console.log               browser console errors and failed requests

Stable screenshot names are deliberate: comparing step-N of two runs is then a
straight lookup, which is what makes visual diffing cheap to add later.

Reading uses `report.json` alone rather than scanning DynamoDB. The endpoints this
replaces called `scan_items("test-results", limit=500)` per request — a full-table
scan that also hid every run past the first 500 items.
"""
from __future__ import annotations

import json
import logging

from src.qatest.types import Report, Step

log = logging.getLogger(__name__)

BUCKET = "test-artifacts"
REPORT = "report.json"
STEPS = "steps.jsonl"


def run_prefix(project_id: str, run_id: str) -> str:
    return f"{project_id}/{run_id}"


def screenshot_key(project_id: str, run_id: str, index: int) -> str:
    return f"{run_prefix(project_id, run_id)}/screenshots/step-{index:04d}.png"


def write_screenshot(project_id: str, run_id: str, index: int, png: bytes) -> str:
    """Store one step's screenshot. Returns the key, which goes into the step record."""
    from src.storage.s3_client import put_object
    key = screenshot_key(project_id, run_id, index)
    put_object(BUCKET, key, png, "image/png")
    return key


def write_steps(project_id: str, run_id: str, steps: list[Step]) -> str:
    """Append-only step log, one JSON object per line.

    JSONL rather than a JSON array so a long run streams and a truncated upload still
    parses up to the last complete line.
    """
    from src.storage.s3_client import put_object
    key = f"{run_prefix(project_id, run_id)}/{STEPS}"
    body = "\n".join(json.dumps(s.as_dict()) for s in steps)
    put_object(BUCKET, key, body.encode(), "application/x-ndjson")
    return key


def write_spec(project_id: str, run_id: str, name: str, source: str) -> str:
    from src.storage.s3_client import put_object
    key = f"{run_prefix(project_id, run_id)}/specs/{name}"
    put_object(BUCKET, key, source.encode(), "text/plain")
    return key


def write_console(project_id: str, run_id: str, lines: list[str]) -> str:
    from src.storage.s3_client import put_object
    key = f"{run_prefix(project_id, run_id)}/console.log"
    put_object(BUCKET, key, ("\n".join(lines)).encode(), "text/plain")
    return key


def write_report(report: Report) -> str:
    """Written LAST, so its presence means the run finished writing.

    A reader that finds a prefix with no report.json is looking at a run that died
    mid-write, and says so rather than showing half a result as if it were whole.
    """
    from src.storage.s3_client import put_json
    key = f"{run_prefix(report.project_id, report.run_id)}/{REPORT}"
    put_json(BUCKET, key, report.as_dict())
    log.info("qatest: report written to s3://.../%s", key)
    return key


# ── Read side ────────────────────────────────────────────────────────────────

def read_report(project_id: str, run_id: str) -> dict | None:
    from src.storage.s3_client import get_json
    return get_json(BUCKET, f"{run_prefix(project_id, run_id)}/{REPORT}")


def read_steps(project_id: str, run_id: str) -> list[dict]:
    from src.storage.s3_client import get_object
    raw = get_object(BUCKET, f"{run_prefix(project_id, run_id)}/{STEPS}")
    if not raw:
        return []
    steps = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            steps.append(json.loads(line))
        except json.JSONDecodeError:
            # A partially flushed final line is expected on an interrupted run;
            # everything before it is still valid evidence.
            log.debug("qatest: skipping unparsable step line")
    return steps


def list_runs(project_id: str) -> list[str]:
    """Run ids for a project, newest first.

    Derived from the key prefixes rather than a database, so the listing cannot
    disagree with what is actually stored.
    """
    from src.storage.s3_client import list_objects
    run_ids: set[str] = set()
    for obj in list_objects(BUCKET, f"{project_id}/"):
        parts = str(obj.get("key", "")).split("/")
        if len(parts) >= 3 and parts[2] == REPORT:
            run_ids.add(parts[1])
    return sorted(run_ids, reverse=True)


def screenshot_urls(project_id: str, run_id: str, expires: int = 3600) -> dict[str, str]:
    """Presigned URL per screenshot key, so the browser can load them directly."""
    from src.storage.s3_client import list_objects, presigned_url
    urls: dict[str, str] = {}
    for obj in list_objects(BUCKET, f"{run_prefix(project_id, run_id)}/screenshots/"):
        key = str(obj.get("key", ""))
        if key.endswith(".png"):
            try:
                urls[key] = presigned_url(BUCKET, key, expires=expires)
            except Exception as exc:  # noqa: BLE001 — one bad key must not hide the rest
                log.debug("qatest: presign failed for %s: %s", key, exc)
    return urls

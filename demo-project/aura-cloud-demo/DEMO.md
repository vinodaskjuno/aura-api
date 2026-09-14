# Demo: what a test run actually touches

**The claim:** QualityMind runs your tests on a real machine, against real cloud services
running locally — and can show you exactly which resources they used.

Everything below works on the deployed dev URL, with nothing installed on the viewer's
machine. Only the presenter needs podman.

---

## Why this app exists

A green test proves the application answered. It does not prove the application *reached*
anything — a route returning a literal passes identically with no emulator running at all.

Every route here reads back what startup wrote, so a passing case is evidence the emulator
was used:

| Route | Reads |
|---|---|
| `GET /catalog` | DynamoDB `aura-demo-catalog` — 3 seeded items |
| `GET /media` | S3 `aura-demo-media` — 3 seeded objects |
| `GET /events` | SQS `aura-demo-events` — queue depth |
| `GET /notify` | SNS `aura-demo-notify` |
| `GET /pricing` | Lambda — **expected to report unavailable**, see below |

All plain, un-parameterised `GET`s, because Aura's planner skips anything else: a POST
"needs a request body the graph does not describe" and a path parameter "has no known
value". A `POST /orders` would be planned and then skipped, proving nothing.

## Setup, once

```bash
python demo-project/setup_cloud_demo.py --api <aura> --user admin --password <pw>
```

Creates the project, clones this code into the workspace, and analyses it — which is what
builds the graph the test plan comes from.

## The run of show

1. **DevMate → Start** on the Floci control.
   It reads *"starting…"* for a few seconds. That is honest: the API is on Fargate and
   cannot start a container on your laptop, so it parks a request the runner collects on
   its next poll.
   → `podman ps` shows `aura-dev-aws-<projectId>`.

2. **QualityMind → run the tests.**
   The run **adopts** the emulator you started rather than starting its own, and leaves it
   running at the end. Watch the terminal: the resource lines appear as the run finishes.

3. **The run's "Cloud resources" panel** — the bucket, the table, the queue, the topic,
   with counts. This is read out of the emulator at the end of the run, so it survives the
   run and the containers.

4. **Runner tab → Inspect.** The same rows, read live from the still-running emulator.
   The header says *"as of 8s ago"* and never "live" — it is a round trip through the
   runner's poll, not a stream.

5. **Stop**, in DevMate, when you are done.

## Two things to know before presenting

**Floci keeps state in memory by default.** Stopping and starting the container empties
it. That is why the demo starts the emulator once, in step 1, and leaves it up — and why
step 4 still has something to show. If you want state to survive a restart, start Floci
with `FLOCI_STORAGE_MODE=persistent`.

**`GET /pricing` is meant to report unavailable.** Lambda is one of Floci's
container-backed services and needs a container runtime socket, which Aura mounts only
when explicitly enabled. A run where only that case is `unemulated` is a healthy run —
`unemulated` means "we could not check", which is deliberately distinct from "failed".
Do not enable it for the first rehearsal.

## Multi-cloud, optional

Uncomment `google-cloud-storage` and `azure-storage-blob` in `backend/requirements.txt`
and re-analyse. Three emulators start instead of one, and `/assets` and `/blobs` come
alive — which demonstrates that emulator selection is *derived from your dependencies*
rather than configured anywhere. It costs two more containers per run.

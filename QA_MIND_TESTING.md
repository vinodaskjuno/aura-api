# QA Mind — manual test guide

Everything below runs against a **local** Aura. The whole point of this feature is that
tests execute on *your* machine with real Floci containers, so the interesting parts
cannot be checked from a deployed environment alone.

Expect the run to take 1–3 minutes the first time (podman pulls the Floci image, and
`npm ci` / `pip install` run for the project under test). Later runs are much faster.

---

## 0. Prerequisites

```bash
python -m src.qatest.agent --doctor      # checks everything, needs no key or URL
```

It reports what is missing, why it matters, and the exact command for your platform:

```
  [ok] podman.present         podman found
  [!!] podman.working         the podman machine is not running
  [ok] browser.binary         Chromium installed (1234)
  [--] compose.provider       no compose provider is installed

  BLOCKS: the podman machine is not running
    podman is installed, but its virtual machine is stopped — so every podman
    command fails. This is the most common cause of a runner that looks healthy
    and then fails mid-run.
    $ podman machine start
```

`python -m src.qatest.agent --setup` walks through fixing them, printing each command
and asking before it runs anything. It defaults to **No**, never runs `sudo` itself,
and has no `--yes`. Add `--dry-run` to see every command without executing any.

**Aura never installs anything on its own, and the server cannot make it.** `--setup`
runs only because you typed it. Pass `--api`/`--key` to it and progress appears on the
Runner tab — that channel reports outward and reads nothing back.

---

## 1. Start Aura

```bash
cd aura-api  && .venv/bin/uvicorn src.main:app --reload --port 8000
cd aura-ui/frontend && npm run dev          # http://localhost:5173
```

Sign in and open **QualityMind**.

> **First thing to notice:** the project list is now *empty* if you have no analysed
> projects. It used to open on a fabricated "demo" project with a pass rate nobody
> earned, and clicking into Artifacts showed hand-drawn mock-ups of Aura's own login
> page. That is gone. If the list is empty, analyse a project in Dev Workspace first.

---

## 2. Get a project in

DevMate → **New project** → upload a folder with a Python or Node app → let the wizard
build the knowledge graph.

A project with `boto3` in its requirements is ideal — it makes Floci start the AWS
emulator, which is what step 6 is about. `aura-api/demo-project/workfusion-claims/`
has `boto3`, `hvac` and `splunk-sdk` in its manifest.

Open **Onto Verse** and confirm the project has `API` and `Service` nodes. If it has
none, the plan preview in step 5 will say so in plain words rather than showing you an
empty picker.

---

## 3. Start the runner  ← *the "Floci on your machine" feature*

Mint a gateway key in the UI with tool label **`qa-runner`**, then:

```bash
cd aura-api
.venv/bin/python -m src.qatest.agent --api http://localhost:8000 --key gw-…
```

**In QualityMind, check:**

| Where | What you should see |
|---|---|
| Page header (any tab) | a green chip: `● <your-machine> · podman ✓` |
| **Runner** tab | one card: your runner, podman ✓, Chromium ✓, OS, "last seen" ticking |
| **Runner** tab | *"No Floci containers are running"* — explanatory, not an error |

Now **stop the agent** (Ctrl-C) and wait ~90s. The chip goes grey and the runner card
says **stopped reporting**. Restart it and it comes back. A sleeping laptop must never
leave the panel claiming containers are up.

Useful flags:
- `--no-container-logs` — never send container output to Aura
- `--report-all-containers` — report every podman container, not just Aura's (off by
  default: your other containers are nobody else's business)

---

## 4. Verify the runner only reports Aura's own containers

With the agent running, start something unrelated:

```bash
podman run -d --name my-own-thing docker.io/library/redis:alpine
```

The Runner tab must **not** list `my-own-thing`. Clean up with
`podman rm -f my-own-thing`.

---

## 5. Choose what to run  ← *test case type selection*

Project card → **Start a run**.

**Check:**
- The picker shows real counts: `Application 1 · API routes N · Services M`
- **Application** is shown but not clickable — it always runs, because it is the only
  case a frontend can be tested by
- *"Show the N cases"* lists them with `method path`, and marks the ones that will be
  skipped
- A line explains how many cannot execute and why (non-GET routes need a request body
  the graph does not describe; parameterised paths have no known value)
- **The Start button reads `Run 12 of 18 cases`** and the number changes as you toggle
  kinds. That is the whole point of offering the choice.

Untick **Services**, leave **API routes** on, press Start.

---

## 6. Watch Floci come up  ← *the important one*

While the run executes, look at the launcher **and** the Runner tab.

**Launcher:** phase stepper advances, and under it a live list —
`aws · :4566 · aura-qa-aws-<runId> · starting… → ready`.

**Runner tab:** the container appears in the table with its image, port and uptime.

**Confirm it is real** — in another terminal:

```bash
podman ps
```

You should see exactly `aura-qa-aws-<runId>` and **nothing else**. A project with no
cloud dependency starts *no* containers; one with only `boto3` starts one, not four.
That selection is derived from the `Dependency` nodes in the graph, so it cannot drift
from the code.

**When the run finishes**, the container disappears from both `podman ps` and the
panel. A leaked emulator holds its port and the next run fails for a reason that looks
nothing like the cause — so this matters.

---

## 7. Progress percentage

Watch the bar during the run.

- Before the runner claims it: the bar **sweeps** and says `plan size not known yet`.
  It must never read `0%` — that would claim nothing has worked.
- Once claimed: `n of m` with a real percentage.
- **It must reach 100%** and stop there.

That last point is the bug this release fixes. Skipped cases previously never reported,
so on any project with POST routes or Services the bar stalled short of the end and sat
there looking hung. Pick a project with several non-GET routes to see it now finish.

---

## 8. Fetch container logs

Runner tab → **Logs** on a running container.

- The drawer says *"asking `<runner>` for logs…"*, then shows output
- The header reads **`podman logs · as of 14:22:31`** — deliberately **not** "live".
  The runner has no inbound port; it collects the request on its next poll, up to ~15s
  later. A control that claims to stream but updates every fifteen seconds is worse
  than an honest one.
- **Logs** is offered only on `aura-qa-*` containers. The server refuses anything else
  with a 400, and the agent refuses it too — both sides check.

> Container output travels from your machine into the shared `test-artifacts` bucket
> and is readable by anyone with `qa_workspace`. It expires after 7 days. Use
> `--no-container-logs` if you would rather it never left the machine.

---

## 9. Coverage

Open the finished run, and the **Coverage** tab.

Two numbers, and they answer different questions:

```
Graph coverage 40%   2 of 5 API and Service nodes verified by a passing test
Plan executed 100%   5 of 5 planned cases ran
```

**Check the numbers add up:** covered + uncovered listed + "not in this run's plan"
must equal the total. Expand *"N not covered"* — every entry names a reason:

```
POST needs a request body the graph does not describe (1)
  POST /items · API
path parameter has no known value (1)
  GET /items/{id} · API
```

> **Graph coverage will read low — around a third is normal — and that is correct.**
> Non-GET routes and parameterised paths cannot be executed, and a Service node names a
> code unit rather than an address, so smoke cases are always skipped (Services are
> reported separately for exactly that reason). The reasons list is what makes a low
> number actionable. If it ever reads high by counting skipped cases as covered, the
> measurement is worthless.

---

## 10. The run detail

Click any run in **Test Runs**.

There is now **one** detail view instead of two that disagreed. Check it shows:

- [ ] Run id, duration, who ran it, the app URL
- [ ] `passed / failed / skipped / **not emulated**` — the last was never shown before,
      and is the difference between "your app is broken" and "we could not check"
- [ ] Both coverage numbers
- [ ] **Test cases** — each with its kind, `method path`, **which graph node it
      verifies**, and its source file. Expand one for its steps, screenshots and errors
- [ ] Filter chips; defaults to **Failed** when there are failures
- [ ] **Cloud emulators** — including any that **failed to start**, with the error.
      The old view filtered those out, hiding precisely the emulator whose failure
      explains a run full of "not emulated" results
- [ ] **Console output** — browser errors and failed requests, captured since the
      beginning and never displayed until now
- [ ] Artifacts, with screenshots marked pass/fail from the **step that produced them**
      rather than guessed from the filename
- [ ] **Re-run** and **Re-run failed**

---

## 11. Degradation — worth doing deliberately

| Do this | Expect |
|---|---|
| Stop the agent, press **Start a run** | Queued, bar sweeps, and the launcher says no runner is connected |
| Stop the agent mid-run, wait ~15 min | The reaper marks the run **abandoned**, and its emulators grey out rather than showing phantoms |
| Uninstall/rename podman, start a run | Status `unavailable` with the reason naming which emulators were wanted — never a false pass |
| Open QualityMind with no runner ever started | Runner tab shows one card with the exact `python -m src.qatest.agent …` command and a copy button |

The rule throughout: **a control whose backend cannot serve it is not rendered** — never
shown-and-disabled, never shown-then-404.

---

## 12. Automated checks

```bash
cd aura-api && .venv/bin/python -m pytest src/tests -q      # 821 passing
cd aura-ui/frontend && npx tsc -p tsconfig.app.json --noEmit # 87 pre-existing errors
cd aura-ui/frontend && npm run build
```

> Note: plain `npx tsc --noEmit` checks **nothing** — the root `tsconfig.json` is
> `"files": []` with project references. Use `-p tsconfig.app.json`.

---

## What to look at first, if you only have ten minutes

1. Steps 3 and 6 — the runner card, and `podman ps` matching the panel while a run
   executes. That is the feature you asked for.
2. Step 7 — the bar reaching 100% on a project with POST routes.
3. Step 9 — coverage with reasons, and confirming the numbers add up.

---

# Testing a project that does not serve HTTP

Added after the WorkFusion demo hit the wall: an RPA application has no ASGI module
and no npm `dev` script, so every run of it reported `unavailable` and said nothing.
Two new case kinds fix that, and between them they cover both ends of a migration.

## File checks (`structure`) — no server needed

Run the WorkFusion project. It now **passes with 18 checks** instead of reporting
`unavailable`:

```
passed  claims-intake.bpmn — parses as BPMN — 1 process: claims-intake
passed  claims-intake.bpmn — every sequence flow resolves — 8 flows all resolve
passed  claims-intake.bpmn — every script task names a file that exists — 1 resolves
passed  ClaimsValidation.groovy — brackets balance (not compiled — that needs a JVM)
passed  bot-config.xml — parses as XML — root element <bots>
```

**Prove it catches things.** Break one deliberately — point a `sequenceFlow` at an id
that does not exist:

```bash
# in demo-project/workfusion-claims/src/main/resources/processes/claims-intake.bpmn
#   targetRef="ocr-invoice"   →   targetRef="ghost"
```

Re-run: that check goes **failed**, naming `ghost`. `failed` means *we looked and it
is wrong*; `unavailable` means *we could not look*. That difference is the point.

Each passing check says what it verified, and none claims more than it did — Groovy
brackets balance rather than "compiles", DAGs parse rather than "import".

## Running stack (`stack`) — for converted output

A migration download now ships a `docker-compose.yml`, a `RUNNING.md` and the folders
Airflow needs, beside the generated DAGs. QualityMind starts that stack and asks three
questions:

| | |
|---|---|
| Airflow reports itself healthy | `/health`, per component |
| **every converted DAG imports without error** | `/api/v1/importErrors` |
| the converted DAGs are registered | `/api/v1/dags` |

The middle one is the one that matters. **An import error does not make a DAG fail —
it makes it absent.** Airflow logs it and carries on, the DAG never appears, and
nothing anywhere goes red. A migration that produced nothing usable looks clean from
every other angle.

### What you need for this half

```bash
brew install docker-compose       # macOS; podman 5.x probes for this BINARY,
                                  # not the `podman-compose` pip package
podman compose version            # must print a version
```

**Without a compose provider the stack cannot start** — the run says so in those
words rather than failing obscurely. `podman compose` is preferred, since a machine
set up for the Floci emulators already has podman.

First boot initialises Airflow's database and takes **2–5 minutes**; the readiness
check allows 300s and probes `/health` rather than the socket, because the container
publishes its port long before the application can serve.

### Try it by hand

```bash
cd <the unzipped migration output>
docker compose up                 # or: podman compose up
open http://localhost:8080        # aura / aura
```

That stack is one container on SQLite with the LocalExecutor — enough to load DAGs
and trigger one, and explicitly not a deployment. `RUNNING.md` in the zip lists what
to change first.

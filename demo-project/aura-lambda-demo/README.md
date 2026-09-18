# aura-lambda-demo

Two AWS Lambda functions that really execute, in containers, on the machine running the
Aura runner — while Aura itself runs on AWS dev.

## Onboarding it through the DevMate wizard

**Upload this folder — `aura-lambda-demo` — and no other.**

That matters. The wizard keeps the folder you select, so choosing this one puts
`requirements.txt` one level below the workspace root, which is exactly where Aura's app
detection looks. Selecting a parent folder buries it a level deeper, and the project then
analyses perfectly — clouds, cases, policy checks all correct — and fails only later with
"no runnable application found", about code that is plainly there.

After onboarding, check the plan reports `clouds: ['aws']` before going further. No cloud
dependency means no emulator, which means nothing to demonstrate. It comes from `boto3`
in `requirements.txt`: Aura derives the emulator from the code rather than from config.

## Then, in DevMate

1. **Start** — brings up an empty Floci emulator (~15s).
2. **Populate** — boots this app once so `cloud.ensure()` creates the table, the bucket
   and both functions, then stops the app. The emulator and its contents stay.
3. **Resources** — DynamoDB, S3 and both Lambdas.

Start does not create anything. Nothing in Aura does: the only thing that ever creates
resources inside an emulator is an application's own startup code, which is what Populate
runs.

## Required for the Lambdas

```bash
# aura-api/src/.env, on the machine running the agent — then restart the agent
QATEST_CONTAINER_BACKED_SERVICES=true
```

Lambda is one of Floci's container-backed services and needs a container runtime socket,
which Aura mounts only when this is set. It is read in the **runner's** process, so
setting it on the deployed Fargate task definition does nothing at all.

Without it the demo still runs green — `/pricing` and `/audit` report unavailable with the
reason, and every other case passes. That is a healthy run with nothing to show for those
two cases, not a failure.

## Routes

Every route is a bare, un-parameterised `GET`, because those are the only ones Aura plans
as runnable cases. Each reads back what startup wrote — a route returning a literal would
pass identically with no emulator running, which is the thing this demo exists to rule out.

| Route | Proves |
|---|---|
| `/` | the app is up; lists both function names |
| `/health` | which services provisioned at startup |
| `/catalog` | DynamoDB holds the seeded rows |
| `/pricing` | a Lambda **read** DynamoDB and returned a computed total |
| `/audit` | a Lambda **wrote** an object to S3 |
| `/media` | the object `/audit` created is really there |

`/pricing` and `/audit` deliberately touch different services, so the Resources panel
visibly changes after an invocation rather than reporting the same rows as before.

## The one detail that breaks Lambdas

Both handlers reach Floci at **`http://floci:4566`**, never `localhost`. A function runs
inside Floci's own container network, where `localhost` is the function's own container
and reaches nothing. Getting this wrong fails as a 30-second timeout that looks like the
emulator being down — check it first, always.

## template.yaml

Not deployed. The app creates its own resources; this file exists to be read by Aura's
NIST policy checks, which parse IaC and never touch a running system.

It is authored to produce **2 of 4 passing**, because a demo where everything passes
proves nothing and one where everything fails looks broken. Each violation is paired with
a compliant sibling so the check is visibly discriminating:

| Control | Result | Where |
|---|---|---|
| IA-5(1) no literal secrets | **fails** | `AuditFunction.DB_PASSWORD` is a literal; `PricingFunction` uses a `{{resolve:secretsmanager:…}}` reference |
| AC-6(1) least privilege | **fails** | `PricingFunction` uses `Action: '*'` and `Resource: '*'`; `AuditFunction` names exactly what it needs |
| SI-2 supported runtime | passes | both on `python3.12` |
| SC-13 encryption at rest | passes | table and bucket both declare it |

## Verified

Every claim above was run before this file was written: both functions deployed and
invoked, `/pricing` returned `total: 54.5` computed inside a container, `/audit` wrote an
object that `/media` then listed, and `podman ps` showed both functions running on
`public.ecr.aws/lambda/python:3.12`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Lambda.InitError` / `Broken pipe` | Floci's podman socket connection went stale while idle | Stop and Start the emulator, then Populate again |
| `port 4566 is already held by …` | another project's emulator has it; Floci's ports are fixed | stop that one first |
| Populate works but no Lambdas | `QATEST_CONTAINER_BACKED_SERVICES` unset, or the agent was not restarted | set it in `src/.env`, restart the agent |
| `Task timed out after 30 seconds` | the function cannot reach Floci | check its endpoint is `http://floci:4566`, not `localhost` |
| Resources empty after a restart | Floci keeps state in memory by default | press Populate again |
| Floci's own console on :4500 shows no Serverless / no resources | its ACCOUNT menu defaults to `0000-0000-0000`; Aura populates into this project's 12-digit account | pick the project's account in the console's ACCOUNT menu (top right), or restart it pre-seeded: `aura-api/floci-ui-brand/start.sh --project <projectId>` |

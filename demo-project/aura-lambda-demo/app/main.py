"""aura-lambda-demo — two AWS Lambdas executing locally in Floci.

Built for one purpose: to make a QualityMind run, or DevMate's Populate button, show two
real Lambda functions running in containers on the operator's own machine.

Two constraints from Aura's planner shape every route here, and both are load-bearing:

1. Only plain, un-parameterised GET routes produce runnable cases. `plan.why_unrunnable`
   skips any non-GET ("needs a request body the graph does not describe") and any path
   carrying a parameter. So every route below is a bare GET — a POST /price would be
   planned and then skipped, proving nothing.
2. Each route READS BACK what startup wrote. A route returning a literal would pass
   identically with no emulator running at all, which is exactly what the demo exists to
   rule out.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import cloud

log = logging.getLogger("aura-lambda-demo")
logging.basicConfig(level=logging.INFO)

#: Filled at startup by `cloud.ensure`. A route whose service failed to provision says so
#: rather than raising, so one broken service costs one case instead of the whole run.
PROBLEMS: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Nothing in Aura provisions resources inside an emulator, so the app lays its own
    # table. This is the code DevMate's Populate button exists to run.
    PROBLEMS.update(cloud.ensure())
    if PROBLEMS:
        log.warning("started with %d unprovisioned service(s): %s",
                    len(PROBLEMS), ", ".join(PROBLEMS))
    else:
        log.info("provisioned dynamodb/s3 and deployed 2 lambdas against %s",
                 cloud.endpoint() or "real AWS")
    yield


app = FastAPI(title="Aura Lambda Demo", lifespan=lifespan)


def _unavailable(service: str) -> dict:
    return {"service": service, "available": False, "reason": PROBLEMS.get(service, "")}


@app.get("/")
def root() -> dict:
    """Aura always plans a case for `GET /`, so this route must exist."""
    return {"app": "aura-lambda-demo", "endpoint": cloud.endpoint() or "real AWS",
            "functions": sorted(cloud.FUNCTIONS)}


@app.get("/health")
def health() -> dict:
    """Which services answered at startup. Green means the emulator was reachable."""
    return {"ok": not PROBLEMS, "endpoint": cloud.endpoint() or "real AWS",
            "problems": PROBLEMS}


@app.get("/catalog")
def catalog() -> dict:
    """Scan DynamoDB. Proves the table exists and holds what startup seeded — and it is
    the same data the pricing function reads, so /pricing can be checked against it."""
    if "dynamodb" in PROBLEMS:
        return _unavailable("dynamodb")
    rows = cloud.client("dynamodb").scan(TableName=cloud.TABLE).get("Items", [])
    return {"service": "dynamodb", "available": True, "table": cloud.TABLE,
            "count": len(rows),
            "items": sorted(({k: v.get("S", "") for k, v in r.items()} for r in rows),
                            key=lambda r: r.get("id", ""))}


@app.get("/media")
def media() -> dict:
    """List S3. Starts empty and grows each time /audit runs, which is how the demo
    shows a Lambda's side effect rather than just its return value."""
    if "s3" in PROBLEMS:
        return _unavailable("s3")
    objects = cloud.client("s3").list_objects_v2(Bucket=cloud.BUCKET).get("Contents", [])
    return {"service": "s3", "available": True, "bucket": cloud.BUCKET,
            "count": len(objects), "keys": sorted(o["Key"] for o in objects)}


@app.get("/pricing")
def pricing() -> dict:
    """Invoke the pricing Lambda, which READS DynamoDB.

    Reports unavailable rather than failing when the emulator cannot run containers — a
    run where only the Lambda cases are `unemulated` is a healthy run, and `unemulated`
    already means "we could not check" rather than "this is broken".
    """
    return _invoke("aura-lambda-pricing")


@app.get("/audit")
def audit() -> dict:
    """Invoke the audit Lambda, which WRITES to S3. Call /media afterwards to see it."""
    return _invoke("aura-lambda-audit")


#: Floci's signature for "I cannot start a container". Matched on substrings rather than
#: an error code because Floci reports it as a generic Lambda.InitError carrying a Java
#: exception, and the code alone cannot be told apart from a genuine failure in the
#: function's own module-level code.
_NO_RUNTIME = ("failed to start lambda container", "socketexception", "broken pipe",
               "no such file or directory", "cannot connect to the docker daemon")

_REMEDY = ("this Floci cannot start Lambda containers. Set "
           "QATEST_CONTAINER_BACKED_SERVICES=true in aura-api/src/.env on the runner and "
           "restart the agent. If it was already set, the emulator's socket connection "
           "has gone stale — stop and start the emulator, then Populate again.")


def _is_missing_runtime(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _NO_RUNTIME)


def _invoke(name: str) -> dict:
    """Invoke one function and report honestly when it is not there.

    Shared by both routes so they cannot drift into explaining the same absence two
    different ways.
    """
    if "lambda" in PROBLEMS:
        return {"service": "lambda", "function": name, "available": False,
                "reason": f"Lambda was not deployed: {PROBLEMS['lambda'][:160]}. {_REMEDY}"}
    try:
        response = cloud.client("lambda").invoke(FunctionName=name)
        payload = json.loads(response["Payload"].read() or b"{}")
    except Exception as exc:                                  # noqa: BLE001
        return {"service": "lambda", "function": name, "available": False,
                "reason": str(exc)[:200]}
    if response.get("FunctionError"):
        detail = str(payload)
        if _is_missing_runtime(detail):
            # Floci accepted create_function but cannot START the container. That is the
            # EMULATOR lacking a capability, not the application misbehaving, so it must
            # read as "could not check". Deployment succeeds without the socket and only
            # invocation fails, so checking at startup never sees this.
            return {"service": "lambda", "function": name, "available": False,
                    "reason": _REMEDY}
        # The function ran and raised. That IS a failure, distinct from not being
        # runnable, and must not be dressed up as unavailable.
        return {"service": "lambda", "function": name, "available": True,
                "ok": False, "error": detail[:300]}
    return {"service": "lambda", "function": name, "available": True, "ok": True,
            "result": payload}

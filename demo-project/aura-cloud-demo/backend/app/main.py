"""aura-cloud-demo — an application that provably touches emulated cloud services.

Built for one purpose: to make a QualityMind run demonstrate that it executes on a real
machine against real cloud APIs running locally in Floci, and to give the run's resource
panel something real to show.

Two constraints from Aura's planner shape every route here, and both are load-bearing:

1. Only plain, un-parameterised GET routes produce runnable cases. `plan.why_unrunnable`
   skips any non-GET ("needs a request body the graph does not describe") and any path
   carrying a parameter ("path parameter has no known value"). So every route below is a
   bare GET — a POST /orders would be planned and then skipped, proving nothing.
2. Each route READS BACK what startup wrote. A route returning a literal would pass
   identically with no emulator running at all, which is exactly the thing the demo is
   supposed to rule out.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import cloud

log = logging.getLogger("aura-cloud-demo")
logging.basicConfig(level=logging.INFO)

#: Filled at startup by `cloud.ensure`. A route whose service failed to provision says so
#: rather than raising, so one broken service costs one case instead of the whole run.
PROBLEMS: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Nothing in Aura provisions resources inside an emulator, so the app lays its own
    # table. Idempotent, so repeated runs against a surviving emulator are stable.
    PROBLEMS.update(cloud.ensure())
    if PROBLEMS:
        log.warning("started with %d unprovisioned service(s): %s",
                    len(PROBLEMS), ", ".join(PROBLEMS))
    else:
        log.info("provisioned s3/dynamodb/sqs/sns against %s",
                 cloud.endpoint() or "real AWS")
    yield


app = FastAPI(title="Aura Cloud Demo", lifespan=lifespan)


def _unavailable(service: str) -> dict:
    """The shape a route returns when its service could not be provisioned."""
    return {"service": service, "available": False, "reason": PROBLEMS.get(service, "")}


@app.get("/")
def root() -> dict:
    """Aura always plans a case for `GET /`, so this is the one route that must exist."""
    return {"app": "aura-cloud-demo", "endpoint": cloud.endpoint() or "real AWS",
            "services": ["s3", "dynamodb", "sqs", "sns"]}


@app.get("/health")
def health() -> dict:
    """Which services answered at startup. Green means the emulator was reachable."""
    return {"ok": not PROBLEMS, "endpoint": cloud.endpoint() or "real AWS",
            "problems": PROBLEMS}


@app.get("/catalog")
def catalog() -> dict:
    """Scan DynamoDB. Proves the table exists and holds what startup seeded."""
    if "dynamodb" in PROBLEMS:
        return _unavailable("dynamodb")
    rows = cloud.client("dynamodb").scan(TableName=cloud.TABLE).get("Items", [])
    return {"service": "dynamodb", "available": True, "table": cloud.TABLE,
            "count": len(rows),
            "items": sorted(({k: v.get("S", "") for k, v in r.items()} for r in rows),
                            key=lambda r: r.get("id", ""))}


@app.get("/media")
def media() -> dict:
    """List S3. Proves the bucket exists and holds the seeded objects."""
    if "s3" in PROBLEMS:
        return _unavailable("s3")
    objects = cloud.client("s3").list_objects_v2(Bucket=cloud.BUCKET).get("Contents", [])
    return {"service": "s3", "available": True, "bucket": cloud.BUCKET,
            "count": len(objects),
            "keys": sorted(o["Key"] for o in objects)}


@app.get("/events")
def events() -> dict:
    """Read the SQS queue's depth. Proves the queue exists and carries the seeded
    message."""
    if "sqs" in PROBLEMS:
        return _unavailable("sqs")
    sqs = cloud.client("sqs")
    url = sqs.get_queue_url(QueueName=cloud.QUEUE)["QueueUrl"]
    attrs = sqs.get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
    return {"service": "sqs", "available": True, "queue": cloud.QUEUE,
            "messages": int(attrs.get("ApproximateNumberOfMessages", 0))}


@app.get("/notify")
def notify() -> dict:
    """List SNS topics. Proves the topic exists."""
    if "sns" in PROBLEMS:
        return _unavailable("sns")
    topics = cloud.client("sns").list_topics().get("Topics", [])
    return {"service": "sns", "available": True, "topic": cloud.TOPIC,
            "count": len(topics),
            "arns": [t["TopicArn"] for t in topics]}


@app.get("/pricing")
def pricing() -> dict:
    """Invoke the pricing Lambda.

    Lambda is one of Floci's container-backed services and needs a container runtime
    socket, which Aura mounts only when the operator enables it on the runner. So this
    route reports unavailable far more often than the others, and does so deliberately
    rather than failing — a run where only the Lambda cases are `unemulated` is a healthy
    run, and `unemulated` already means "we could not check" rather than "this is broken".
    """
    return _invoke("aura-demo-pricing")


@app.get("/audit")
def audit() -> dict:
    """Invoke the audit Lambda, which WRITES to S3.

    Deliberately a different service from /pricing: together they show a Lambda reading
    and a Lambda writing, so the resource panel visibly changes after a run rather than
    reporting the same rows it did before.
    """
    return _invoke("aura-demo-audit")


#: Floci's signature for "I cannot start a container". Matched on substrings rather than
#: an error code because Floci reports it as a generic Lambda.InitError carrying a Java
#: exception, and the code alone cannot be told apart from a genuine init failure in the
#: function's own module-level code.
_NO_RUNTIME = ("failed to start lambda container", "socketexception",
               "no such file or directory", "cannot connect to the docker daemon")


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
                "reason": f"Lambda is not available on this emulator: "
                          f"{PROBLEMS['lambda'][:160]}. It needs a container runtime "
                          f"socket — set QATEST_CONTAINER_BACKED_SERVICES=true on the "
                          f"runner and restart the agent."}
    try:
        response = cloud.client("lambda").invoke(FunctionName=name)
        payload = json.loads(response["Payload"].read() or b"{}")
    except Exception as exc:                                  # noqa: BLE001
        return {"service": "lambda", "function": name, "available": False,
                "reason": str(exc)[:200]}
    if response.get("FunctionError"):
        detail = str(payload)
        if _is_missing_runtime(detail):
            # Floci accepted create_function but cannot START the container: the runtime
            # socket is not mounted. That is the EMULATOR lacking a capability, not the
            # application misbehaving, so it must read as "could not check" — the run
            # records unemulated and stays green. Discovered by running it: deployment
            # succeeds without the socket and only invocation fails, so checking at
            # startup (as this first did) never sees the problem.
            return {"service": "lambda", "function": name, "available": False,
                    "reason": "this Floci cannot start Lambda containers — it has no "
                              "container runtime socket. Set "
                              "QATEST_CONTAINER_BACKED_SERVICES=true on the runner and "
                              "restart the agent."}
        # The function ran and raised. That IS a failure, distinct from not being
        # runnable, and it must not be dressed up as unavailable.
        return {"service": "lambda", "function": name, "available": True,
                "ok": False, "error": detail[:300]}
    return {"service": "lambda", "function": name, "available": True, "ok": True,
            "result": payload}


@app.get("/assets")
def assets() -> dict:
    """Google Cloud Storage. Present only in the multi-cloud variant; see
    requirements.txt. Degrades on its own so a missing emulator costs one case."""
    host = os.environ.get("STORAGE_EMULATOR_HOST", "")
    if not host:
        return {"service": "gcs", "available": False,
                "reason": "no GCP emulator — google-cloud-storage is not declared"}
    try:
        from google.cloud import storage
        buckets = [b.name for b in storage.Client(project="aura-local").list_buckets()]
    except Exception as exc:                                  # noqa: BLE001
        return {"service": "gcs", "available": False, "reason": str(exc)[:200]}
    return {"service": "gcs", "available": True, "host": host, "buckets": buckets}


@app.get("/blobs")
def blobs() -> dict:
    """Azure Blob Storage. Same shape and same reasoning as /assets."""
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn:
        return {"service": "azure-blob", "available": False,
                "reason": "no Azure emulator — azure-storage-blob is not declared"}
    try:
        from azure.storage.blob import BlobServiceClient
        svc = BlobServiceClient.from_connection_string(conn)
        containers = [c["name"] for c in svc.list_containers()]
    except Exception as exc:                                  # noqa: BLE001
        return {"service": "azure-blob", "available": False, "reason": str(exc)[:200]}
    return {"service": "azure-blob", "available": True, "containers": containers}

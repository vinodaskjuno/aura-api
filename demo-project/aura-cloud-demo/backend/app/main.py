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
    """Invoke a Lambda.

    The flagged extension. Lambda is one of Floci's container-backed services and needs a
    container runtime socket, which Aura mounts only when explicitly enabled — so this
    route reports unavailable far more often than the others, and does so deliberately
    rather than failing. A run where only this case is `unemulated` is a healthy run.
    """
    try:
        functions = cloud.client("lambda").list_functions().get("Functions", [])
    except Exception as exc:                                  # noqa: BLE001
        return {"service": "lambda", "available": False, "reason": str(exc)[:200]}
    names = [f["FunctionName"] for f in functions]
    if cloud.FUNCTION not in names:
        return {"service": "lambda", "available": False,
                "reason": f"{cloud.FUNCTION} is not deployed — Lambda needs a container "
                          f"runtime socket, which is off by default",
                "functions": names}
    payload = cloud.client("lambda").invoke(FunctionName=cloud.FUNCTION)
    return {"service": "lambda", "available": True, "function": cloud.FUNCTION,
            "status": payload.get("StatusCode")}


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

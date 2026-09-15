"""Cloud clients and startup provisioning for the two-Lambda demo.

Ordinary boto3 with an explicit `endpoint_url` taken from the environment Aura injects
(`AWS_ENDPOINT_URL`, see qatest/emulators.py). That is the whole trick: the application
code is unchanged and only the endpoint moves.

`ensure()` creates what the routes read back, including deploying both functions.
Nothing in Aura creates resources inside an emulator — an app that assumes a bucket
exists gets ResourceNotFoundException on a fresh container, so the app lays its own
table. This is what DevMate's Populate button runs.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("aura-lambda-demo")

TABLE = "aura-lambda-catalog"
BUCKET = "aura-lambda-media"

#: The two functions, and the handler module each is built from.
FUNCTIONS = {
    "aura-lambda-pricing": "pricing",
    "aura-lambda-audit": "audit",
}

#: Seeded at startup and read back by the routes and by the pricing function, so a
#: passing test is evidence the emulator was really reached rather than evidence a stub
#: returned 200.
CATALOG = [
    {"id": "sku-1", "name": "Aura T-shirt", "price": "24.00"},
    {"id": "sku-2", "name": "Floci mug", "price": "12.50"},
    {"id": "sku-3", "name": "QualityMind cap", "price": "18.00"},
]


def endpoint() -> str:
    """Where the AWS emulator is, or "" when running against real AWS."""
    return os.environ.get("AWS_ENDPOINT_URL", "")


def client(service: str):
    """A boto3 client for the emulator.

    Credentials come from the environment rather than being hardcoded, so the same code
    runs against real AWS with real credentials and no endpoint override.
    """
    import boto3

    kwargs = {"region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1")}
    if endpoint():
        kwargs["endpoint_url"] = endpoint()
    return boto3.client(service, **kwargs)


def ensure() -> dict:
    """Create everything the routes read. Idempotent, so repeated Populates are stable.

    Each service is provisioned independently and failures are collected rather than
    raised: one unavailable service costs its own route, not the whole app.
    """
    problems: dict[str, str] = {}
    for name, fn in (("dynamodb", _ensure_dynamodb), ("s3", _ensure_s3),
                     ("lambda", _ensure_lambda)):
        try:
            fn()
        except Exception as exc:                              # noqa: BLE001
            problems[name] = str(exc)[:200]
            log.warning("could not provision %s: %s", name, exc)
    return problems


def _ensure_dynamodb() -> None:
    ddb = client("dynamodb")
    if TABLE not in ddb.list_tables().get("TableNames", []):
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST")
        ddb.get_waiter("table_exists").wait(TableName=TABLE)
    for row in CATALOG:
        ddb.put_item(TableName=TABLE, Item={k: {"S": v} for k, v in row.items()})


def _ensure_s3() -> None:
    s3 = client("s3")
    if BUCKET not in [b["Name"] for b in s3.list_buckets().get("Buckets", [])]:
        s3.create_bucket(Bucket=BUCKET)


def _ensure_lambda() -> None:
    """Deploy both functions, if this Floci can run them.

    Lambda is one of Floci's container-backed services: it needs a container runtime
    socket, which Aura mounts only when QATEST_CONTAINER_BACKED_SERVICES is set on the
    RUNNER. Without it `create_function` fails, `ensure()` records it under "lambda",
    and the two routes explain themselves — a healthy run with two cases reporting
    unavailable rather than a broken one.
    """
    fn = client("lambda")
    existing = {f["FunctionName"] for f in fn.list_functions().get("Functions", [])}
    for name, module in FUNCTIONS.items():
        if name in existing:
            continue
        fn.create_function(
            FunctionName=name,
            Runtime="python3.12",
            # Floci does not check the role exists; a well-formed ARN is enough. A
            # real-looking one keeps the IAM checks in template.yaml meaningful.
            Role="arn:aws:iam::000000000000:role/aura-lambda-demo",
            Handler=f"{module}.handler",
            Code={"ZipFile": _zip_handler(module)},
            Timeout=30,
            Environment={"Variables": {
                # NOT localhost. The function runs on Floci's own container network,
                # where localhost is the function's own container and reaches nothing.
                "FLOCI_ENDPOINT": "http://floci:4566",
                # The account this project's resources live in. The emulator is
                # shared by every project on the machine and Floci separates them by
                # AWS account, so a function that used the default credentials would
                # read an EMPTY namespace while the app wrote to the project's — a
                # failure that looks exactly like a broken emulator. Floci's docs cover
                # account resolution for incoming requests but say nothing about
                # propagating it into an invocation, so the app has to pass it on.
                "FLOCI_ACCOUNT_ID": os.environ.get("AWS_ACCESS_KEY_ID", "000000000000"),
                "CATALOG_TABLE": TABLE,
                "MEDIA_BUCKET": BUCKET,
            }},
        )
        fn.get_waiter("function_active_v2").wait(FunctionName=name)


def _zip_handler(module: str) -> bytes:
    """A deployment package holding one handler, built in memory.

    boto3 is not bundled: Floci's python3.12 runtime image already provides it, as the
    real Lambda runtime does. Vendoring a copy would make the zip tens of MB for no gain.
    """
    import io
    import zipfile

    source = (Path(__file__).parent / "lambdas" / f"{module}.py").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{module}.py", source)
    return buffer.getvalue()

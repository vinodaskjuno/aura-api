"""Cloud clients for the demo, pointed at whatever emulator is present.

Every client is built with an explicit `endpoint_url` taken from the environment Aura
injects (`AWS_ENDPOINT_URL` and friends, see qatest/emulators.py). That is the whole
trick the demo exists to show: the application code is ordinary boto3, unchanged, and
only the endpoint moves.

`ensure()` provisions what the routes read back. Nothing in Aura creates resources inside
an emulator, so an app that assumes a bucket exists gets ResourceNotFoundException on a
fresh container — the app has to lay its own table.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aura-cloud-demo")

TABLE = "aura-demo-catalog"
BUCKET = "aura-demo-media"
QUEUE = "aura-demo-events"
TOPIC = "aura-demo-notify"
FUNCTION = "aura-demo-pricing"

#: Seeded at startup and read back by the routes, so a passing test is evidence the
#: emulator was really reached rather than evidence a stub returned 200.
CATALOG = [
    {"id": "sku-1", "name": "Aura T-shirt", "price": "24.00"},
    {"id": "sku-2", "name": "Floci mug", "price": "12.50"},
    {"id": "sku-3", "name": "QualityMind cap", "price": "18.00"},
]
MEDIA = {"sku-1.txt": b"t-shirt", "sku-2.txt": b"mug", "sku-3.txt": b"cap"}


def endpoint() -> str:
    """Where the AWS emulator is, or "" when running against real AWS."""
    return os.environ.get("AWS_ENDPOINT_URL", "")


def client(service: str):
    """A boto3 client for the emulator.

    Credentials default to the throwaway pair Aura injects. They are read from the
    environment rather than hardcoded so the same code runs against real AWS with real
    credentials and no endpoint override.
    """
    import boto3

    kwargs = {"region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1")}
    if endpoint():
        kwargs["endpoint_url"] = endpoint()
    return boto3.client(service, **kwargs)


def ensure() -> dict:
    """Create and seed everything the routes read. Idempotent.

    Each service is provisioned independently and failures are collected rather than
    raised: one unavailable service should cost its own route, not the whole app. The
    routes report what is missing themselves.
    """
    problems: dict[str, str] = {}

    for name, fn in (("s3", _ensure_s3), ("dynamodb", _ensure_dynamodb),
                     ("sqs", _ensure_sqs), ("sns", _ensure_sns)):
        try:
            fn()
        except Exception as exc:                              # noqa: BLE001
            problems[name] = str(exc)[:200]
            log.warning("could not provision %s: %s", name, exc)

    return problems


def _ensure_s3() -> None:
    s3 = client("s3")
    buckets = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if BUCKET not in buckets:
        s3.create_bucket(Bucket=BUCKET)
    for key, body in MEDIA.items():
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)


def _ensure_dynamodb() -> None:
    ddb = client("dynamodb")
    if TABLE not in ddb.list_tables().get("TableNames", []):
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        ddb.get_waiter("table_exists").wait(TableName=TABLE)
    for row in CATALOG:
        ddb.put_item(TableName=TABLE, Item={k: {"S": v} for k, v in row.items()})


def _ensure_sqs() -> None:
    sqs = client("sqs")
    url = sqs.create_queue(QueueName=QUEUE)["QueueUrl"]
    sqs.send_message(QueueUrl=url, MessageBody="demo order placed")


def _ensure_sns() -> None:
    client("sns").create_topic(Name=TOPIC)

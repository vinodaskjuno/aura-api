"""aura-lambda-pricing — totals the catalogue.

Runs INSIDE a container Floci starts, not in the application process. That is the whole
point: a green /pricing proves Floci really executed a Lambda on this machine, which no
amount of in-process code could show.

THE ENDPOINT IS NOT localhost. This function runs on Floci's own container network,
where `localhost` is the function's own container and reaches nothing. Floci is at
`http://floci:4566` — the name `FLOCI_HOSTNAME=floci` establishes. Getting this wrong is
the single most likely way for a Lambda to fail here, and it fails as a 30-second
timeout that looks like the emulator being down.
"""
import os

import boto3

ENDPOINT = os.environ.get("FLOCI_ENDPOINT", "http://floci:4566")
TABLE = os.environ.get("CATALOG_TABLE", "aura-lambda-catalog")


def handler(event, context):
    ddb = boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name="us-east-1",
                       aws_access_key_id="test", aws_secret_access_key="test")
    rows = ddb.scan(TableName=TABLE).get("Items", [])
    total = 0.0
    skipped = 0
    for row in rows:
        try:
            total += float(row.get("price", {}).get("S", "0"))
        except (TypeError, ValueError):
            # One unparseable row must not fail the whole total — count them instead, so
            # the number on screen is explainable.
            skipped += 1
    return {"items": len(rows), "total": round(total, 2), "skipped": skipped,
            "table": TABLE, "computedBy": "aura-lambda-pricing", "endpoint": ENDPOINT}

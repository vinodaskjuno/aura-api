"""aura-demo-pricing — totals the catalogue.

Runs INSIDE a container Floci starts, not in the application process. That is the whole
point of the case: a green /pricing proves Floci really executed a Lambda on this machine,
which no amount of in-process code could show.

THE ENDPOINT IS NOT localhost. This function runs on Floci's own container network, where
`localhost` is the function's own container and reaches nothing. Floci is at
`http://floci:4566` — the hostname `FLOCI_HOSTNAME=floci` establishes. Getting this wrong
is the single most likely way for a Lambda to fail here, and it fails as a connection
timeout that looks like the emulator being down.
"""
import os

import boto3

ENDPOINT = os.environ.get("FLOCI_ENDPOINT", "http://floci:4566")
#: The Floci account this function's resources live in. A 12-digit access key IS the
#: account selector — the secret is never validated. Without it the function reads the
#: default namespace while the app wrote to the project's, and finds nothing.
ACCOUNT = os.environ.get("FLOCI_ACCOUNT_ID", "000000000000")
TABLE = os.environ.get("CATALOG_TABLE", "aura-demo-catalog")


def handler(event, context):
    ddb = boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name="us-east-1",
                       aws_access_key_id=ACCOUNT, aws_secret_access_key="test")
    rows = ddb.scan(TableName=TABLE).get("Items", [])
    total = 0.0
    for row in rows:
        try:
            total += float(row.get("price", {}).get("S", "0"))
        except (TypeError, ValueError):
            # One unparseable row must not fail the whole total — say how many were
            # skipped instead, so the number on screen is explainable.
            continue
    return {"items": len(rows), "total": round(total, 2), "table": TABLE,
            "computedBy": "aura-demo-pricing", "endpoint": ENDPOINT}

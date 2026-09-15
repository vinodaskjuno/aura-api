"""aura-demo-audit — writes an audit record to S3 and returns its key.

The second function, deliberately touching a DIFFERENT service from the first: together
they show a Lambda reading (DynamoDB) and writing (S3), so the resource panel changes
visibly when they run rather than reporting the same rows as before.

Same endpoint rule as pricing.py: `http://floci:4566`, never localhost.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import boto3

ENDPOINT = os.environ.get("FLOCI_ENDPOINT", "http://floci:4566")
#: The Floci account this function's resources live in. A 12-digit access key IS the
#: account selector — the secret is never validated. Without it the function reads the
#: default namespace while the app wrote to the project's, and finds nothing.
ACCOUNT = os.environ.get("FLOCI_ACCOUNT_ID", "000000000000")
BUCKET = os.environ.get("MEDIA_BUCKET", "aura-demo-media")


def handler(event, context):
    s3 = boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1",
                      aws_access_key_id=ACCOUNT, aws_secret_access_key="test")
    key = f"audit/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    body = {"at": datetime.now(timezone.utc).isoformat(),
            "writtenBy": "aura-demo-audit", "endpoint": ENDPOINT}
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(body).encode(),
                  ContentType="application/json")
    return {"bucket": BUCKET, "key": key, "writtenBy": "aura-demo-audit"}

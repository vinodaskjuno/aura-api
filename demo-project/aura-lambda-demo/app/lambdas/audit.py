"""aura-lambda-audit — writes an audit record to S3 and returns its key.

The second function, deliberately touching a DIFFERENT service from the first: together
they show a Lambda reading (DynamoDB) and a Lambda writing (S3), so the Resources panel
visibly changes after an invocation rather than reporting the same rows as before.

Same endpoint rule as pricing.py: `http://floci:4566`, never localhost.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import boto3

ENDPOINT = os.environ.get("FLOCI_ENDPOINT", "http://floci:4566")
BUCKET = os.environ.get("MEDIA_BUCKET", "aura-lambda-media")


def handler(event, context):
    s3 = boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    stamp = datetime.now(timezone.utc)
    key = f"audit/{stamp.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    body = {"at": stamp.isoformat(), "writtenBy": "aura-lambda-audit",
            "endpoint": ENDPOINT}
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(body).encode(),
                  ContentType="application/json")
    return {"bucket": BUCKET, "key": key, "writtenBy": "aura-lambda-audit"}

"""Short-lived AWS credentials for a self-hosted QA runner.

The runner executes on a developer machine and has to write evidence into the same S3
bucket the Results tab reads, plus the run's index row. The alternative — presigned PUT
URLs — would mean threading a second transport through `evidence.py`, which writes one
screenshot per step from inside `runner._Recorder.add`. Handing the runner scoped
credentials instead means `evidence.py`, `s3_client.py` and the DynamoDB index all work
completely unchanged.

Stated plainly, because it is a trade and not a free lunch: this puts AWS credentials on
a developer machine. They are mitigated by being (a) narrowly scoped by a session policy
to one bucket PREFIX for one run, (b) time-boxed to an hour, and (c) minted per run
rather than held. This is what CI runners do.

`sts:GetCallerIdentity` has to be allowed as well: `s3_client._bucket()` calls it to
build `aura-<accountId>-test-artifacts`, and without it the account id silently becomes
the literal `"local"` and every write goes to a bucket that does not exist.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

DURATION_S = 3600


def _session_policy(bucket: str, table_arn: str, project_id: str, run_id: str) -> str:
    """Least privilege for exactly one run's evidence.

    An IAM session policy INTERSECTS with the role's own policy, so this can only ever
    narrow what the role already allows — it cannot grant anything new.
    """
    prefix = f"{project_id}/{run_id}/*"
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {   # Evidence for this run only: report.json, steps.jsonl, screenshots.
                "Sid": "RunEvidence",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/{prefix}"],
            },
            {   # write_report reads back what it wrote; list is needed by nothing here,
                # but GetBucketLocation is used by boto3 when the region is ambiguous.
                "Sid": "BucketMeta",
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {   # The best-effort index row service.execute writes at the end.
                "Sid": "RunIndexRow",
                "Effect": "Allow",
                "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem"],
                "Resource": [table_arn],
            },
            {   # s3_client._bucket() needs this to resolve the account id.
                "Sid": "WhoAmI",
                "Effect": "Allow",
                "Action": ["sts:GetCallerIdentity"],
                "Resource": ["*"],
            },
        ],
    })


def mint(project_id: str, run_id: str) -> dict | None:
    """Assume the runner role, scoped to one run. None if not configured.

    None is a normal answer, not a failure: an environment with no runner role (local
    dev, where the runner already has the developer's own credentials) simply gets no
    credentials block and carries on using whatever boto3 finds.
    """
    from src.config_settings import get_settings

    settings = get_settings()
    role_arn = (getattr(settings, "qa_runner_role_arn", "") or "").strip()
    if not role_arn:
        return None

    import boto3

    from src.storage import s3_client

    bucket = s3_client._bucket("test-artifacts")
    region = getattr(settings, "s3_region", "") or "us-east-1"
    # Account id from the same helper the bucket name uses, NOT parsed out of the bucket
    # string — a deployment with a custom s3_bucket_prefix has no account id in the name
    # at all, and splitting on "-" would have silently produced a nonsense ARN.
    account = s3_client._get_account_id()
    prefix = getattr(settings, "dynamodb_table_prefix", "aura-")
    table_arn = f"arn:aws:dynamodb:{region}:{account}:table/{prefix}test-results"

    try:
        sts = boto3.client("sts", region_name=region)
        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"qa-runner-{run_id}"[:64],
            Policy=_session_policy(bucket, table_arn, project_id, run_id),
            DurationSeconds=DURATION_S,
        )
    except Exception as exc:                                  # noqa: BLE001
        # The runner can still execute — it just cannot upload. Better to say so in the
        # claim response than to hand back a run that will fail at the first screenshot.
        log.warning("QA runner credentials for %s could not be minted: %s", run_id, exc)
        return None

    creds = resp.get("Credentials") or {}
    expires = creds.get("Expiration")
    return {
        "accessKeyId": creds.get("AccessKeyId", ""),
        "secretAccessKey": creds.get("SecretAccessKey", ""),
        "sessionToken": creds.get("SessionToken", ""),
        "region": region,
        "bucket": bucket,
        "expiresAt": expires.isoformat() if expires else "",
    }

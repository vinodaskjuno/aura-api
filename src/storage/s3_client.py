"""S3 helper — thin wrapper around boto3 client.

Buckets used:
  aura-uploads          raw uploaded files (JSON, CSV, Excel, PDF, Markdown)
  aura-analysis         code analysis results per project
  aura-test-artifacts   generated Playwright scripts, test reports
  aura-config           seed data, TTL schemas, agent prompts
  aura-exports          ontology exports, graph snapshots, RCA reports
"""
import io
import json
import logging
from typing import Any
import boto3
from botocore.exceptions import ClientError
from src.config_settings import get_settings

logger = logging.getLogger(__name__)

_client = None

BUCKETS = ["uploads", "analysis", "test-artifacts", "config", "exports"]


def _get_client():
    global _client
    if _client is None:
        s = get_settings()
        kwargs: dict = {"region_name": s.s3_region}
        if s.aws_access_key_id:
            kwargs["aws_access_key_id"] = s.aws_access_key_id
            kwargs["aws_secret_access_key"] = s.aws_secret_access_key
        _client = boto3.client("s3", **kwargs)
    return _client


_account_id_cache: str = ""

def _get_account_id() -> str:
    """Return AWS account ID for unique bucket naming. Cached after first call."""
    global _account_id_cache
    if _account_id_cache:
        return _account_id_cache
    try:
        import boto3
        s = get_settings()
        sts = boto3.client("sts", region_name=s.aws_region)
        _account_id_cache = sts.get_caller_identity()["Account"]
    except Exception:
        _account_id_cache = "local"
    return _account_id_cache


def _bucket(name: str) -> str:
    s = get_settings()
    prefix = s.s3_bucket_prefix
    # If prefix is the default "aura-", append account ID to avoid global S3 name conflicts
    if prefix == "aura-":
        acct = _get_account_id()
        return f"aura-{acct}-{name}"
    return f"{prefix}{name}"


# ── Core operations ──────────────────────────────────────────────────────────

def put_object(bucket_suffix: str, key: str, body: bytes | str,
               content_type: str = "application/octet-stream") -> str:
    """Upload bytes/str to S3. Returns the full s3:// URI."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    bucket = _bucket(bucket_suffix)
    try:
        _get_client().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
        return f"s3://{bucket}/{key}"
    except ClientError as e:
        logger.error("S3 put_object failed [%s/%s]: %s", bucket, key, e)
        raise


def put_json(bucket_suffix: str, key: str, data: Any) -> str:
    return put_object(bucket_suffix, key, json.dumps(data, default=str), "application/json")


def get_object(bucket_suffix: str, key: str) -> bytes | None:
    bucket = _bucket(bucket_suffix)
    try:
        resp = _get_client().get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        logger.error("S3 get_object failed [%s/%s]: %s", bucket, key, e)
        return None


def get_json(bucket_suffix: str, key: str) -> Any | None:
    raw = get_object(bucket_suffix, key)
    if raw is None:
        return None
    return json.loads(raw)


def delete_object(bucket_suffix: str, key: str) -> None:
    bucket = _bucket(bucket_suffix)
    try:
        _get_client().delete_object(Bucket=bucket, Key=key)
    except ClientError as e:
        logger.error("S3 delete_object failed [%s/%s]: %s", bucket, key, e)


def list_objects(bucket_suffix: str, prefix: str = "") -> list[dict]:
    bucket = _bucket(bucket_suffix)
    try:
        resp = _get_client().list_objects_v2(Bucket=bucket, Prefix=prefix)
        return [{"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"].isoformat()}
                for o in resp.get("Contents", [])]
    except ClientError as e:
        logger.error("S3 list_objects failed [%s]: %s", bucket, e)
        return []


def presigned_url(bucket_suffix: str, key: str, expires: int = 3600) -> str:
    bucket = _bucket(bucket_suffix)
    try:
        return _get_client().generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
        )
    except ClientError as e:
        logger.error("S3 presigned_url failed [%s/%s]: %s", bucket, key, e)
        return ""


def upload_fileobj(bucket_suffix: str, key: str, fileobj: io.IOBase,
                   content_type: str = "application/octet-stream") -> str:
    bucket = _bucket(bucket_suffix)
    try:
        _get_client().upload_fileobj(fileobj, bucket, key,
                                     ExtraArgs={"ContentType": content_type})
        return f"s3://{bucket}/{key}"
    except ClientError as e:
        logger.error("S3 upload_fileobj failed [%s/%s]: %s", bucket, key, e)
        raise


# ── Bucket bootstrap (creates buckets if they don't exist — for local dev) ───

def _bucket_exists(client, name: str) -> bool:
    """Return True if bucket exists (owned by us or someone else — either way skip creation)."""
    try:
        client.head_bucket(Bucket=name)
        return True  # 200 — we own it
    except ClientError as e:
        code = e.response["Error"]["Code"]
        http = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        # 403 = bucket exists but owned by another account → skip creation
        # 301 = bucket exists in a different region → skip creation
        if http in (403, 301) or code in ("403", "AllAccessDisabled", "PermanentRedirect"):
            return True
        return False  # 404 = truly doesn't exist


def _create_bucket_safe(client, name: str, region: str) -> None:
    """Create an S3 bucket, trying both constraint forms to handle regional endpoint quirks."""
    # Attempt 1: standard approach based on region
    try:
        if region == "us-east-1":
            client.create_bucket(Bucket=name)
        else:
            client.create_bucket(
                Bucket=name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        return
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
            return
        if code not in ("IllegalLocationConstraintException", "InvalidLocationConstraint"):
            raise

    # Attempt 2: flip constraint — covers cases where endpoint region differs from config
    try:
        if region == "us-east-1":
            # Was us-east-1 without constraint — endpoint is regional, try with constraint
            # Use the endpoint's actual region via head_bucket trick
            pass  # fall through to attempt 3
        else:
            # Was non-us-east-1 with constraint — try without (endpoint may be global)
            client.create_bucket(Bucket=name)
            return
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
            return

    # Attempt 3: ask AWS what region to use by checking the account's bucket region via STS
    try:
        sts = client.meta.client if hasattr(client.meta, "client") else None
        import boto3
        account_region = boto3.session.Session().region_name or region
        if account_region and account_region != "us-east-1":
            client.create_bucket(
                Bucket=name,
                CreateBucketConfiguration={"LocationConstraint": account_region},
            )
        else:
            client.create_bucket(Bucket=name)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
            raise


def ensure_buckets() -> None:
    s = get_settings()
    c = _get_client()
    region = c.meta.region_name or s.s3_region

    for suffix in BUCKETS:
        name = _bucket(suffix)
        if _bucket_exists(c, name):
            continue  # already exists — skip silently
        try:
            _create_bucket_safe(c, name, region)
            logger.info("Created S3 bucket: %s", name)
        except ClientError as e:
            # Non-fatal — app works without S3 in local dev
            logger.debug("S3 bucket %s not created: %s", name, e)

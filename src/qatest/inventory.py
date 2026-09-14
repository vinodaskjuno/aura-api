"""What a test run actually created and used inside the cloud emulators.

A green test tells you the application answered. It does not tell you the application
reached anything — a route returning a literal passes identically with no emulator
running at all. This reads the emulator back and says what is in it: the buckets, tables,
queues and topics the run touched, with counts.

Two callers, one collector:

  * `service.execute` takes a snapshot at the end of a run, while the containers are
    still up, and attaches it to the Report.
  * the runner's `inventory` command answers "what is in there right now" on demand, for
    an emulator the operator started and left running.

Both go through `collect()`, so the panel showing a finished run and the panel showing a
live emulator can never disagree about what a resource looks like.

WHY A DEDICATED SESSION, NEVER os.environ
-----------------------------------------
The runner process holds REAL, scoped AWS credentials — minted per run by the API and
used by `evidence.py` to upload screenshots and the report to the real S3 bucket. Setting
`AWS_ENDPOINT_URL` in `os.environ` to read the emulator would redirect those uploads into
the emulator, and the run's evidence would vanish into a container that is about to be
deleted. Every client here is built on its own Session with an explicit `endpoint_url`
and throwaway credentials, and touches nothing global.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Caps. A chatty test must not be able to inflate report.json without bound — it is
#: stored per run and read by the UI on every open.
MAX_BUCKETS = 25
MAX_OBJECTS = 50
MAX_TABLES = 25
MAX_QUEUES = 25
MAX_TOPICS = 25
MAX_FUNCTIONS = 25

#: Short on purpose. This runs at the end of a run, after the result is already decided;
#: waiting on a wedged emulator would delay a finished run for nothing.
TIMEOUT_S = 5


def _session(endpoint: str):
    """A boto3 client factory bound to one emulator. Shares nothing with the process."""
    import boto3
    from botocore.config import Config

    session = boto3.session.Session(
        aws_access_key_id="test", aws_secret_access_key="test",
        region_name="us-east-1")
    config = Config(connect_timeout=TIMEOUT_S, read_timeout=TIMEOUT_S,
                    retries={"max_attempts": 1})
    return lambda service: session.client(service, endpoint_url=endpoint, config=config)


def collect(endpoints: dict[str, str]) -> dict:
    """Inventory every emulator in `endpoints` ({cloud: url}).

    Never raises. An inventory is a description of a run, not part of it — a failure here
    must not turn a passing run into a failing one, so every probe is guarded
    individually and the worst case is an empty section.
    """
    out: dict = {}
    for cloud, url in (endpoints or {}).items():
        if not url:
            continue
        try:
            if cloud == "aws":
                found = _aws(_session(url))
            else:
                # Only AWS is inventoried today. The others start and are reported as
                # running; enumerating them needs their own SDKs and their own probes,
                # and claiming an empty list would read as "nothing there" rather than
                # "not looked at".
                found = {}
            if found:
                out[cloud] = found
        except Exception as exc:                              # noqa: BLE001
            log.debug("qatest: inventory of %s failed: %s", cloud, exc)
    return out


def _aws(client) -> dict:
    """Each service independently guarded: one unavailable service costs its own section."""
    services: dict = {}
    for name, probe in (("s3", _s3), ("dynamodb", _dynamodb), ("sqs", _sqs),
                        ("sns", _sns), ("lambda", _lambda)):
        try:
            found = probe(client)
        except Exception as exc:                              # noqa: BLE001
            log.debug("qatest: inventory %s: %s", name, exc)
            continue
        if found:
            services[name] = found
    return services


def _s3(client) -> list[dict]:
    s3 = client("s3")
    out = []
    for bucket in s3.list_buckets().get("Buckets", [])[:MAX_BUCKETS]:
        name = bucket.get("Name", "")
        keys: list[str] = []
        count = 0
        try:
            listing = s3.list_objects_v2(Bucket=name, MaxKeys=MAX_OBJECTS)
            keys = [o["Key"] for o in listing.get("Contents", [])]
            # KeyCount is what the page returned; IsTruncated says there is more, and
            # reporting the page size as the total would understate a large bucket.
            count = int(listing.get("KeyCount", len(keys)))
            if listing.get("IsTruncated"):
                count = -1        # rendered as "50+" rather than a wrong number
        except Exception as exc:                              # noqa: BLE001
            log.debug("qatest: inventory s3 %s: %s", name, exc)
        out.append({"name": name, "count": count, "items": keys[:MAX_OBJECTS]})
    return out


def _dynamodb(client) -> list[dict]:
    ddb = client("dynamodb")
    out = []
    for name in ddb.list_tables().get("TableNames", [])[:MAX_TABLES]:
        count = 0
        try:
            count = int(ddb.describe_table(TableName=name)["Table"].get("ItemCount", 0))
        except Exception as exc:                              # noqa: BLE001
            log.debug("qatest: inventory dynamodb %s: %s", name, exc)
        out.append({"name": name, "count": count})
    return out


def _sqs(client) -> list[dict]:
    sqs = client("sqs")
    out = []
    for url in (sqs.list_queues().get("QueueUrls", []) or [])[:MAX_QUEUES]:
        count = 0
        try:
            attrs = sqs.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=["ApproximateNumberOfMessages"]).get("Attributes", {})
            count = int(attrs.get("ApproximateNumberOfMessages", 0))
        except Exception as exc:                              # noqa: BLE001
            log.debug("qatest: inventory sqs %s: %s", url, exc)
        out.append({"name": url.rsplit("/", 1)[-1], "count": count})
    return out


def _sns(client) -> list[dict]:
    topics = client("sns").list_topics().get("Topics", [])[:MAX_TOPICS]
    return [{"name": t.get("TopicArn", "").rsplit(":", 1)[-1]} for t in topics]


def _lambda(client) -> list[dict]:
    functions = client("lambda").list_functions().get("Functions", [])[:MAX_FUNCTIONS]
    return [{"name": f.get("FunctionName", "")} for f in functions]


def summarise(resources: dict) -> list[str]:
    """One human line per resource, for the run's console.

    These ride the activity stream the runner already sends on every heartbeat, so the
    resources appear in the live terminal during a run without adding a wire field.
    """
    lines = []
    for cloud, services in (resources or {}).items():
        for service, items in (services or {}).items():
            for item in items:
                count = item.get("count")
                detail = ("" if count is None
                          else " — 50+ items" if count == -1
                          else f" — {count} item{'' if count == 1 else 's'}")
                lines.append(f"{cloud}/{service}: {item.get('name', '')}{detail}")
    return lines

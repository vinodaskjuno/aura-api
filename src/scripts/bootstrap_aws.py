"""Create every DynamoDB table and S3 bucket in a target AWS account.

Reuses the app's own ensure_tables()/ensure_buckets() so there is exactly one
schema definition (src/database/dynamo_client.TABLE_SCHEMAS) — a standalone copy
would drift the moment someone adds a table.

Usage:
    # 1. See where your credentials currently point (no writes):
    AWS_PROFILE=aura-new python -m src.scripts.bootstrap_aws --dry-run

    # 2. Create everything, refusing to run unless the account matches:
    AWS_PROFILE=aura-new python -m src.scripts.bootstrap_aws --expect-account 111122223333

Note: AWS_PROFILE is only honoured when AWS_ACCESS_KEY_ID is empty in src/.env —
dynamo_client passes explicit keys to boto3 whenever that setting is non-empty,
and explicit keys beat the profile.
"""
from __future__ import annotations

import argparse
import sys

import boto3


def main() -> int:
    ap = argparse.ArgumentParser(description="Bootstrap AURA DynamoDB tables + S3 buckets.")
    ap.add_argument("--expect-account", metavar="ID",
                    help="12-digit account ID that must match, or the run aborts.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report the target account and planned resources; create nothing.")
    ap.add_argument("--skip-s3", action="store_true", help="Create tables only.")
    args = ap.parse_args()

    from src.config_settings import get_settings
    from src.database.dynamo_client import TABLE_SCHEMAS
    from src.storage.s3_client import BUCKETS

    s = get_settings()

    # ── Where are we actually pointed? ───────────────────────────────────────
    try:
        ident = boto3.client("sts").get_caller_identity()
    except Exception as exc:
        print(f"ERROR: cannot resolve AWS identity: {exc}", file=sys.stderr)
        print("Configure a profile first: aws configure --profile <name>", file=sys.stderr)
        return 2

    acct = ident["Account"]
    print(f"caller        : {ident['Arn']}")
    print(f"account       : {acct}")
    print(f"ddb region    : {s.dynamodb_region}")
    print(f"table prefix  : {s.dynamodb_table_prefix}")
    print(f"ddb endpoint  : {s.dynamodb_endpoint_url or '(real AWS)'}")
    if s.aws_access_key_id:
        print("credentials   : explicit keys from src/.env "
              "(AWS_PROFILE is IGNORED — clear the keys to use a profile)")
    else:
        print("credentials   : default chain / AWS_PROFILE")

    if args.expect_account and acct != args.expect_account:
        print(f"\nABORT: expected account {args.expect_account}, got {acct}. "
              "Nothing created.", file=sys.stderr)
        return 1
    if not args.expect_account and not args.dry_run:
        print("\nABORT: pass --expect-account <id> to confirm the target, "
              "or --dry-run to inspect.", file=sys.stderr)
        return 1

    n_tab, n_buk = len(TABLE_SCHEMAS), 0 if args.skip_s3 else len(BUCKETS)
    print(f"\nplan          : {n_tab} DynamoDB tables, {n_buk} S3 buckets")

    if args.dry_run:
        print("\n-- tables --")
        for sc in TABLE_SCHEMAS:
            sk = f" / {sc['sk']}" if sc.get("sk") else ""
            gsi = f"  +{len(sc['gsis'])} GSI" if sc.get("gsis") else ""
            print(f"  {s.dynamodb_table_prefix}{sc['name']:<26} {sc['pk']}{sk}{gsi}")
        if not args.skip_s3:
            print("\n-- buckets --")
            for b in BUCKETS:
                print(f"  aura-{acct}-{b}")
        print("\ndry run — nothing created.")
        return 0

    # ── Create ──────────────────────────────────────────────────────────────
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from src.database.dynamo_client import ensure_tables
    print("\ncreating DynamoDB tables (idempotent)...")
    ensure_tables()

    if not args.skip_s3:
        from src.storage.s3_client import ensure_buckets
        print("\ncreating S3 buckets (idempotent)...")
        ensure_buckets()

    # ── Verify ──────────────────────────────────────────────────────────────
    ddb = boto3.client("dynamodb", region_name=s.dynamodb_region)
    live = [t for pg in ddb.get_paginator("list_tables").paginate()
            for t in pg["TableNames"] if t.startswith(s.dynamodb_table_prefix)]
    print(f"\nverified      : {len(live)}/{n_tab} tables present in {acct}")
    missing = {f"{s.dynamodb_table_prefix}{x['name']}" for x in TABLE_SCHEMAS} - set(live)
    if missing:
        print(f"MISSING       : {sorted(missing)}", file=sys.stderr)
        return 1
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

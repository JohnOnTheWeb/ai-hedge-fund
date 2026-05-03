#!/usr/bin/env python3
"""Seed Secrets Manager entries from the local shell environment.

Reads FINANCIAL_DATASETS_API_KEY and MD_STORE_TOKEN from the environment and
writes them to the two Secrets Manager entries created by platform_stack.py.
No Bedrock secret — the deployment uses IAM task-role auth for Bedrock.

Idempotent: existing secrets are updated via put_secret_value. If a secret does
not yet exist (first deploy), the script skips it with a warning so bootstrap
can proceed; run again after `cdk deploy` to populate.
"""
from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError

_SECRETS = {
    "aihedge/financial-datasets": ("FINANCIAL_DATASETS_API_KEY", True),
    "aihedge/md-store-token": ("MD_STORE_TOKEN", True),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "IGENV"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    sm = session.client("secretsmanager")

    any_missing_env = False
    for secret_id, (env_var, required) in _SECRETS.items():
        value = os.environ.get(env_var)
        if not value:
            print(f"[WARN] env var {env_var} is empty — skipping {secret_id}")
            any_missing_env = any_missing_env or required
            continue
        try:
            sm.put_secret_value(SecretId=secret_id, SecretString=value)
            print(f"[OK]   updated {secret_id}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                print(f"[INFO] {secret_id} not yet provisioned — run `cdk deploy` first, then re-run seed_secrets")
            else:
                print(f"[ERR]  {secret_id}: {code}: {exc}")
                return 1

    return 1 if any_missing_env else 0


if __name__ == "__main__":
    sys.exit(main())

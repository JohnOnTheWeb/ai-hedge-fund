"""Step Functions entry step — load the tickers watchlist from S3."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

import boto3


def handler(event, _context):
    bucket = os.environ["AIHEDGE_CONFIG_BUCKET"]
    key = event.get("tickers_key") or "watchlist.json"

    s3 = boto3.client("s3")
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    config = json.loads(body)

    tickers = config.get("tickers") or []
    if isinstance(event.get("tickers"), list) and event["tickers"]:
        tickers = event["tickers"]

    run_id = event.get("run_id") or f"run-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    trade_date = event.get("trade_date") or datetime.utcnow().strftime("%Y-%m-%d")

    return {
        "run_id": run_id,
        "trade_date": trade_date,
        "tickers": tickers,
        "config_bucket": bucket,
    }

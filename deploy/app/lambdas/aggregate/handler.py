"""Aggregate per-ticker results → summary JSON + SNS email."""
from __future__ import annotations

import json
import os

import boto3


def handler(event, _context):
    bucket = os.environ["AIHEDGE_CONFIG_BUCKET"]
    topic = os.environ["AIHEDGE_SUMMARY_TOPIC_ARN"]
    run_id = event["run_id"]
    trade_date = event["trade_date"]
    tickers = event.get("tickers") or []

    s3 = boto3.client("s3")
    results = {}
    for t in tickers:
        key = f"runs/{run_id}/{t}.json"
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            results[t] = json.loads(body)
        except s3.exceptions.NoSuchKey:
            results[t] = {"status": "missing"}

    summary_key = f"runs/{run_id}/_summary.json"
    s3.put_object(
        Bucket=bucket,
        Key=summary_key,
        Body=json.dumps({"run_id": run_id, "trade_date": trade_date, "results": results}, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    sns = boto3.client("sns")
    subject = f"[AIHedge] {trade_date} — {len(results)} tickers"
    body = _format_email(run_id, trade_date, results)
    sns.publish(TopicArn=topic, Subject=subject, Message=body)

    return {"run_id": run_id, "trade_date": trade_date, "summary_key": summary_key, "results": results}


def _format_email(run_id: str, trade_date: str, results: dict) -> str:
    lines = [f"AI-HedgeFund run {run_id} ({trade_date})", ""]
    for ticker, r in sorted(results.items()):
        decision = (r or {}).get("decisions", {}).get(ticker) or {}
        action = decision.get("action", "?")
        qty = decision.get("quantity", 0)
        conf = decision.get("confidence", "?")
        lines.append(f"  {ticker:6s}  {action:5s}  qty={qty}  conf={conf}")
    return "\n".join(lines) + "\n"

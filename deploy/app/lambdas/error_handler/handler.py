"""Catch-all state — posts a failure summary to SNS."""
from __future__ import annotations

import json
import os

import boto3


def handler(event, _context):
    topic = os.environ["AIHEDGE_SUMMARY_TOPIC_ARN"]
    run_id = event.get("run_id", "unknown")
    err = event.get("error") or event

    msg = (
        f"AI-HedgeFund run {run_id} FAILED.\n\n"
        f"Error:\n{json.dumps(err, default=str, indent=2)}\n"
    )

    sns = boto3.client("sns")
    sns.publish(TopicArn=topic, Subject=f"[AIHedge] FAILED {run_id}", Message=msg)
    return {"run_id": run_id, "status": "failed"}

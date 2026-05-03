"""APIGW POST /runs → Step Functions StartExecution."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

import boto3


def handler(event, _context):
    sfn_arn = os.environ["AIHEDGE_STATE_MACHINE_ARN"]
    body = _parse_body(event)

    run_id = body.get("run_id") or f"run-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    payload = {
        "trigger": "api",
        "run_id": run_id,
        "trade_date": body.get("trade_date") or datetime.utcnow().strftime("%Y-%m-%d"),
        "tickers": body.get("tickers") or [],
        "tickers_key": body.get("tickers_key") or "watchlist.json",
    }

    sfn = boto3.client("stepfunctions")
    resp = sfn.start_execution(stateMachineArn=sfn_arn, name=run_id, input=json.dumps(payload))

    return _response(
        202,
        {
            "run_id": run_id,
            "executionArn": resp["executionArn"],
            "startDate": resp["startDate"].isoformat(),
        },
    )


def _parse_body(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def _response(status: int, body: dict):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }

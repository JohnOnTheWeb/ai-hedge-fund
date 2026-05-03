"""APIGW GET /runs/{runId} → DescribeExecution."""
from __future__ import annotations

import json
import os

import boto3


def handler(event, _context):
    sfn_arn = os.environ["AIHEDGE_STATE_MACHINE_ARN"]
    run_id = event.get("pathParameters", {}).get("runId")
    if not run_id:
        return _response(400, {"error": "missing runId"})

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    # Execution ARN shape: arn:aws:states:<region>:<account>:execution:<name>:<id>
    account = sfn_arn.split(":")[4]
    state_machine_name = sfn_arn.split(":")[-1]
    execution_arn = f"arn:aws:states:{region}:{account}:execution:{state_machine_name}:{run_id}"

    sfn = boto3.client("stepfunctions")
    try:
        resp = sfn.describe_execution(executionArn=execution_arn)
    except sfn.exceptions.ExecutionDoesNotExist:
        return _response(404, {"error": "not_found", "run_id": run_id})

    return _response(
        200,
        {
            "run_id": run_id,
            "status": resp["status"],
            "startDate": resp["startDate"].isoformat(),
            "stopDate": resp["stopDate"].isoformat() if resp.get("stopDate") else None,
            "output": json.loads(resp["output"]) if resp.get("output") else None,
        },
    )


def _response(status: int, body: dict):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }

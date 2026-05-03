"""Fargate analyst-driver entrypoint.

Invoked by Step Functions via ECS.RunTask.sync per ticker. Streams NDJSON from
the AgentCore Runtime, discards heartbeats, writes the final result to S3 at
`s3://<config_bucket>/runs/<run_id>/<ticker>.json`.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

import boto3


def main() -> int:
    ticker = os.environ["AIHEDGE_TICKER"]
    trade_date = os.environ["AIHEDGE_TRADE_DATE"]
    run_id = os.environ["AIHEDGE_RUN_ID"]
    bucket = os.environ["AIHEDGE_CONFIG_BUCKET"]
    runtime_arn = os.environ["AIHEDGE_RUNTIME_ARN"]
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    agentcore = boto3.client("bedrock-agentcore", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    payload = {
        "run_id": run_id,
        "trade_date": trade_date,
        "tickers": [ticker],
        "show_reasoning": False,
    }

    started = time.time()
    session_id = str(uuid.uuid4())
    print(f"[{ticker}] invoking {runtime_arn} session={session_id}")

    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        accept="application/x-ndjson",
        contentType="application/json",
        payload=json.dumps(payload).encode("utf-8"),
    )

    final: dict[str, Any] | None = None
    for line in _iter_ndjson(resp):
        ev = json.loads(line)
        kind = ev.get("type")
        if kind == "heartbeat":
            print(f"[{ticker}]   heartbeat agent={ev.get('agent')} elapsed={ev.get('ts'):.1f}s")
        elif kind == "result":
            final = ev
            break
        elif kind == "error":
            print(f"[{ticker}] ERROR from runtime: {ev}", file=sys.stderr)
            raise RuntimeError(f"runtime error: {ev.get('message')}")
        else:
            print(f"[{ticker}] unknown event: {ev}")

    if final is None:
        raise RuntimeError("no result event from runtime")

    out_key = f"runs/{run_id}/{ticker}.json"
    s3.put_object(
        Bucket=bucket,
        Key=out_key,
        Body=json.dumps(final, default=str).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"[{ticker}] wrote s3://{bucket}/{out_key} ({time.time()-started:.1f}s)")
    return 0


def _iter_ndjson(resp: dict[str, Any]):
    """Yield lines from the AgentCore streaming response body."""
    stream = resp.get("response") or resp.get("completion") or resp.get("body")
    if stream is None:
        raise RuntimeError(f"no stream in invoke response: keys={list(resp.keys())}")
    # StreamingBody supports iter_lines in boto3.
    if hasattr(stream, "iter_lines"):
        for chunk in stream.iter_lines():
            if chunk:
                yield chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        return
    # Event stream variant: yields dicts with 'chunk'/'bytes'.
    buffer = b""
    for event in stream:
        data = event.get("chunk", {}).get("bytes") if isinstance(event, dict) else None
        if not data:
            continue
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                yield line.decode("utf-8")


if __name__ == "__main__":
    sys.exit(main())

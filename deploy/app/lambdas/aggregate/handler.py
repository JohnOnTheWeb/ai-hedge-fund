"""Aggregate per-ticker results → summary JSON + SNS email.

A Step Functions Map can SUCCEED while individual tickers failed (task_runner
catches errors and still returns cleanly). We inspect each per-ticker JSON,
build a failed-tickers list, and emit `status` = SUCCESS / PARTIAL_FAILURE /
FAILURE so downstream consumers (email, dashboards) can tell the difference
from the SFN top-level state.
"""
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
    results: dict[str, dict] = {}
    failed: list[str] = []

    for t in tickers:
        key = f"runs/{run_id}/{t}.json"
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            payload = json.loads(body)
        except s3.exceptions.NoSuchKey:
            results[t] = {"status": "missing"}
            failed.append(t)
            continue

        results[t] = payload
        if _is_failure(payload):
            failed.append(t)

    if not failed:
        status = "SUCCESS"
    elif len(failed) == len(tickers):
        status = "FAILURE"
    else:
        status = "PARTIAL_FAILURE"

    summary = {
        "run_id": run_id,
        "trade_date": trade_date,
        "status": status,
        "tickers_ok": [t for t in tickers if t not in failed],
        "tickers_failed": failed,
        "results": results,
    }

    summary_key = f"runs/{run_id}/_summary.json"
    s3.put_object(
        Bucket=bucket,
        Key=summary_key,
        Body=json.dumps(summary, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    sns = boto3.client("sns")
    subject = f"[AIHedge] {status} {trade_date} — ok={len(tickers)-len(failed)} failed={len(failed)}"
    sns.publish(TopicArn=topic, Subject=subject, Message=_format_email(run_id, trade_date, status, results, failed))

    return summary


def _is_failure(payload: dict) -> bool:
    """A payload is a failure if it has no decisions, an explicit error, or a non-OK status."""
    if not isinstance(payload, dict):
        return True
    if payload.get("type") == "error" or payload.get("error"):
        return True
    status = (payload.get("status") or "").lower()
    if status in ("missing", "failed", "error", "timeout"):
        return True
    # task_runner output shape from runtime.py: {"type":"result", ...}
    if payload.get("type") == "result" and payload.get("decisions"):
        return False
    # Fallback: missing decisions is a failure.
    if not payload.get("decisions"):
        return True
    return False


def _format_email(run_id: str, trade_date: str, status: str, results: dict, failed: list[str]) -> str:
    lines = [
        f"AI-HedgeFund run {run_id} ({trade_date})",
        f"Status: {status}",
        "",
    ]
    if failed:
        lines.append(f"FAILED TICKERS ({len(failed)}):")
        for t in failed:
            err = (results.get(t) or {}).get("error") or (results.get(t) or {}).get("message") or "(no detail)"
            lines.append(f"  {t:6s}  {err}")
        lines.append("")

    ok_items = [(t, r) for t, r in sorted(results.items()) if t not in failed]
    if ok_items:
        lines.append("DECISIONS:")
        for ticker, r in ok_items:
            decision = (r or {}).get("decisions", {}).get(ticker) or {}
            action = decision.get("action", "?")
            qty = decision.get("quantity", 0)
            conf = decision.get("confidence", "?")
            lines.append(f"  {ticker:6s}  {action:5s}  qty={qty}  conf={conf}")
    return "\n".join(lines) + "\n"

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
import uuid

import boto3


def _write_summary_to_md_store(trade_date: str, run_id: str, results: dict, failed: list[str], status: str) -> None:
    """Write the run-summary markdown to MD-Store at AIHedge/summary.md.

    One file total, overwritten every run. Contains ticker + decision for
    every ticker in the run, plus the overall status line. Best-effort:
    failures are logged but do not fail the aggregate Lambda — the per-
    ticker writes done by Runtime are the authoritative outputs.
    """
    import logging

    log = logging.getLogger("aggregate.summary")
    endpoint = os.environ.get("AIHEDGE_MD_STORE_URL")
    prefix = os.environ.get("AIHEDGE_MD_STORE_PREFIX", "AIHedge")
    secret_id = os.environ.get("AIHEDGE_MD_STORE_SECRET_ID", "aihedge/md-store-token")
    if not endpoint:
        log.warning("AIHEDGE_MD_STORE_URL not set; skipping summary write")
        return
    try:
        sm = boto3.client("secretsmanager")
        raw = sm.get_secret_value(SecretId=secret_id)["SecretString"]
        try:
            token = json.loads(raw).get("token") or raw
        except (json.JSONDecodeError, TypeError):
            token = raw
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read MD-Store token: %s", exc)
        return

    lines = [
        "# AI-HedgeFund — run summary",
        f"- trade_date: {trade_date}",
        f"- run_id: `{run_id}`",
        f"- status: **{status}**",
        "",
        "| ticker | action | quantity | confidence |",
        "| --- | --- | --- | --- |",
    ]
    for ticker in sorted(results.keys()):
        r = results.get(ticker) or {}
        if ticker in failed:
            lines.append(f"| {ticker} | FAILED | — | — |")
            continue
        decision = (r.get("decisions") or {}).get(ticker) or {}
        lines.append(
            f"| {ticker} | {decision.get('action', '?')} | {decision.get('quantity', 0)} | {decision.get('confidence', '?')} |"
        )
    body = "\n".join(lines) + "\n"

    key = f"{prefix}/summary.md"
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": "write_file", "arguments": {"key": key, "content": body}},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Agent-Id": "ai-hedge-fund-aggregate",
        "MCP-Protocol-Version": "2025-06-18",
    }
    try:
        import urllib.request

        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = json.loads(resp.read().decode("utf-8"))
        if "error" in resp_body:
            log.error("MD-Store summary write returned error: %s", resp_body["error"])
    except Exception as exc:  # noqa: BLE001
        log.error("MD-Store summary write failed (non-fatal): %s", exc)


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

    # Write the consolidated decision summary to MD-Store at AIHedge/summary.md.
    # Best-effort — never blocks the SFN on MD-Store outage.
    _write_summary_to_md_store(trade_date, run_id, results, failed, status)

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

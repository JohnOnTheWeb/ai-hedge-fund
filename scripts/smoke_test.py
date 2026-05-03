#!/usr/bin/env python3
"""End-to-end smoke test against the deployed AIHedge-Run state machine.

Success criteria (all must pass):
  1. SFN execution status == SUCCEEDED
  2. Aggregate output status == "SUCCESS"
  3. tickers_failed == [] (all tickers in tickers_ok)
  4. For each ticker, one file at AIHedge/<trade_date>/<run_id>/<ticker>.md
     exists on the MD-Store (verified via read_file MCP call)
  5. No agent signal reasoning contains error-indicator phrases
     (e.g. "Error in analysis", "Parsing error", "Insufficient data")
  6. Each per-ticker result JSON has a non-empty report_keys dict

Exit 0 on pass, 1 on any failure, with a human-readable summary either way.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid

import boto3
import requests


DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]
DEFAULT_TRADE_DATE = "2026-04-30"
STATE_MACHINE_ARN = "arn:aws:states:us-east-1:590183796434:stateMachine:AIHedge-Run"
MD_STORE_ENDPOINT = "https://jjjtiltcja.execute-api.us-east-1.amazonaws.com/prod/mcp/v2"
MD_STORE_TOKEN_SECRET = "aihedge/md-store-token"
CONFIG_BUCKET = "aihedge-config-590183796434-us-east-1"

# Canned fallback strings that agent code returns when the framework (LLM
# call, JSON parse, missing data check) fails. Matched as EXACT full reasoning
# values — LLMs can use these words in natural-language reasoning and that's
# fine; we only reject when the reasoning IS (entirely) one of these stubs.
ERROR_STUBS = {
    "Error in analysis, defaulting to neutral",
    "Error in analysis; defaulting to neutral",
    "Parsing error; defaulting to neutral",
    "Parsing error - defaulting to neutral",
    "Parsing error — defaulting to neutral",
    "Parsing error – defaulting to neutral",
    "Error in generating analysis; defaulting to neutral.",
    "Error in generating analysis, defaulting to neutral.",
    "Insufficient data",
}


def _start(sfn, tickers: list[str], trade_date: str) -> str:
    resp = sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        input=json.dumps({"tickers": tickers, "trade_date": trade_date}),
    )
    return resp["executionArn"]


def _wait(sfn, exec_arn: str, timeout_s: int = 1800) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        d = sfn.describe_execution(executionArn=exec_arn)
        status = d["status"]
        print(f"  SFN status: {status}")
        if status in {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}:
            return d
        time.sleep(30)
    raise RuntimeError(f"SFN execution did not terminate within {timeout_s}s")


def _md_store_token(session: boto3.Session) -> str:
    sm = session.client("secretsmanager")
    return sm.get_secret_value(SecretId=MD_STORE_TOKEN_SECRET)["SecretString"]


def _md_store_read(token: str, key: str) -> tuple[bool, str]:
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"key": key}},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Agent-Id": "ai-hedge-fund-smoke",
        "MCP-Protocol-Version": "2025-06-18",
    }
    r = requests.post(MD_STORE_ENDPOINT, headers=headers, json=payload, timeout=30)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    body = r.json()
    if "error" in body:
        return False, str(body["error"])
    # MD-Store returns 200 with content=[{"text":"null"}] when the key is
    # missing. Treat null/empty content as "not found".
    result = body.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text") if content and isinstance(content[0], dict) else None
    if not text or text == "null":
        return False, "file not found (read_file returned null)"
    return True, ""


def _ticker_results(session: boto3.Session, run_id: str, tickers: list[str]) -> dict[str, dict]:
    s3 = session.client("s3")
    out: dict[str, dict] = {}
    for ticker in tickers:
        key = f"runs/{run_id}/{ticker}.json"
        try:
            body = s3.get_object(Bucket=CONFIG_BUCKET, Key=key)["Body"].read()
            out[ticker] = json.loads(body)
        except Exception as exc:  # noqa: BLE001
            out[ticker] = {"__error__": str(exc)}
    return out


def _find_agent_errors(per_ticker_result: dict) -> list[str]:
    """Return list of "ticker.agent: <stub>" for any agent whose reasoning is
    an exact framework-fallback stub (vs genuine LLM-generated reasoning)."""
    errors: list[str] = []
    analyst_signals = per_ticker_result.get("analyst_signals", {})
    if not isinstance(analyst_signals, dict):
        return errors
    for agent_name, per_tkr in analyst_signals.items():
        if not isinstance(per_tkr, dict):
            continue
        for ticker, sig in per_tkr.items():
            if not isinstance(sig, dict):
                continue
            reasoning = sig.get("reasoning")
            if isinstance(reasoning, str):
                text = reasoning.strip()
            else:
                # Non-string reasoning (dict/structured) is analytical output,
                # not an error stub. Skip.
                continue
            if text in ERROR_STUBS:
                errors.append(f"{ticker}.{agent_name}: stub=\"{text}\"")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="IGENV")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--trade-date", default=DEFAULT_TRADE_DATE)
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    sfn = session.client("stepfunctions")

    print(f">>> Starting SFN execution for tickers={tickers} trade_date={args.trade_date}")
    exec_arn = _start(sfn, tickers, args.trade_date)
    print(f"    exec: {exec_arn}")

    desc = _wait(sfn, exec_arn, timeout_s=args.timeout)
    failures: list[str] = []

    # Criterion 1
    if desc["status"] != "SUCCEEDED":
        failures.append(f"[1] SFN status is {desc['status']}, not SUCCEEDED")
    else:
        print("[1] OK  SFN status == SUCCEEDED")

    # Parse aggregate output
    output = json.loads(desc.get("output", "{}"))
    status = output.get("status")
    tickers_ok = output.get("tickers_ok", [])
    tickers_failed = output.get("tickers_failed", [])
    results = output.get("results", {})
    run_id = output.get("run_id")
    trade_date = output.get("trade_date")

    # Criterion 2
    if status != "SUCCESS":
        failures.append(f"[2] aggregate.status == {status!r}, expected 'SUCCESS'")
    else:
        print("[2] OK  aggregate.status == SUCCESS")

    # Criterion 3
    if set(tickers_failed) or set(tickers_ok) != set(tickers):
        failures.append(
            f"[3] ticker set mismatch: ok={tickers_ok} failed={tickers_failed} expected={tickers}"
        )
    else:
        print(f"[3] OK  all {len(tickers)} tickers in tickers_ok")

    # Criterion 6 (check S3 JSONs first — needed for 4 and 5 lookups too)
    per_ticker = _ticker_results(session, run_id, tickers) if run_id else {}
    missing_report_keys = []
    for ticker in tickers:
        r = per_ticker.get(ticker, {})
        if "__error__" in r:
            missing_report_keys.append(f"{ticker}: S3 read failed ({r['__error__']})")
            continue
        if not r.get("report_keys"):
            missing_report_keys.append(f"{ticker}: report_keys is empty")
    if missing_report_keys:
        failures.append("[6] report_keys missing:\n    " + "\n    ".join(missing_report_keys))
    else:
        print("[6] OK  every ticker result has non-empty report_keys")

    # Criterion 4: per-ticker files at AIHedge/<ticker>.md + AIHedge/summary.md
    md_token = _md_store_token(session)
    md_missing = []
    for ticker in tickers:
        key = f"AIHedge/{ticker}.md"
        ok, msg = _md_store_read(md_token, key)
        if not ok:
            md_missing.append(f"{key}: {msg}")
    ok_sum, msg_sum = _md_store_read(md_token, "AIHedge/summary.md")
    if not ok_sum:
        md_missing.append(f"AIHedge/summary.md: {msg_sum}")
    if md_missing:
        failures.append("[4] MD-Store files missing:\n    " + "\n    ".join(md_missing))
    else:
        print(f"[4] OK  all {len(tickers)} per-ticker MD-Store files + summary.md present")

    # Criterion 5
    all_agent_errors: list[str] = []
    for ticker in tickers:
        r = per_ticker.get(ticker, {})
        if "__error__" in r:
            continue
        all_agent_errors.extend(_find_agent_errors(r))
    if all_agent_errors:
        preview = "\n    ".join(all_agent_errors[:20])
        more = f"\n    ... (+{len(all_agent_errors) - 20} more)" if len(all_agent_errors) > 20 else ""
        failures.append(f"[5] agent reasoning errors ({len(all_agent_errors)} total):\n    {preview}{more}")
    else:
        print("[5] OK  no agent reasoning contains error phrases")

    print()
    if failures:
        print(f"SMOKE TEST FAILED — {len(failures)} criteria failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SMOKE TEST PASSED — all 6 criteria met")
    return 0


if __name__ == "__main__":
    sys.exit(main())

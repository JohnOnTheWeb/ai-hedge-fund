"""MCP `memory-log` target — DynamoDB past-context for Portfolio Manager.

Table: aihedge-memory-log
  PK ticker                (S)
  SK trade_date_run        (S)  e.g. "2026-05-02#<run_id>"
  attrs: decision, analyst_signals, cost_usd, tokens, pending (bool),
         realized_return_raw, realized_return_vs_spy, ttl

Event shape: see data_tools.handler — same Gateway contract (tool name in
context.client_context.custom, args splatted on event).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

_TTL_DAYS = 400
_META_KEYS = frozenset({"toolName", "name", "arguments", "input", "tool_name"})


def _table():
    name = os.environ.get("AIHEDGE_MEMORY_TABLE", "aihedge-memory-log")
    return boto3.resource("dynamodb").Table(name)


def _to_decimal(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, list):
        return [_to_decimal(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    return obj


def _to_json_safe(obj):
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    if isinstance(obj, list):
        return [_to_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    return obj


def _resolve_tool_name_and_args(event: dict, context) -> tuple[str, dict]:
    raw_name = ""
    client_ctx = getattr(context, "client_context", None)
    custom = getattr(client_ctx, "custom", None) if client_ctx else None
    if custom:
        raw_name = custom.get("bedrockAgentCoreToolName") or ""

    if not raw_name:
        raw_name = event.get("toolName") or event.get("name") or event.get("tool_name") or ""

    tool_name = raw_name.rsplit("___", 1)[-1] if "___" in raw_name else raw_name

    if "arguments" in event and isinstance(event["arguments"], dict):
        args = event["arguments"]
    elif "input" in event and isinstance(event["input"], dict):
        args = event["input"]
    else:
        args = {k: v for k, v in event.items() if k not in _META_KEYS}

    return tool_name, args


def handler(event, context):
    tool_name, args = _resolve_tool_name_and_args(event or {}, context)
    tbl = _table()

    if tool_name == "get_past_context":
        ticker = args["ticker"]
        same_limit = int(args.get("same_ticker_limit", 5))
        cross_limit = int(args.get("cross_ticker_limit", 10))

        same = tbl.query(
            KeyConditionExpression=Key("ticker").eq(ticker),
            ScanIndexForward=False,
            Limit=same_limit,
        ).get("Items", [])
        cross = tbl.scan(Limit=cross_limit).get("Items", [])
        return {
            "tool_name": tool_name,
            "result": {
                "same_ticker": [_to_json_safe(i) for i in same],
                "cross_ticker": [_to_json_safe(i) for i in cross],
            },
        }

    if tool_name == "store_decision":
        ticker = args["ticker"]
        trade_date = args["trade_date"]
        run_id = args["run_id"]
        ttl = int(time.time()) + _TTL_DAYS * 24 * 3600
        item = {
            "ticker": ticker,
            "trade_date_run": f"{trade_date}#{run_id}",
            "trade_date": trade_date,
            "run_id": run_id,
            "decision": _to_decimal(args.get("decision") or {}),
            "analyst_signals": _to_decimal(args.get("analyst_signals") or {}),
            "cost_usd": _to_decimal(args.get("cost_usd", 0.0)),
            "tokens": _to_decimal(args.get("tokens") or {}),
            "pending": True,
            "created_at": datetime.utcnow().isoformat(),
            "ttl": ttl,
        }
        tbl.put_item(Item=item)
        return {"tool_name": tool_name, "result": {"stored": True, "key": item["trade_date_run"]}}

    if tool_name == "get_pending_entries":
        ticker = args["ticker"]
        older_than_days = int(args.get("older_than_days", 1))
        cutoff = (datetime.utcnow() - timedelta(days=older_than_days)).strftime("%Y-%m-%d")
        items = tbl.query(
            KeyConditionExpression=Key("ticker").eq(ticker) & Key("trade_date_run").lt(f"{cutoff}#~"),
            ScanIndexForward=False,
            Limit=20,
        ).get("Items", [])
        pending = [i for i in items if i.get("pending")]
        return {"tool_name": tool_name, "result": [_to_json_safe(i) for i in pending]}

    if tool_name == "update_realized_returns":
        tbl.update_item(
            Key={"ticker": args["ticker"], "trade_date_run": args["trade_date_run"]},
            UpdateExpression="SET realized_return_raw=:r, realized_return_vs_spy=:s, pending=:p",
            ExpressionAttributeValues={
                ":r": _to_decimal(args["realized_return_raw"]),
                ":s": _to_decimal(args["realized_return_vs_spy"]),
                ":p": False,
            },
        )
        return {"tool_name": tool_name, "result": {"updated": True}}

    return {"error": f"unknown tool: {tool_name}"}

"""MCP `data-tools` target — Financial Datasets API.

Reuses the existing helpers in `src/tools/api.py` so local CLI and in-AWS
behaviour stay in lockstep. API key is resolved from Secrets Manager on
cold start.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3

from src.tools.api import (
    get_company_news,
    get_financial_metrics,
    get_insider_trades,
    get_market_cap,
    get_prices,
    search_line_items,
)


@lru_cache(maxsize=1)
def _api_key() -> str:
    secret_id = os.environ.get("AIHEDGE_FINANCIAL_DATASETS_SECRET_ID", "aihedge/financial-datasets")
    sm = boto3.client("secretsmanager")
    value = sm.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        return json.loads(value).get("api_key", value)
    except (json.JSONDecodeError, TypeError):
        return value


def _as_list(obj):
    if obj is None:
        return []
    if isinstance(obj, list):
        return [x.model_dump() if hasattr(x, "model_dump") else x for x in obj]
    return obj.model_dump() if hasattr(obj, "model_dump") else obj


def handler(event, _context):
    """Gateway dispatches one tool per invocation; event carries `name` + `arguments`."""
    name = event.get("toolName") or event.get("name")
    args = event.get("arguments") or event.get("input") or {}
    api_key = _api_key()

    if name == "get_prices":
        data = get_prices(args["ticker"], args["start_date"], args["end_date"], api_key=api_key)
    elif name == "get_financial_metrics":
        data = get_financial_metrics(
            args["ticker"],
            args["end_date"],
            period=args.get("period", "ttm"),
            limit=int(args.get("limit", 10)),
            api_key=api_key,
        )
    elif name == "search_line_items":
        data = search_line_items(
            args["ticker"],
            args["line_items"],
            args["end_date"],
            period=args.get("period", "ttm"),
            limit=int(args.get("limit", 10)),
            api_key=api_key,
        )
    elif name == "get_market_cap":
        data = get_market_cap(args["ticker"], args["end_date"], api_key=api_key)
    elif name == "get_company_news":
        data = get_company_news(
            args["ticker"],
            args["end_date"],
            limit=int(args.get("limit", 50)),
            api_key=api_key,
        )
    elif name == "get_insider_trades":
        data = get_insider_trades(
            args["ticker"],
            args["end_date"],
            limit=int(args.get("limit", 50)),
            api_key=api_key,
        )
    else:
        return {"error": f"unknown tool: {name}"}

    return {"result": _as_list(data)}

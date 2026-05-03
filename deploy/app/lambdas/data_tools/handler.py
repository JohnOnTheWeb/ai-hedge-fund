"""MCP `data-tools` target — Financial Datasets API.

Self-contained: does NOT import `src.tools.api`. That module is Gateway-routed
when `AIHEDGE_GATEWAY_URL` is set, which would create a Lambda → Gateway →
Lambda → Gateway infinite loop.

As a defense-in-depth, we assert at cold start that `AIHEDGE_GATEWAY_URL` is
unset in this environment so a future wiring mistake fails loudly instead of
looping.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3

from deploy.app.lambdas.data_tools import vendor

_GATEWAY_URL_ENV = "AIHEDGE_GATEWAY_URL"
assert not os.environ.get(_GATEWAY_URL_ENV), (
    f"{_GATEWAY_URL_ENV} must NOT be set on the data-tools Lambda; that would "
    "create a Lambda→Gateway→Lambda loop. Unset it in the task definition."
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


def handler(event, _context):
    """Gateway dispatches one tool per invocation; event carries `name` + `arguments`.

    Gateway namespaces the tool name as `data-tools___<tool>`; strip the prefix
    before dispatching.
    """
    raw_name = event.get("toolName") or event.get("name") or ""
    name = raw_name.split("___", 1)[-1] if "___" in raw_name else raw_name
    args = event.get("arguments") or event.get("input") or {}
    api_key = _api_key()

    if name == "get_prices":
        data = vendor.get_prices(args["ticker"], args["start_date"], args["end_date"], api_key)
    elif name == "get_financial_metrics":
        data = vendor.get_financial_metrics(
            args["ticker"],
            args["end_date"],
            period=args.get("period", "ttm"),
            limit=int(args.get("limit", 10)),
            api_key=api_key,
        )
    elif name == "search_line_items":
        data = vendor.search_line_items(
            args["ticker"],
            args["line_items"],
            args["end_date"],
            period=args.get("period", "ttm"),
            limit=int(args.get("limit", 10)),
            api_key=api_key,
        )
    elif name == "get_market_cap":
        data = vendor.get_market_cap(args["ticker"], args["end_date"], api_key)
    elif name == "get_company_news":
        data = vendor.get_company_news(
            args["ticker"],
            args["end_date"],
            limit=int(args.get("limit", 50)),
            api_key=api_key,
        )
    elif name == "get_insider_trades":
        data = vendor.get_insider_trades(
            args["ticker"],
            args["end_date"],
            limit=int(args.get("limit", 50)),
            api_key=api_key,
        )
    else:
        return {"error": f"unknown tool: {raw_name}"}

    return {"tool_name": raw_name, "result": data}

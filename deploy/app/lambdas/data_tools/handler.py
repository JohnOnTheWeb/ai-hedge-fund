"""MCP `data-tools` target — Financial Datasets API.

Self-contained: does NOT import `src.tools.api`. That module is Gateway-routed
when `AIHEDGE_GATEWAY_URL` is set, which would create a Lambda → Gateway →
Lambda → Gateway infinite loop.

Event-shape contract (TauricResearch 2026-05-02):
  - Tool name arrives via `context.client_context.custom["bedrockAgentCoreToolName"]`,
    namespaced as `data-tools___<tool>`. Strip the `<target>___` prefix.
  - Tool **arguments are splatted at the top level of `event`**. No `arguments`
    envelope. A handful of MCP/Powertools-style meta keys can appear; ignore them.
  - Direct-invoke (for smoke tests) falls back to `event["toolName"]` /
    `event["name"]` + `event["arguments"]`.
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

# Keys the handler should NOT treat as tool arguments when the Gateway splats
# the event at the top level.
_META_KEYS = frozenset({"toolName", "name", "arguments", "input", "tool_name"})


@lru_cache(maxsize=1)
def _api_key() -> str:
    secret_id = os.environ.get("AIHEDGE_FINANCIAL_DATASETS_SECRET_ID", "aihedge/financial-datasets")
    sm = boto3.client("secretsmanager")
    value = sm.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        return json.loads(value).get("api_key", value)
    except (json.JSONDecodeError, TypeError):
        return value


def _resolve_tool_name_and_args(event: dict, context) -> tuple[str, dict]:
    """Return (bare_tool_name, kwargs) for both Gateway and direct invocations."""
    # Gateway path — tool name in client_context.custom, args splatted on event.
    raw_name = ""
    client_ctx = getattr(context, "client_context", None)
    custom = getattr(client_ctx, "custom", None) if client_ctx else None
    if custom:
        raw_name = custom.get("bedrockAgentCoreToolName") or ""

    # Direct-invoke fallback (smoke tests, Step Functions).
    if not raw_name:
        raw_name = event.get("toolName") or event.get("name") or event.get("tool_name") or ""

    tool_name = raw_name.rsplit("___", 1)[-1] if "___" in raw_name else raw_name

    # Args: Gateway splats top-level; direct invoke typically uses "arguments".
    if "arguments" in event and isinstance(event["arguments"], dict):
        args = event["arguments"]
    elif "input" in event and isinstance(event["input"], dict):
        args = event["input"]
    else:
        args = {k: v for k, v in event.items() if k not in _META_KEYS}

    return tool_name, args


def handler(event, context):
    tool_name, args = _resolve_tool_name_and_args(event or {}, context)
    api_key = _api_key()

    if tool_name == "get_prices":
        data = vendor.get_prices(args["ticker"], args["start_date"], args["end_date"], api_key)
    elif tool_name == "get_financial_metrics":
        data = vendor.get_financial_metrics(
            args["ticker"],
            args["end_date"],
            period=args.get("period", "ttm"),
            limit=int(args.get("limit", 10)),
            api_key=api_key,
        )
    elif tool_name == "search_line_items":
        data = vendor.search_line_items(
            args["ticker"],
            args["line_items"],
            args["end_date"],
            period=args.get("period", "ttm"),
            limit=int(args.get("limit", 10)),
            api_key=api_key,
        )
    elif tool_name == "get_market_cap":
        data = vendor.get_market_cap(args["ticker"], args["end_date"], api_key)
    elif tool_name == "get_company_news":
        data = vendor.get_company_news(
            args["ticker"],
            args["end_date"],
            limit=int(args.get("limit", 50)),
            api_key=api_key,
        )
    elif tool_name == "get_insider_trades":
        data = vendor.get_insider_trades(
            args["ticker"],
            args["end_date"],
            limit=int(args.get("limit", 50)),
            api_key=api_key,
        )
    else:
        return {"error": f"unknown tool: {tool_name}"}

    return {"tool_name": tool_name, "result": data}

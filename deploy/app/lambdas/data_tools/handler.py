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

# yfinance fallback is imported lazily on first use (import is ~1s cold).
_yf_vendor = None


def _yfinance():
    global _yf_vendor
    if _yf_vendor is None:
        from deploy.app.lambdas.data_tools import yfinance_vendor as _mod
        _yf_vendor = _mod
    return _yf_vendor


def _route(primary_call, fallback_call, *, tool: str):
    """Run FD first; on any exception or empty list, run yfinance.

    Mirrors TauricResearch's route_to_vendor: agents see one stable tool
    contract regardless of which vendor answered. Fallback triggers on
    exceptions OR empty results (FD returns 200 + empty list when a key
    plan can't access that ticker).
    """
    import logging
    log = logging.getLogger("data_tools.router")
    try:
        result = primary_call()
        if result:
            return result
        log.warning("%s: primary (FD) returned empty; falling back to yfinance", tool)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: primary (FD) raised %s: %s; falling back to yfinance", tool, type(exc).__name__, exc)
    try:
        return fallback_call()
    except Exception as exc:  # noqa: BLE001
        log.error("%s: fallback (yfinance) also failed: %s: %s", tool, type(exc).__name__, exc)
        return []

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
        data = _route(
            lambda: vendor.get_prices(args["ticker"], args["start_date"], args["end_date"], api_key),
            lambda: _yfinance().get_prices(args["ticker"], args["start_date"], args["end_date"]),
            tool=tool_name,
        )
    elif tool_name == "get_financial_metrics":
        period = args.get("period", "ttm")
        limit = int(args.get("limit", 10))
        data = _route(
            lambda: vendor.get_financial_metrics(args["ticker"], args["end_date"], period=period, limit=limit, api_key=api_key),
            lambda: _yfinance().get_financial_metrics(args["ticker"], args["end_date"], period=period, limit=limit),
            tool=tool_name,
        )
    elif tool_name == "search_line_items":
        period = args.get("period", "ttm")
        limit = int(args.get("limit", 10))
        data = _route(
            lambda: vendor.search_line_items(args["ticker"], args["line_items"], args["end_date"], period=period, limit=limit, api_key=api_key),
            lambda: _yfinance().search_line_items(args["ticker"], args["line_items"], args["end_date"], period=period, limit=limit),
            tool=tool_name,
        )
    elif tool_name == "get_market_cap":
        primary_val = vendor.get_market_cap(args["ticker"], args["end_date"], api_key)
        data = primary_val if primary_val is not None else _yfinance().get_market_cap(args["ticker"], args["end_date"])
    elif tool_name == "get_company_news":
        limit = int(args.get("limit", 50))
        data = _route(
            lambda: vendor.get_company_news(args["ticker"], args["end_date"], limit=limit, api_key=api_key),
            lambda: _yfinance().get_company_news(args["ticker"], args["end_date"], limit=limit),
            tool=tool_name,
        )
    elif tool_name == "get_insider_trades":
        limit = int(args.get("limit", 50))
        data = _route(
            lambda: vendor.get_insider_trades(args["ticker"], args["end_date"], limit=limit, api_key=api_key),
            lambda: _yfinance().get_insider_trades(args["ticker"], args["end_date"], limit=limit),
            tool=tool_name,
        )
    else:
        return {"error": f"unknown tool: {tool_name}"}

    return {"tool_name": tool_name, "result": data}

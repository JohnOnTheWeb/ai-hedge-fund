"""MCP `options-tools` target — yfinance-backed vol/earnings signals.

Adapted from TauricResearch brokerage_mcp tools. We don't (yet) have Schwab
or Tastytrade creds in this account, so everything is sourced from yfinance.
The tool contracts are the same shapes TA's agents consume, so if we later
add Schwab, only the vendor module changes.

Tools exposed:
  - get_historical_vol: annualized realized vol for N-day windows
  - get_earnings_context: next earnings date + recent EPS history
  - get_options_chain: ATM options chain at expiration closest to dte_target
"""
from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any

_META_KEYS = frozenset({"toolName", "name", "arguments", "input", "tool_name"})

_GATEWAY_URL_ENV = "AIHEDGE_GATEWAY_URL"
assert not os.environ.get(_GATEWAY_URL_ENV), (
    f"{_GATEWAY_URL_ENV} must NOT be set on the options-tools Lambda"
)


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


def _historical_vol(ticker: str, windows: list[int]) -> dict[str, Any]:
    import yfinance as yf
    import numpy as np

    df = yf.Ticker(ticker).history(period=f"{max(windows) + 30}d", auto_adjust=False)
    if df is None or df.empty:
        return {"ticker": ticker, "windows": {}, "note": "no price data"}
    closes = df["Close"].astype(float).values
    log_ret = np.diff(np.log(closes))
    out: dict[str, float] = {}
    for w in windows:
        if len(log_ret) < w:
            continue
        segment = log_ret[-w:]
        daily_std = float(segment.std(ddof=1))
        # Annualize: ~252 trading days
        out[str(w)] = round(daily_std * math.sqrt(252), 4)
    return {"ticker": ticker, "windows": out}


def _earnings_context(ticker: str) -> dict[str, Any]:
    import yfinance as yf

    t = yf.Ticker(ticker)
    info = t.info or {}
    next_earnings = None
    try:
        cal = t.calendar
        if cal is not None and "Earnings Date" in cal:
            dates = cal["Earnings Date"]
            if dates:
                first = dates[0] if isinstance(dates, list) else dates
                next_earnings = str(first) if first else None
    except Exception:
        pass

    history: list[dict[str, Any]] = []
    try:
        eh = t.earnings_history
        if eh is not None and not eh.empty:
            for idx, row in eh.head(8).iterrows():
                history.append({
                    "period": str(idx),
                    "eps_estimate": float(row.get("epsEstimate", 0) or 0),
                    "eps_actual": float(row.get("epsActual", 0) or 0),
                    "surprise_pct": float(row.get("surprisePercent", 0) or 0),
                })
    except Exception:
        pass

    return {
        "ticker": ticker,
        "next_earnings_date": next_earnings,
        "forward_pe": info.get("forwardPE"),
        "trailing_eps": info.get("trailingEps"),
        "forward_eps": info.get("forwardEps"),
        "earnings_history": history,
    }


def _options_chain(ticker: str, dte_target: int, strikes_width: int) -> dict[str, Any]:
    import yfinance as yf

    t = yf.Ticker(ticker)
    expirations = t.options or ()
    if not expirations:
        return {"ticker": ticker, "note": "no options", "chain": {}}
    today = datetime.utcnow().date()
    # Pick expiration closest to dte_target days out.
    best_exp = min(
        expirations,
        key=lambda e: abs((datetime.fromisoformat(e).date() - today).days - dte_target),
    )
    try:
        chain = t.option_chain(best_exp)
    except Exception as exc:
        return {"ticker": ticker, "expiration": best_exp, "error": str(exc)}
    spot = None
    try:
        spot_df = t.history(period="1d")
        if not spot_df.empty:
            spot = float(spot_df["Close"].iloc[-1])
    except Exception:
        pass

    def _near_atm(df) -> list[dict[str, Any]]:
        if df is None or df.empty or spot is None:
            return []
        df = df.assign(_abs=(df["strike"] - spot).abs()).sort_values("_abs")
        return [
            {
                "strike": float(r["strike"]),
                "last_price": float(r.get("lastPrice") or 0),
                "bid": float(r.get("bid") or 0),
                "ask": float(r.get("ask") or 0),
                "implied_vol": float(r.get("impliedVolatility") or 0),
                "open_interest": int(r.get("openInterest") or 0),
                "volume": int(r.get("volume") or 0),
            }
            for _, r in df.head(strikes_width).iterrows()
        ]

    return {
        "ticker": ticker,
        "expiration": best_exp,
        "spot": spot,
        "calls": _near_atm(chain.calls),
        "puts": _near_atm(chain.puts),
    }


def handler(event, context):
    tool_name, args = _resolve_tool_name_and_args(event or {}, context)

    if tool_name == "get_historical_vol":
        windows = args.get("windows") or [30, 60, 90]
        data = _historical_vol(args["ticker"], [int(w) for w in windows])
    elif tool_name == "get_earnings_context":
        data = _earnings_context(args["ticker"])
    elif tool_name == "get_options_chain":
        data = _options_chain(
            args["ticker"],
            dte_target=int(args.get("dte_target", 30)),
            strikes_width=int(args.get("strikes_width", 10)),
        )
    else:
        return {"error": f"unknown tool: {tool_name}"}

    return {"tool_name": tool_name, "result": data}

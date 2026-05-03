"""Direct FinancialDatasets.ai client for the data-tools Lambda target.

IMPORTANT: this module must NOT import from `src.tools.api` — that module is
Gateway-routed when `AIHEDGE_GATEWAY_URL` is set. If the Lambda ever had that
env var in scope, we'd build a Lambda → Gateway → Lambda → Gateway loop.

Keep this module self-contained: raw HTTP against the vendor, nothing else.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

_BASE = "https://api.financialdatasets.ai"


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-KEY": api_key} if api_key else {}


def _request(method: str, url: str, headers: dict, *, json_body: dict | None = None, max_retries: int = 3) -> requests.Response:
    """GET/POST with 429 linear backoff (mirrors src/tools/api.py semantics)."""
    for attempt in range(max_retries + 1):
        resp = requests.request(method, url, headers=headers, json=json_body, timeout=30)
        if resp.status_code == 429 and attempt < max_retries:
            time.sleep(60 + 30 * attempt)
            continue
        return resp
    return resp


def get_prices(ticker: str, start_date: str, end_date: str, api_key: str) -> list[dict[str, Any]]:
    url = f"{_BASE}/prices/?ticker={ticker}&interval=day&interval_multiplier=1&start_date={start_date}&end_date={end_date}"
    resp = _request("GET", url, _headers(api_key))
    if resp.status_code != 200:
        return []
    return resp.json().get("prices", []) or []


def get_financial_metrics(ticker: str, end_date: str, period: str, limit: int, api_key: str) -> list[dict[str, Any]]:
    url = f"{_BASE}/financial-metrics/?ticker={ticker}&report_period_lte={end_date}&limit={limit}&period={period}"
    resp = _request("GET", url, _headers(api_key))
    if resp.status_code != 200:
        return []
    return resp.json().get("financial_metrics", []) or []


def search_line_items(ticker: str, line_items: list[str], end_date: str, period: str, limit: int, api_key: str) -> list[dict[str, Any]]:
    url = f"{_BASE}/financials/search/line-items"
    body = {"tickers": [ticker], "line_items": line_items, "end_date": end_date, "period": period, "limit": limit}
    resp = _request("POST", url, _headers(api_key), json_body=body)
    if resp.status_code != 200:
        return []
    return (resp.json().get("search_results") or [])[:limit]


def get_market_cap(ticker: str, end_date: str, api_key: str) -> float | None:
    # Company-facts endpoint for "today"; otherwise pull from financial-metrics.
    from datetime import datetime

    if end_date == datetime.utcnow().strftime("%Y-%m-%d"):
        resp = _request("GET", f"{_BASE}/company/facts/?ticker={ticker}", _headers(api_key))
        if resp.status_code != 200:
            return None
        facts = resp.json().get("company_facts", {})
        return facts.get("market_cap")

    metrics = get_financial_metrics(ticker, end_date, period="ttm", limit=1, api_key=api_key)
    if not metrics:
        return None
    return metrics[0].get("market_cap")


def get_company_news(ticker: str, end_date: str, limit: int, api_key: str) -> list[dict[str, Any]]:
    url = f"{_BASE}/news/?ticker={ticker}&end_date={end_date}&limit={limit}"
    resp = _request("GET", url, _headers(api_key))
    if resp.status_code != 200:
        return []
    return resp.json().get("news", []) or []


def get_insider_trades(ticker: str, end_date: str, limit: int, api_key: str) -> list[dict[str, Any]]:
    url = f"{_BASE}/insider-trades/?ticker={ticker}&filing_date_lte={end_date}&limit={limit}"
    resp = _request("GET", url, _headers(api_key))
    if resp.status_code != 200:
        return []
    return resp.json().get("insider_trades", []) or []

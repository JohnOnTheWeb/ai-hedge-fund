"""Per-ticker tool-result prefetch.

Runs the high-frequency tool calls in parallel ONCE at the start of a ticker's
LangGraph run. Results are cached in `state["data"]["prefetched"][ticker]`.
Tool functions in `src/tools/api.py` consult this cache first and fall back
to the live Gateway call on miss (preserving CLI / non-AgentCore code paths).

Ported from TauricResearch's `tradingagents/graph/prefetch.py`. Differences:
- AIHedge's agents call tools in Python, not via tool-calling LLMs. We cache
  results in state (not markdown in a prompt) and modify the tool wrappers
  to check state first.
- The Lambda layer (`_common/cache.py`) deduplicates cross-ticker and
  across runs. Prefetch deduplicates within a ticker's run.

Design rules:
- Each request is wrapped in its own try/except. One slow tool does not
  block the rest of the bundle — it just returns an empty shape and the
  downstream agent falls through to a live Gateway call.
- The line-items superset MUST include every name any agent requests. If
  an agent asks for a line item not in the superset, the cache miss
  triggers a live call (still correct, just slower).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Superset of line-item names any agent asks for, kept here so prefetch can
# fetch them in one call. Union of `search_line_items([...])` calls across
# src/agents/*.py. Adding a new agent? Add its line items here.
LINE_ITEMS_SUPERSET: list[str] = [
    "book_value_per_share",
    "capital_expenditure",
    "cash_and_equivalents",
    "current_assets",
    "current_liabilities",
    "debt_to_equity",
    "depreciation_and_amortization",
    "dividends_and_other_cash_distributions",
    "earnings_per_share",
    "ebit",
    "ebitda",
    "free_cash_flow",
    "goodwill_and_intangible_assets",
    "gross_profit",
    "issuance_or_purchase_of_equity_shares",
    "long_term_debt",
    "net_income",
    "operating_expense",
    "operating_income",
    "operating_margin",
    "outstanding_shares",
    "research_and_development",
    "revenue",
    "shareholders_equity",
    "total_assets",
    "total_debt",
    "total_liabilities",
    "working_capital",
]

_HISTORY_DAYS = 365  # 1y window is enough for all current agents


def _back(end_date: str, days: int) -> str:
    return (datetime.fromisoformat(end_date) - timedelta(days=days)).strftime("%Y-%m-%d")


def _build_tasks(ticker: str, end_date: str, api_key: str | None) -> list[tuple[str, Any]]:
    """(label, callable) list of prefetch requests for one ticker."""
    from src.tools.api import (
        get_company_news,
        get_earnings_context,
        get_financial_metrics,
        get_historical_vol,
        get_insider_trades,
        get_market_cap,
        get_prices,
        search_line_items,
    )

    start_1y = _back(end_date, _HISTORY_DAYS)

    return [
        ("prices_1y", lambda: get_prices(ticker, start_1y, end_date, api_key=api_key)),
        ("metrics_annual", lambda: get_financial_metrics(ticker, end_date, period="annual", limit=10, api_key=api_key)),
        ("metrics_ttm", lambda: get_financial_metrics(ticker, end_date, period="ttm", limit=10, api_key=api_key)),
        ("line_items_annual", lambda: search_line_items(ticker, LINE_ITEMS_SUPERSET, end_date, period="annual", limit=10, api_key=api_key)),
        ("line_items_ttm", lambda: search_line_items(ticker, LINE_ITEMS_SUPERSET, end_date, period="ttm", limit=5, api_key=api_key)),
        ("market_cap", lambda: get_market_cap(ticker, end_date, api_key=api_key)),
        ("insider_trades", lambda: get_insider_trades(ticker, end_date=end_date, limit=100)),
        ("news", lambda: get_company_news(ticker, end_date=end_date, limit=100)),
        ("historical_vol", lambda: get_historical_vol(ticker, windows=[30, 60, 90])),
        ("earnings_context", lambda: get_earnings_context(ticker)),
    ]


def fetch_bundle(ticker: str, end_date: str, api_key: str | None = None, max_workers: int = 8) -> Dict[str, Any]:
    """Run the prefetch bundle for one ticker. Returns a dict keyed by label.

    Errors are logged and materialize as empty / None values in the bundle;
    agents fall through to live Gateway calls for those paths.
    """
    tasks = _build_tasks(ticker, end_date, api_key)
    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_safe_call, fn): label for label, fn in tasks}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception as exc:  # noqa: BLE001 — defense in depth
                logger.warning("prefetch %s raised: %s", label, exc)
                results[label] = None
    return results


def _safe_call(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("prefetch task failed: %s: %s", type(exc).__name__, exc)
        return None

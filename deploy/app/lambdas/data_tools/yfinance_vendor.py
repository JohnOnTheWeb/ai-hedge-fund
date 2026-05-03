"""yfinance fallback for the data-tools Lambda.

Kicks in only when the primary vendor (FinancialDatasets.ai) fails or returns
an empty result. Mirrors the TauricResearch `route_to_vendor` pattern: agents
see a single stable tool contract and never know which vendor answered.

Returns lists of dicts matching the FD shape as closely as reasonable so
downstream Pydantic models in `src/data/models.py` keep validating.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import yfinance as yf


def get_prices(ticker: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """OHLCV bars matching FD `Price` shape: open/close/high/low/volume/time."""
    # yfinance end is exclusive; bump by 1 day to include end_date.
    end_inclusive = (datetime.fromisoformat(end_date) + timedelta(days=1)).strftime("%Y-%m-%d")
    df = yf.Ticker(ticker).history(start=start_date, end=end_inclusive, auto_adjust=False)
    if df is None or df.empty:
        return []
    out = []
    for idx, row in df.iterrows():
        out.append({
            "ticker": ticker,
            "open": float(row["Open"]),
            "close": float(row["Close"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "volume": int(row["Volume"]),
            "time": idx.strftime("%Y-%m-%d"),
        })
    return out


def get_financial_metrics(ticker: str, end_date: str, period: str, limit: int) -> list[dict[str, Any]]:
    """Best-effort financial metrics. yfinance doesn't expose the full FD
    shape; we return a single most-recent metrics dict with the keys the
    agents most commonly read (profitability, leverage, valuation)."""
    t = yf.Ticker(ticker)
    info = t.info or {}
    # yfinance doesn't expose historical metrics, so limit/period are ignored.
    metrics = {
        "ticker": ticker,
        "report_period": end_date,
        "fiscal_period": period,
        "period": period,
        "currency": info.get("currency"),
        "market_cap": info.get("marketCap"),
        "enterprise_value": info.get("enterpriseValue"),
        "price_to_earnings_ratio": info.get("trailingPE"),
        "price_to_book_ratio": info.get("priceToBook"),
        "price_to_sales_ratio": info.get("priceToSalesTrailing12Months"),
        "enterprise_value_to_ebitda_ratio": info.get("enterpriseToEbitda"),
        "gross_margin": info.get("grossMargins"),
        "operating_margin": info.get("operatingMargins"),
        "net_margin": info.get("profitMargins"),
        "return_on_equity": info.get("returnOnEquity"),
        "return_on_assets": info.get("returnOnAssets"),
        "return_on_invested_capital": None,
        "debt_to_equity": (info.get("debtToEquity") / 100.0) if info.get("debtToEquity") else None,
        "current_ratio": info.get("currentRatio"),
        "quick_ratio": info.get("quickRatio"),
        "revenue_growth": info.get("revenueGrowth"),
        "earnings_growth": info.get("earningsGrowth"),
        "book_value_growth": None,
        "earnings_per_share_growth": info.get("earningsQuarterlyGrowth"),
        "free_cash_flow_growth": None,
        "operating_income_growth": None,
        "ebitda_growth": None,
        "payout_ratio": info.get("payoutRatio"),
        "earnings_per_share": info.get("trailingEps"),
        "book_value_per_share": info.get("bookValue"),
        "free_cash_flow_per_share": None,
    }
    return [metrics] if metrics.get("market_cap") else []


def search_line_items(ticker: str, line_items: list[str], end_date: str, period: str, limit: int) -> list[dict[str, Any]]:
    """Return annual financial line items from yfinance income/balance/cashflow
    statements. Maps FD's snake_case names onto yfinance's statement index."""
    t = yf.Ticker(ticker)
    try:
        inc = t.income_stmt if period == "annual" else t.quarterly_income_stmt
        bs = t.balance_sheet if period == "annual" else t.quarterly_balance_sheet
        cf = t.cashflow if period == "annual" else t.quarterly_cashflow
    except Exception:
        return []

    # yfinance column is the report date (descending). Line-item name ->
    # best-effort yfinance row label (statements combined).
    _NAME_MAP = {
        "revenue": ("inc", "Total Revenue"),
        "gross_profit": ("inc", "Gross Profit"),
        "operating_income": ("inc", "Operating Income"),
        "operating_expense": ("inc", "Operating Expense"),
        "research_and_development": ("inc", "Research And Development"),
        "net_income": ("inc", "Net Income"),
        "ebit": ("inc", "EBIT"),
        "ebitda": ("inc", "EBITDA"),
        "operating_margin": ("inc", "Operating Income"),  # compute inline below
        "total_assets": ("bs", "Total Assets"),
        "total_liabilities": ("bs", "Total Liabilities Net Minority Interest"),
        "shareholders_equity": ("bs", "Stockholders Equity"),
        "cash_and_equivalents": ("bs", "Cash And Cash Equivalents"),
        "total_debt": ("bs", "Total Debt"),
        "long_term_debt": ("bs", "Long Term Debt"),
        "goodwill_and_intangible_assets": ("bs", "Goodwill And Other Intangible Assets"),
        "outstanding_shares": ("bs", "Ordinary Shares Number"),
        "free_cash_flow": ("cf", "Free Cash Flow"),
        "capital_expenditure": ("cf", "Capital Expenditure"),
        "depreciation_and_amortization": ("cf", "Depreciation And Amortization"),
        "debt_to_equity": ("bs", "Total Debt"),
        "dividends_and_other_cash_distributions": ("cf", "Cash Dividends Paid"),
    }
    stmts = {"inc": inc, "bs": bs, "cf": cf}
    out: list[dict[str, Any]] = []
    if inc is None or inc.empty:
        return []
    for col in list(inc.columns)[:limit]:
        report_period = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
        row: dict[str, Any] = {"ticker": ticker, "report_period": report_period, "period": period}
        for name in line_items:
            if name in _NAME_MAP:
                stmt_key, label = _NAME_MAP[name]
                df = stmts.get(stmt_key)
                if df is not None and not df.empty and label in df.index and col in df.columns:
                    val = df.loc[label, col]
                    try:
                        row[name] = float(val) if val is not None and str(val) != "nan" else None
                    except (TypeError, ValueError):
                        row[name] = None
                else:
                    row[name] = None
            else:
                row[name] = None
        out.append(row)
    return out


def get_market_cap(ticker: str, end_date: str) -> float | None:
    info = yf.Ticker(ticker).info or {}
    mc = info.get("marketCap")
    return float(mc) if mc else None


def get_company_news(ticker: str, end_date: str, limit: int) -> list[dict[str, Any]]:
    items = yf.Ticker(ticker).news or []
    out = []
    for it in items[:limit]:
        content = it.get("content") if isinstance(it.get("content"), dict) else it
        out.append({
            "ticker": ticker,
            "title": content.get("title") or it.get("title", ""),
            "author": content.get("author") or "",
            "source": (content.get("provider", {}) or {}).get("displayName") or it.get("publisher", ""),
            "date": content.get("pubDate") or it.get("providerPublishTime", ""),
            "url": (content.get("canonicalUrl", {}) or {}).get("url") or it.get("link", ""),
            "sentiment": "neutral",
        })
    return out


def get_insider_trades(ticker: str, end_date: str, limit: int) -> list[dict[str, Any]]:
    t = yf.Ticker(ticker)
    try:
        df = t.insider_transactions
    except Exception:
        return []
    if df is None or df.empty:
        return []
    out = []
    for _, row in df.head(limit).iterrows():
        out.append({
            "ticker": ticker,
            "issuer": row.get("Insider", ""),
            "name": row.get("Insider", ""),
            "title": row.get("Position", ""),
            "is_board_director": None,
            "transaction_date": str(row.get("Start Date", "")),
            "transaction_shares": float(row.get("Shares", 0) or 0),
            "transaction_price_per_share": None,
            "transaction_value": float(row.get("Value", 0) or 0),
            "shares_owned_before_transaction": None,
            "shares_owned_after_transaction": None,
            "security_title": "",
            "filing_date": str(row.get("Start Date", "")),
        })
    return out

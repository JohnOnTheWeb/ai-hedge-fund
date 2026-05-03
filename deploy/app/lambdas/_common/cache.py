"""DynamoDB-backed tool-result cache for the MCP target Lambdas.

Ported from TauricResearch (infra/lambdas/data_tools/cache.py). The design:

- Key shape: `<tool>::<args_hash>::<date_bucket>`.
- `date_bucket` is chosen per tool. Historical data (end_date in the past)
  uses `"all"` so hits span days forever. Today-sensitive data uses a
  15-minute slot to bound intraday staleness.
- TTL is stored as the DynamoDB-native `ttl` attribute so DDB expires rows
  server-side (no sweeper).
- Best-effort: if the table is unreachable, the producer is called
  directly. Cache miss must never block the tool.
- Error sentinels (string payloads starting with "[..." and containing
  "unavailable") are never cached — otherwise a transient failure would
  freeze into a permanent one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, date, timezone
from typing import Any, Callable, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_TABLE_NAME = os.environ.get("AIHEDGE_TOOL_CACHE_TABLE", "")
_table = None
_cloudwatch = None
_METRIC_NS = os.environ.get("AIHEDGE_TOOL_CACHE_METRIC_NAMESPACE", "AIHedge/ToolCache")


def _emit_metric(tool: str, hit: bool) -> None:
    """Best-effort CloudWatch hit/miss counter. Silent on failure."""
    global _cloudwatch
    try:
        if _cloudwatch is None:
            _cloudwatch = boto3.client("cloudwatch")
        _cloudwatch.put_metric_data(
            Namespace=_METRIC_NS,
            MetricData=[
                {
                    "MetricName": "Hit" if hit else "Miss",
                    "Dimensions": [{"Name": "Tool", "Value": tool}],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:  # noqa: BLE001
        pass


# Per-tool TTL in seconds. Err on the short side rather than serving stale
# data. Override via env TOOL_CACHE_TTL_<TOOL>.
_TTL_DEFAULTS: Dict[str, int] = {
    # Historical OHLCV with end_date < today: immutable. The bucket is
    # rewritten to "all" for that case so hits span forever until the 30-day
    # guardrail expires.
    "get_prices": 30 * 24 * 3600,
    # Fundamentals refresh at most quarterly (with earnings). 24h is safe.
    "get_financial_metrics": 24 * 3600,
    "search_line_items": 24 * 3600,
    # Market cap moves intraday but AIHedge runs are once-per-day; daily is fine.
    "get_market_cap": 24 * 3600,
    # Filings and news: daily cadence is tolerable for research.
    "get_insider_trades": 24 * 3600,
    "get_company_news": 4 * 3600,
    # Options-tools: vol regime shifts slowly, earnings dates are calendar items.
    "get_historical_vol": 24 * 3600,
    "get_earnings_context": 24 * 3600,
    "get_options_chain": 15 * 60,  # IV is intraday-sensitive
}


def _get_table():
    global _table
    if _table is None and _TABLE_NAME:
        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        _table = boto3.resource("dynamodb", region_name=region).Table(_TABLE_NAME)
    return _table


def _ttl_seconds(tool: str) -> int:
    override = os.environ.get(f"TOOL_CACHE_TTL_{tool.upper()}")
    if override and override.isdigit():
        return int(override)
    return _TTL_DEFAULTS.get(tool, 6 * 3600)


def _is_past(iso: Optional[str], today_iso: str) -> bool:
    if not iso or not isinstance(iso, str):
        return False
    try:
        return iso < today_iso  # lexicographic on YYYY-MM-DD
    except Exception:  # noqa: BLE001
        return False


def _quarter_hour_slot() -> int:
    now = datetime.now(timezone.utc)
    return (now.hour * 4) + (now.minute // 15)


def _date_bucket(tool: str, args: Dict[str, Any]) -> str:
    """Bucket key that lets caches span days when the output is historical.

    Returns `"all"` when the relevant date in args is strictly before today
    (UTC) — the answer cannot change. Otherwise returns today's date, with
    a 15-minute suffix for tools whose output moves intraday.
    """
    today = date.today().isoformat()

    if tool == "get_prices":
        end = args.get("end_date")
        if _is_past(end, today):
            return "all"
        return f"{today}_q{_quarter_hour_slot()}"

    if tool == "get_company_news":
        end = args.get("end_date")
        if _is_past(end, today):
            return "all"
        return f"{today}_q{_quarter_hour_slot()}"

    if tool in ("get_financial_metrics", "search_line_items"):
        end = args.get("end_date")
        if _is_past(end, today):
            return "all"
        return today

    if tool in ("get_market_cap", "get_insider_trades"):
        end = args.get("end_date")
        if _is_past(end, today):
            return "all"
        return today

    if tool == "get_historical_vol":
        # yfinance realized vol is computed from recent closes; tie to day.
        return today

    if tool == "get_earnings_context":
        return today

    if tool == "get_options_chain":
        return f"{today}_q{_quarter_hour_slot()}"

    return today


def _args_hash(args: Dict[str, Any]) -> str:
    # Canonical JSON for stable hashing. Sort keys; drop None so absent vs.
    # explicit-default args map to the same key.
    canonical = {k: v for k, v in args.items() if v is not None}
    blob = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _cache_key(tool: str, args: Dict[str, Any]) -> str:
    return f"{tool}::{_args_hash(args)}::{_date_bucket(tool, args)}"


def cached_call(
    tool: str,
    args: Dict[str, Any],
    producer: Callable[[], Any],
) -> Any:
    """Look up `tool(**args)` in the DDB cache; call `producer()` on miss.

    If the cache table is unavailable or misconfigured, the producer runs
    directly. Caching is best-effort — it must never block tool calls.
    """
    table = _get_table()
    if table is None:
        return producer()

    key = _cache_key(tool, args)

    try:
        resp = table.get_item(Key={"cache_key": key}, ConsistentRead=False)
        item = resp.get("Item")
        if item:
            logger.info("cache HIT tool=%s key=%s", tool, key)
            _emit_metric(tool, hit=True)
            payload = item.get("payload")
            if payload is not None:
                try:
                    return json.loads(payload)
                except (TypeError, ValueError):
                    return payload
    except ClientError as err:
        logger.warning("cache get_item error for %s: %s", tool, err)

    logger.info("cache MISS tool=%s key=%s", tool, key)
    _emit_metric(tool, hit=False)
    result = producer()

    # Don't cache degraded sentinel strings — caching them would freeze
    # a transient failure into a permanent one.
    if isinstance(result, str) and result.startswith("[") and "unavailable" in result[:60]:
        return result
    # Also skip caching empty-list results for fallback-sensitive tools:
    # if FD returned empty and yfinance fallback also ran, we want the
    # router layer (not us) to decide when to retry.
    if isinstance(result, list) and not result:
        return result

    try:
        ttl_epoch = int(time.time()) + _ttl_seconds(tool)
        table.put_item(
            Item={
                "cache_key": key,
                "payload": json.dumps(result, default=str),
                "cached_at": int(time.time()),
                "ttl": ttl_epoch,
                "tool": tool,
            },
        )
    except ClientError as err:
        logger.warning("cache put_item error for %s: %s", tool, err)
    except (TypeError, ValueError) as err:
        logger.warning("cache serialization error for %s: %s", tool, err)

    return result

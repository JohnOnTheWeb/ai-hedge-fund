"""AgentCore Runtime FastAPI container.

Hosts the full LangGraph pipeline from `src.main.create_workflow`. One
`POST /invocations` request runs one ticker (or a small batch) end-to-end
and streams NDJSON events back to the caller (Fargate driver):

  {"type":"heartbeat","ts":..., "agent":"<current_node>"}       every 10s
  {"type":"result", "decisions":{...}, "report_key":"..."}      on completion

AgentCore terminates any invocation idle for 15 minutes; the heartbeat loop
resets that timer. This mirrors the TauricResearch runtime pattern.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta


def _default_start_date(trade_date: str) -> str:
    """trade_date - 90 days, ISO. Used when the caller omits start_date."""
    return (datetime.fromisoformat(trade_date) - timedelta(days=90)).strftime("%Y-%m-%d")
from typing import Any, Iterator

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from src.utils.otel import init_tracing

_HEARTBEAT_SECS = 10.0
_MAX_RUNTIME_SECS = 14 * 60  # leave safety margin vs AgentCore 15-min idle cap


app = FastAPI(title="aihedge-runtime", version="1.0.0")

# Tracing is a no-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
init_tracing(service_name=os.environ.get("OTEL_SERVICE_NAME", "aihedge-runtime"))


@app.get("/ping")
def ping() -> PlainTextResponse:
    return PlainTextResponse("OK")


@app.post("/invocations")
async def invocations(request: Request) -> StreamingResponse:
    payload = await request.json()
    return StreamingResponse(_run_and_stream(payload), media_type="application/x-ndjson")


def _run_and_stream(payload: dict[str, Any]) -> Iterator[bytes]:
    """Spawn the graph in a worker thread, emit heartbeats + a final result."""
    result_box: dict[str, Any] = {}
    current_node = {"name": "starting"}
    error_box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result_box["value"] = _run_graph(payload, current_node)
        except Exception as exc:  # noqa: BLE001
            error_box["value"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "trace": traceback.format_exc(limit=20),
            }

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    started = time.time()
    while t.is_alive():
        elapsed = time.time() - started
        if elapsed > _MAX_RUNTIME_SECS:
            error_box["value"] = {
                "type": "Timeout",
                "message": f"runtime exceeded {_MAX_RUNTIME_SECS}s",
            }
            break
        yield _nd({"type": "heartbeat", "ts": elapsed, "agent": current_node["name"]})
        t.join(timeout=_HEARTBEAT_SECS)

    if error_box:
        yield _nd({"type": "error", **error_box["value"]})
        return

    yield _nd({"type": "result", **result_box["value"]})


def _nd(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, default=str) + "\n").encode("utf-8")


def _run_graph(payload: dict[str, Any], current_node: dict[str, str]) -> dict[str, Any]:
    """Execute the LangGraph pipeline and write the per-ticker MD-Store report.

    Imports are deferred so unit tests can import this module without pulling
    in LangChain/Bedrock.
    """
    from langchain_core.messages import HumanMessage

    from src.main import create_workflow, parse_hedge_fund_response
    from src.utils.analysts import get_analyst_nodes

    tickers = payload["tickers"] if isinstance(payload.get("tickers"), list) else [payload["ticker"]]
    trade_date = payload.get("trade_date") or datetime.utcnow().strftime("%Y-%m-%d")
    run_id = payload["run_id"]
    show_reasoning = bool(payload.get("show_reasoning", False))

    selected = payload.get("selected_analysts") or list(get_analyst_nodes().keys())

    workflow = create_workflow(selected)
    graph = workflow.compile()

    portfolio = payload.get("portfolio") or _default_portfolio(tickers)

    # Hook: LangGraph lets us observe state transitions via `stream(..., stream_mode='updates')`.
    # We update `current_node` so heartbeats reflect progress.
    final_state: dict[str, Any] | None = None
    for event in graph.stream(
        {
            "messages": [HumanMessage(content="Make trading decisions based on the provided data.")],
            "data": {
                "tickers": tickers,
                "portfolio": portfolio,
                "start_date": payload.get("start_date") or _default_start_date(trade_date),
                "end_date": trade_date,
                "analyst_signals": {},
            },
            "metadata": {
                "show_reasoning": show_reasoning,
                "model_name": payload.get("model_name", "claude-sonnet-4-5-20250929-v1:0"),
                "model_provider": payload.get("model_provider", "Bedrock"),
                "run_id": run_id,
            },
        },
        stream_mode="updates",
    ):
        for node_name in event.keys():
            current_node["name"] = node_name
        final_state = event

    if final_state is None:
        raise RuntimeError("LangGraph produced no updates")

    # The last update under `portfolio_manager` carries the final message.
    last_state = list(final_state.values())[-1]
    last_message_content = last_state["messages"][-1].content if last_state.get("messages") else "{}"
    decisions = parse_hedge_fund_response(last_message_content) or {}
    analyst_signals = last_state.get("data", {}).get("analyst_signals", {})

    report_keys = _write_reports(run_id=run_id, trade_date=trade_date, decisions=decisions, analyst_signals=analyst_signals, tickers=tickers)

    return {
        "run_id": run_id,
        "trade_date": trade_date,
        "decisions": decisions,
        "analyst_signals": analyst_signals,
        "report_keys": report_keys,
    }


def _default_portfolio(tickers: list[str]) -> dict[str, Any]:
    return {
        "cash": 100_000.0,
        "margin_requirement": 0.0,
        "margin_used": 0.0,
        "positions": {
            t: {"long": 0, "short": 0, "long_cost_basis": 0.0, "short_cost_basis": 0.0, "short_margin_used": 0.0}
            for t in tickers
        },
        "realized_gains": {t: {"long": 0.0, "short": 0.0} for t in tickers},
    }


def _write_reports(
    *,
    run_id: str,
    trade_date: str,
    decisions: dict[str, Any],
    analyst_signals: dict[str, Any],
    tickers: list[str],
) -> dict[str, str]:
    """Write a markdown report per ticker to the MD-Store under AIHedge/..."""
    prefix = os.environ.get("AIHEDGE_MD_STORE_PREFIX", "AIHedge")
    md_store_token = _get_md_store_token()

    # MD-Store write uses the official MD-Store HTTP API (bearer token).
    # We call it via requests with SigV4 NOT required (external service).
    import requests  # local import to keep cold start lean

    base = os.environ.get("AIHEDGE_MD_STORE_URL", "https://mdstore.internal/files")
    headers = {"Authorization": f"Bearer {md_store_token}", "X-Agent-Id": "ai-hedge-fund"}

    report_keys: dict[str, str] = {}
    for ticker in tickers:
        body = _format_report(
            ticker=ticker,
            run_id=run_id,
            trade_date=trade_date,
            decision=decisions.get(ticker, {}),
            analyst_signals={k: v.get(ticker) for k, v in analyst_signals.items() if ticker in (v or {})},
        )
        key = f"{prefix}/{trade_date}/{run_id}/{ticker}.md"
        resp = requests.put(f"{base}/{key}", headers=headers, data=body.encode("utf-8"), timeout=15)
        resp.raise_for_status()
        report_keys[ticker] = key
    return report_keys


def _get_md_store_token() -> str:
    secret_id = os.environ.get("AIHEDGE_MD_STORE_SECRET_ID", "aihedge/md-store-token")
    sm = boto3.client("secretsmanager")
    value = sm.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        parsed = json.loads(value)
        return parsed.get("token") or value
    except (json.JSONDecodeError, TypeError):
        return value


def _format_report(*, ticker: str, run_id: str, trade_date: str, decision: dict, analyst_signals: dict) -> str:
    lines = [
        f"# AI-HedgeFund — {ticker}  ({trade_date})",
        f"run_id: `{run_id}`",
        "",
        "## Decision",
        "```json",
        json.dumps(decision, indent=2),
        "```",
        "",
        "## Analyst signals",
    ]
    for agent, sig in analyst_signals.items():
        lines.append(f"### {agent}")
        lines.append("```json")
        lines.append(json.dumps(sig, indent=2, default=str))
        lines.append("```")
    return "\n".join(lines) + "\n"

"""Tiny MCP-over-HTTP client for AgentCore Gateway.

Enabled by setting `AIHEDGE_GATEWAY_URL` (+ optional `AIHEDGE_GATEWAY_REGION`).
When set, `src.tools.api` and `src.agents.portfolio_manager` route their tool
calls through the Gateway (SigV4) instead of talking to vendors directly.

Two gotchas learned the hard way from the TauricResearch deployment:

1. **Tool namespacing**. AgentCore Gateway flattens all target tools into one
   namespace using a `<target>___<tool>` convention (three underscores). This
   client prepends the target automatically.
2. **MCP response unwrap**. Gateway responses nest the payload three levels
   deep:
       parsed["result"]["content"][0]["json"]   →   {"tool_name": ..., "result": ...}
   We unwrap the outer two layers plus the inner `result` envelope, so the
   caller gets the raw payload.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any


def gateway_enabled() -> bool:
    return bool(os.environ.get("AIHEDGE_GATEWAY_URL"))


def call_tool(target: str, tool: str, arguments: dict[str, Any]) -> Any:
    """Invoke `<target>___<tool>` via the Gateway. Returns the inner `result`.

    `target` is the Gateway target name ("data-tools", "memory-log"). `tool` is
    the bare tool name ("get_prices", "store_decision"). We combine them into
    `data-tools___get_prices` per the Gateway namespacing convention.

    Lazy-imports boto3 + botocore auth so local CLI users without these
    packages aren't affected.
    """
    # AgentCore Gateway speaks JSON-RPC 2.0 at the /mcp endpoint. The
    # GatewayUrl attribute already ends in "/mcp"; don't append further.
    endpoint = os.environ["AIHEDGE_GATEWAY_URL"].rstrip("/")
    if not endpoint.endswith("/mcp"):
        endpoint = f"{endpoint}/mcp"
    region = os.environ.get("AIHEDGE_GATEWAY_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    namespaced = f"{target}___{tool}"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": namespaced, "arguments": arguments},
    }).encode("utf-8")

    signed_headers, signed_body = _sigv4_sign("POST", endpoint, body, region)
    headers = {"Content-Type": "application/json", **signed_headers}

    import requests

    resp = requests.post(endpoint, headers=headers, data=signed_body, timeout=60)
    resp.raise_for_status()
    parsed = resp.json()
    return _unwrap(parsed)


def _unwrap(parsed: dict[str, Any]) -> Any:
    """Strip the three-layer MCP envelope to expose the tool's payload.

    Shape: parsed["result"]["content"][0]["json"] == {"tool_name": ..., "result": ...}
    We return the innermost "result". If the Gateway returns an error we raise
    with the error message so callers can surface it.
    """
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError(f"Gateway error: {parsed['error']}")

    try:
        outer_result = parsed["result"]
        content = outer_result.get("content") if isinstance(outer_result, dict) else None
        if not content:
            # Some MCP dialects return {"result": ...} with no content wrapper.
            if isinstance(outer_result, dict) and "result" in outer_result:
                return outer_result["result"]
            return outer_result
        inner = content[0].get("json") or json.loads(content[0]["text"]) if isinstance(content[0], dict) else content[0]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unexpected Gateway response shape: {parsed}") from exc

    if isinstance(inner, dict) and "error" in inner:
        raise RuntimeError(f"Tool error: {inner['error']}")
    if isinstance(inner, dict) and "result" in inner:
        return inner["result"]
    return inner


@lru_cache(maxsize=1)
def _session():
    import boto3

    return boto3.Session()


def _sigv4_sign(method: str, url: str, body: bytes, region: str) -> tuple[dict[str, str], bytes]:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    creds = _session().get_credentials().get_frozen_credentials()
    request = AWSRequest(method=method, url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "bedrock-agentcore", region).add_auth(request)
    return dict(request.headers.items()), body

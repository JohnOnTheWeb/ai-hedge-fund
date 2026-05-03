"""Tiny MCP-over-HTTP client for AgentCore Gateway.

Enabled by setting `AIHEDGE_GATEWAY_URL` + `AIHEDGE_GATEWAY_REGION`. When set,
`src.tools.api` routes data-tool calls through the Gateway (SigV4) instead of
hitting FinancialDatasets directly. Satisfies the "all tool calls through the
gateway" constraint without changing agent logic.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any


def gateway_enabled() -> bool:
    return bool(os.environ.get("AIHEDGE_GATEWAY_URL"))


def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Invoke an MCP tool via the Gateway. Returns the `result` payload.

    Lazy-imports boto3 + botocore auth so local CLI users without these
    packages are unaffected.
    """
    endpoint = os.environ["AIHEDGE_GATEWAY_URL"].rstrip("/")
    region = os.environ.get("AIHEDGE_GATEWAY_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    body = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    signed_headers, signed_body = _sigv4_sign("POST", f"{endpoint}/mcp/tools/call", body, region)
    headers.update(signed_headers)

    # Use requests (already a transitive dep) to keep the surface small.
    import requests

    resp = requests.post(f"{endpoint}/mcp/tools/call", headers=headers, data=signed_body, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"Gateway tool '{name}' failed: {payload['error']}")
    return payload.get("result")


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

"""API-key resolution.

Precedence (highest first):
  1. `state.metadata.request.api_keys[<name>]` — the web app path.
  2. `os.environ[<name>]` — local CLI / `.env`.
  3. AWS Secrets Manager — when `AIHEDGE_IN_CLUSTER=1` is set (the deployed
     AgentCore Runtime / Fargate / Lambda environments).

Callers pass the env-var name (e.g. `FINANCIAL_DATASETS_API_KEY`). The
Secrets-Manager step looks up a fixed secret-id for each known name:

  FINANCIAL_DATASETS_API_KEY -> aihedge/financial-datasets
  MD_STORE_TOKEN             -> aihedge/md-store-token
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

_SECRET_MAP = {
    "FINANCIAL_DATASETS_API_KEY": "aihedge/financial-datasets",
    "MD_STORE_TOKEN": "aihedge/md-store-token",
}


def get_api_key_from_state(state: dict | None, api_key_name: str) -> Optional[str]:
    """Return the requested API key, or None if unavailable."""
    if state and state.get("metadata", {}).get("request"):
        request = state["metadata"]["request"]
        if hasattr(request, "api_keys") and request.api_keys:
            value = request.api_keys.get(api_key_name)
            if value:
                return value

    env_value = os.environ.get(api_key_name)
    if env_value:
        return env_value

    if os.environ.get("AIHEDGE_IN_CLUSTER") == "1":
        secret_id = _SECRET_MAP.get(api_key_name)
        if secret_id:
            return _fetch_secret(secret_id)

    return None


@lru_cache(maxsize=8)
def _fetch_secret(secret_id: str) -> Optional[str]:
    try:
        import boto3  # deferred so CLI users without boto3 aren't affected
    except ImportError:
        return None

    sm = boto3.client("secretsmanager")
    value = sm.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            # Common shapes: {"api_key": "..."} or {"token": "..."}.
            for k in ("api_key", "token", "value"):
                if k in parsed:
                    return parsed[k]
        return value
    except (json.JSONDecodeError, TypeError):
        return value

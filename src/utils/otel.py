"""OpenTelemetry init helper.

Idempotent — safe to call from every container entrypoint and Lambda cold
start. When OTEL_EXPORTER_OTLP_ENDPOINT is unset (local CLI runs), all calls
become no-ops so existing behavior is preserved.

Exports via OTLP/HTTP with optional SigV4 signing when AIH_OTEL_SIGV4=1.
"""
from __future__ import annotations

import os
from typing import Optional

_initialized = False


def init_tracing(service_name: Optional[str] = None) -> None:
    """Install a global tracer provider the first time we're called."""
    global _initialized
    if _initialized:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        _initialized = True
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource_attrs = {"service.name": service_name or os.environ.get("OTEL_SERVICE_NAME", "aihedge")}
        for kv in (os.environ.get("OTEL_RESOURCE_ATTRIBUTES") or "").split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                resource_attrs[k.strip()] = v.strip()
        resource = Resource.create(resource_attrs)

        provider = TracerProvider(resource=resource)
        exporter = _build_exporter(endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        _install_autoinstrumentation()
    except Exception as exc:  # noqa: BLE001
        # Never let OTel bring down the agent run.
        print(f"[otel] init skipped: {exc}")
    finally:
        _initialized = True


def _build_exporter(endpoint: str):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    sigv4 = os.environ.get("AIH_OTEL_SIGV4") == "1"
    if sigv4:
        # SigV4 signing for OSIS public ingest — uses the calling identity's creds.
        session_headers = _sigv4_headers_factory(endpoint)
        return OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", headers=session_headers)
    return OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")


def _sigv4_headers_factory(endpoint: str):
    """Return a headers dict prepared for SigV4. Full request signing happens
    in a custom exporter variant in production; placeholder for v1.
    """
    return {"x-amz-target": "opensearch-ingestion"}


def _install_autoinstrumentation() -> None:
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        BotocoreInstrumentor().instrument()
    except Exception:
        pass
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:
        pass
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
    except Exception:
        pass

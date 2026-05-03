# Single ARM64 image for AI-HedgeFund on AWS.
# Consumers override CMD:
#   AgentCore Runtime:   ["uvicorn","deploy.app.runtime:app","--host","0.0.0.0","--port","8080"]
#   Fargate driver:      ["python","-m","deploy.app.task_runner"]
#   Lambda handlers:     ["deploy.app.lambdas.<name>.handler.handler"]   (awslambdaric)
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.11-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

# System dependencies needed by awslambdaric (compiles a small native bit).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential libcurl4-openssl-dev ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Poetry only used here for the lockfile; we install deps via pip for speed.
COPY pyproject.toml poetry.lock /app/
RUN pip install --upgrade pip \
    && pip install "poetry==1.8.3" \
    && poetry export -f requirements.txt --without-hashes --output /app/requirements.txt \
    && pip install -r /app/requirements.txt

# Additional deps needed inside AWS (Bedrock, OTel, FastAPI, awslambdaric).
RUN pip install \
        "langchain-aws>=0.2.6" \
        "boto3>=1.34" \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "awslambdaric>=2.1" \
        "opentelemetry-api>=1.27" \
        "opentelemetry-sdk>=1.27" \
        "opentelemetry-exporter-otlp-proto-http>=1.27" \
        "opentelemetry-instrumentation-botocore>=0.48b0" \
        "opentelemetry-instrumentation-httpx>=0.48b0" \
        "opentelemetry-instrumentation-requests>=0.48b0" \
        "botocore-sigv4-requests>=0.1.2 ; python_version >= '3.11'" \
        "requests-aws4auth>=1.3.1"

# Project source — src/ plus the deploy/app/ wrappers.
COPY src/ /app/src/
COPY deploy/ /app/deploy/

# Defensive: buildx has been observed stripping exec bits off COPYed binaries
# in multi-stage builds, causing every Lambda invocation to fail with
# Runtime.InvalidEntrypoint. This is cheap insurance for any future multi-stage
# refactor of this Dockerfile.
RUN chmod a+rx /usr/local/bin/python* 2>/dev/null || true

# AgentCore Runtime expects port 8080; default CMD runs the FastAPI app.
EXPOSE 8080

# ENTRYPOINT is intentionally empty. Each consumer sets its own:
#   AgentCore Runtime: default CMD below (uvicorn)
#   Fargate driver:    command=["python","-m","deploy.app.task_runner"]
#   Lambda handlers:   entry_point=["python","-m","awslambdaric"] + cmd=["<module>.handler"]
# Previously had `ENTRYPOINT ["python","-m"]` which prepended to every CMD;
# that works for the uvicorn default but breaks every Lambda/Fargate consumer.
ENTRYPOINT []
CMD ["python", "-m", "uvicorn", "deploy.app.runtime:app", "--host", "0.0.0.0", "--port", "8080"]

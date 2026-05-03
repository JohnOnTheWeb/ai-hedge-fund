# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Layout

This is a single Poetry project rooted at the repo top level. Three Python packages are declared in `pyproject.toml`:

- `src/` — the production CLI hedge fund and backtester (LangGraph-based multi-agent system).
- `app/` — full-stack web application: FastAPI backend (`app/backend/`) + React/Vite frontend (`app/frontend/`).
- `v2/` — **work in progress**, not wired into the rest of the app. A ground-up quantitative rebuild (signals/features/portfolio/risk/validation) meant to eventually replace the personality-based agents in `src/`. Treat it as an independent module until the README says otherwise.

A `.env` at the repo root supplies API keys for all entry points (copy from `.env.example`). At least one LLM provider key is required (OpenAI, Anthropic, Groq, DeepSeek, Google, xAI, Moonshot/Kimi, GigaChat, OpenRouter, or Azure OpenAI) plus `FINANCIAL_DATASETS_API_KEY` for any ticker outside the free set (AAPL, GOOGL, MSFT, NVDA, TSLA).

## Common Commands

Install (from repo root):
```bash
poetry install
```

Run the CLI hedge fund:
```bash
poetry run python src/main.py --ticker AAPL,MSFT,NVDA
poetry run python src/main.py --ticker AAPL,MSFT,NVDA --ollama
poetry run python src/main.py --ticker AAPL,MSFT,NVDA --start-date 2024-01-01 --end-date 2024-03-01
```

Run the backtester (two equivalent entry points):
```bash
poetry run python src/backtester.py --ticker AAPL,MSFT,NVDA
poetry run backtester --ticker AAPL,MSFT,NVDA   # console script from pyproject.toml
```

Run the web app backend (from `app/backend/`):
```bash
poetry run uvicorn app.backend.main:app --reload
```
API docs at http://localhost:8000/docs. The backend enables CORS only for `http://localhost:5173`.

Run the frontend (from `app/frontend/`):
```bash
npm install
npm run dev      # vite dev server on :5173
npm run build    # tsc && vite build
npm run lint     # eslint, --max-warnings 0
```

Run tests:
```bash
poetry run pytest                                   # all tests
poetry run pytest tests/test_cache.py               # one file
poetry run pytest tests/backtesting/test_metrics.py::test_name   # one test
poetry run pytest tests/backtesting/integration/    # integration suite
```

Format / lint (Python):
```bash
poetry run black .         # line-length is 420 (intentional — see pyproject)
poetry run isort .         # profile=black, alphabetical within sections
poetry run flake8 .
```

Docker (from `docker/`):
```bash
./run.sh build
./run.sh --ticker AAPL,MSFT,NVDA main
./run.sh --ticker AAPL,MSFT,NVDA backtest
./run.sh --ticker AAPL,MSFT,NVDA --ollama --ollama-base-url http://localhost:11434 main
```
Set `OLLAMA_BASE_URL` to reuse an external Ollama; otherwise add the `embedded-ollama` profile to start the bundled container.

Alembic migrations (from `app/backend/`):
```bash
poetry run alembic upgrade head
poetry run alembic revision --autogenerate -m "message"
```

## Architecture: CLI Hedge Fund (`src/`)

The run is a LangGraph `StateGraph` assembled in `src/main.py::create_workflow`:

```
start_node ──► [selected analyst agents in parallel] ──► risk_management_agent ──► portfolio_manager ──► END
```

- `AgentState` (`src/graph/state.py`) is a `TypedDict` with three keys: `messages` (appended via `operator.add`), `data` (dict-merged — holds `tickers`, `portfolio`, `start_date`, `end_date`, `analyst_signals`), and `metadata` (dict-merged — holds `show_reasoning`, `model_name`, `model_provider`). Reducers matter: every agent should return partial dicts to merge, not overwrite.
- Analyst agents live in `src/agents/` (one file per persona + the method-based `fundamentals`, `technicals`, `valuation`, `sentiment`, `news_sentiment`, `growth_agent`). Each writes its signal into `data.analyst_signals[agent_id][ticker]`.
- **`src/utils/analysts.py` is the single source of truth for agents.** `ANALYST_CONFIG` maps each analyst key to its display name, description, `agent_func`, and ordering. `ANALYST_ORDER` and `get_analyst_nodes()` are derived from it. When adding an agent, register it here — `main.py`, the CLI selector, and the web backend all read from this config.
- `risk_management_agent` (`src/agents/risk_manager.py`) consumes all analyst signals and sets per-ticker position limits.
- `portfolio_manager` (`src/agents/portfolio_manager.py`) is the terminal node — it emits the final JSON trading decisions that `run_hedge_fund` parses from `final_state["messages"][-1].content`.
- LLM calls go through `src/utils/llm.py::call_llm`, which uses the provider/model chosen via `src/llm/models.py` (`ModelProvider` enum, `LLMModel`, configs in `src/llm/api_models.json` and `src/llm/ollama_models.json`). API keys resolve via `src/utils/api_key.py::get_api_key_from_state`, which reads from `state.metadata["request"]` first (web app path) then env vars (CLI path) — both paths must keep working.
- Market/fundamentals data: `src/tools/api.py` wraps the Financial Datasets API with retry/backoff for 429s and an in-memory cache (`src/data/cache.py`). Pydantic models in `src/data/models.py`.
- CLI argument parsing is shared between `main.py` and `backtester.py` via `src/cli/input.py::parse_cli_inputs`.
- Backtesting (`src/backtesting/`) wraps `run_hedge_fund` in a date-loop: `engine.py` orchestrates, `controller.py` invokes agents, `trader.py` executes simulated fills, `portfolio.py` tracks state, `metrics.py` computes Sharpe/drawdown/etc., `valuation.py` marks positions to market, `benchmarks.py` compares against buy-and-hold.

## Architecture: Web App (`app/`)

- **Backend** (`app/backend/`, FastAPI, entry `app.backend.main:app`):
  - `routes/` — `hedge_fund.py` (runs the workflow, streams progress), `flows.py` / `flow_runs.py` (persisted flow graphs), `api_keys.py`, `language_models.py`, `ollama.py`, `health.py`, `storage.py`.
  - `services/` — `agent_service.py` bridges to `src/` agents, `graph.py` builds dynamic workflows from frontend graph definitions, `ollama_service.py` manages the local Ollama lifecycle (checked on app startup), `backtest_service.py` runs backtests async, `api_key_service.py` persists keys to the DB.
  - `database/` — SQLAlchemy + Alembic. `connection.py` creates the engine; `models.py` holds ORM models. `Base.metadata.create_all` runs at startup (safe to re-run).
  - `models/schemas.py` — Pydantic request/response schemas; `models/events.py` — SSE event types for streaming runs.
- **Frontend** (`app/frontend/`, React 18 + Vite + TypeScript + Tailwind + shadcn/ui + `@xyflow/react` for the node-based flow editor). Path alias `@/` → `./src/`. Talks to backend on `:8000`; CORS is pinned to `:5173`, so use that port for dev.

## Architecture: v2 (`v2/`) — WIP

Quantitative pipeline: `data → signals → features → validation → backtesting → portfolio → risk → pipeline(execution)`. Signals implement a `BaseSignal` ABC (`v2/signals/base.py`) with output in `[-1, +1]`. Core data contracts in `v2/models.py` (`SignalResult`, `QuantSignals`, `PortfolioTarget`, `TradeOrder`, `ExecutionResult`). Most submodules are scaffolded but empty — check each `__init__.py` before assuming a feature exists. v2 is **not** imported from `src/` or `app/`.

## Conventions

- Python 3.11 (`^3.11`). Poetry manages deps for all three packages; there is no separate `requirements.txt`.
- `black` line-length is **420** on purpose (the project prefers long single-line prompt strings in agent files). Don't "fix" it.
- `isort` is configured with `force_alphabetical_sort_within_sections = true`.
- Agents return partial state dicts that rely on the `AgentState` reducers — never return a whole new state object that drops keys from upstream analysts.
- When adding an LLM provider, update `ModelProvider` in `src/llm/models.py`, add a factory branch in `get_model()`, register defaults in `src/llm/api_models.json`, and ensure `src/utils/api_key.py` can source the key.

## AWS deployment (`infra/`, `deploy/`, `scripts/`, `Dockerfile.app`, `buildspec.yml`, `statemachine/`)

Two-stack CDK-Python app:
- `AIHedge-Platform-Stack` — VPC, ECR, CodePipeline/CodeBuild, OpenSearch, AMP, OSIS, Gateway shell, KMS, Secrets, Config rule. Long-lived.
- `AIHedge-App-Stack` — AgentCore Runtime + Gateway Lambda targets, Fargate, Step Functions, APIGW HTTP API (IAM auth), EventBridge daily cron, DynamoDB memory log, SNS. Push-button destroy.

Tag enforcement is 3-layered: CDK aspect (synth-time), IAM permission boundary (request-time), AWS Config `required-tags` rule (runtime drift). Mandatory tag: `UsedBy=AIHedge`.

Non-obvious operational facts:

- **CodeBuild clones from GitHub, not your local tree.** Source is `JohnOnTheWeb/ai-hedge-fund@main`. `git push` before triggering the pipeline — local edits are invisible otherwise.
- **Two-phase deploy.** First `cdk deploy AIHedge-Platform-Stack` (ECR, CodePipeline, Gateway shell); trigger CodePipeline; then `cdk deploy AIHedge-App-Stack -c agentCoreEnabled=true` creates the Runtime + Gateway targets + Lambdas + Step Fn. Deploying the app stack without `agentCoreEnabled=true` produces an empty stack by design — the container-image Lambdas can't synth against the placeholder digest.
- **Manual console steps between phases.** (1) After Phase 1: accept the CodeStar `aihedge-github` connection in the AWS Developer Tools console — it starts in `PENDING` and blocks CodePipeline until OAuth completes. (2) Before Phase 3: request Bedrock model access for the three Claude model IDs (Opus 4.7, Sonnet 4.5, Haiku 4.5) in the Bedrock console. Both prompts are in `scripts/bootstrap.sh`.
- **`cdk synth` needs `cdk.context.json`.** The app stack reads image URI/tag/digest from SSM via `value_from_lookup`, which writes the resolved values to `infra/cdk.context.json` on first run. If you delete that file or synth in a fresh checkout, the first synth produces placeholders and subsequent ones resolve. Don't hand-edit it — regenerate with `cdk synth`.
- **After every image push**, the buildspec's post_build calls `bedrock-agentcore-control update-agent-runtime` to force the Runtime to re-pull. Lambdas pin to image digest (via SSM `/aihedge/image/digest`) so CFN detects real deltas on redeploy.
- **Bedrock auth is IAM task-role**, not bearer token. `AWS_BEARER_TOKEN_BEDROCK` is not used anywhere.
- **Secrets must be seeded manually.** `scripts/seed_secrets.py` reads `FINANCIAL_DATASETS_API_KEY` + `MD_STORE_TOKEN` from your local environment and writes to Secrets Manager. The MD-Store token in particular has no AWS-managed copy — if you run `cdk destroy --all` it regenerates to garbage and MD-Store returns 401 until you re-seed from the external source.
- **Retained-resource orphans block re-deploy.** `cdk destroy` leaves Secrets (by design) and may leave OpenSearch/AMP/OSIS/ECR (via `RETAIN` unless `-c platformDestroy=true`). `scripts/purge_orphans.sh` cleans up anything tagged `UsedBy=AIHedge`; run with `--execute` to actually delete.
- **Gateway tool namespacing is `<target>___<tool>` (triple underscore).** `src/tools/mcp_client.py` handles the prefix automatically; Lambda handlers strip it on receive.
- **Step Functions can SUCCEED while tickers failed.** `aggregate` Lambda sets `status` to `SUCCESS` / `PARTIAL_FAILURE` / `FAILURE` and lists `tickers_failed` in the summary JSON and SNS email.
- **Map concurrency starts at 5** (`statemachine/aihedge_run.asl.json`). TauricResearch ran at 15 once Fargate quotas were confirmed; bump after first prod run.
- **Local CLI is unaffected.** `poetry run python src/main.py --ticker AAPL` works unchanged — AWS hooks are gated on `AIHEDGE_IN_CLUSTER=1` or `AIHEDGE_GATEWAY_URL`.

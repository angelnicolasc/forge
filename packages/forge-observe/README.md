<div align="center">

# forge-observe

**Observability and FinOps for Forge.**

OpenTelemetry tracing, event-driven cost tracking, real-time metrics, console output, and a FastAPI backend for monitoring multi-agent systems in production.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-orange.svg)](../../LICENSE)
[![Package](https://img.shields.io/badge/package-forge--observe-111827.svg)](https://github.com/angelnicolasc/forge/tree/main/packages/forge-observe)

</div>

---

## Why `forge-observe`

Agent systems are hard to operate when you cannot answer simple questions fast:

- Which agent is spending the money?
- Which model is driving cost?
- Where are the failures happening?
- Are we seeing real telemetry or just framework-level guesses?
- Can we expose live run data to dashboards and internal tooling?

`forge-observe` is the package in Forge that turns runtime signals into something you can inspect, aggregate, export, and act on. It sits on top of `forge-core` and treats events as the source of truth for costs, spans, and operational visibility.

## What ships in this package

- `ForgeLLMInterceptor` for canonical LLM call instrumentation
- `ForgeTracer` for OpenTelemetry wiring and event-to-span mirroring
- `MetricsCollector` for aggregated session, model, and agent metrics
- `DefaultCostModel` for pricing-aware FinOps calculations
- Rich console output helpers for CLI-facing run presentation
- FastAPI REST + SSE backend for recent runs, metrics, evolution entries, and memory endpoints
- SDK instrumentation helpers for direct Anthropic, OpenAI, and Google GenAI usage
- Label sanitization to control metric-cardinality explosions

## Installation

Inside the Forge workspace:

```bash
uv sync
```

Standalone:

```bash
pip install forge-observe
```

Requirements:

- Python 3.11+
- `forge-core==0.1.0`
- `opentelemetry-api`
- `opentelemetry-sdk`
- `opentelemetry-exporter-otlp`
- `fastapi`
- `uvicorn`
- `rich`

## Quickstart

The most important runtime object here is the interceptor. It converts raw model-call telemetry into populated `RunEvent` objects with costs and tracing metadata attached.

```python
from forge_core.events import EventBus
from forge_observe.interceptor import ForgeLLMInterceptor

bus = EventBus()
interceptor = ForgeLLMInterceptor(bus=bus)
```

In a full Forge stack, `MetaOrchestrator` owns one interceptor and hands it to adapters so all frameworks feed the same event stream.

## The core model

`forge-observe` is built around one idea:

```text
raw LLM/tool activity -> RunEvent -> metrics / traces / API / console
```

Instead of each adapter inventing its own accounting path, this package centralizes observability around Forge's shared event model.

That gives you:

- one place to compute cost
- one place to publish telemetry
- one place to wire tracing
- one place to expose aggregated state

## `ForgeLLMInterceptor`

`ForgeLLMInterceptor` is the canonical implementation of the `LLMCallInterceptor` protocol from `forge-core`.

Responsibilities:

- open and close OpenTelemetry spans around model calls
- compute cost using `DefaultCostModel`
- publish `LLM_CALL` and `ERROR` events to the `EventBus`
- preserve per-call metadata such as model, agent, tool, latency, and token counts
- protect degraded providers through a circuit breaker

This is the linchpin that keeps FinOps honest. If instrumentation bypasses the interceptor, costs stop being trustworthy.

### Breaker behavior

The interceptor can wire an LLM circuit breaker automatically. When repeated failures accumulate, new starts are short-circuited and an `ERROR` event is emitted instead of letting dead dependencies flood the runtime with timeouts.

Environment flag:

- `FORGE_LLM_CIRCUIT_BREAKER=0`
  - disables the default breaker wiring

## `ForgeTracer`

`ForgeTracer` configures OpenTelemetry export and can subscribe directly to the Forge event bus.

It serves two roles:

- ensure a tracer provider exists and exporters are configured
- mirror bus events into spans so traces are visible even when the interceptor is not the only event source

Convenience helper:

```python
from forge_observe.tracer import attach_tracer_to_orchestrator

tracer, handle = attach_tracer_to_orchestrator(orchestrator)
```

After attachment, runs executed by the orchestrator automatically produce OTel spans.

## `MetricsCollector`

`MetricsCollector` aggregates run outcomes into session-level operational metrics.

Tracked dimensions include:

- total runs
- successful vs failed runs
- total cost
- total input and output tokens
- total duration
- per-model cost
- per-agent LLM calls, cost, latency, tool calls, and errors

It also includes a cost-savings estimator that compares current spending against cheaper model substitutions.

Example:

```python
from forge_observe.metrics import MetricsCollector

collector = MetricsCollector()
collector.record_run(result)

print(collector.session.total_cost)
print(collector.summary_dict())
```

## Pricing and FinOps

`DefaultCostModel` provides pricing-aware cost calculation for a registry of known models.

Notable behavior:

- exact and prefix model matching
- custom pricing overrides
- warning-once behavior for unknown model names
- deterministic zero-cost fallback for unknown models instead of silent guessing

This matters because downstream evaluation and optimization logic depends on cost accuracy.

## Rich console output

The console exporter is designed for high-signal CLI output and demos.

It includes helpers such as:

- `print_forge_banner()`
- `print_run_result()`
- `print_evolution_proposal()`
- `live_progress()`

These render:

- run status
- cost breakdown
- topology tree
- mutations and proposals
- progress feedback for long operations

## API and live backend

`forge-observe` ships a FastAPI application that exposes recent runtime state over REST and SSE.

Key endpoints:

- `GET /api/v1/health`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/events`
- `POST /api/v1/runs`
- `GET /api/v1/evolution`
- `POST /api/v1/evolution`
- `GET /api/v1/evolution/stats`
- `GET /api/v1/metrics`
- `GET /api/v1/stream`
- `GET /api/v1/memory/query`
- `GET /api/v1/memory/graph`

The API keeps an in-memory store for development and lightweight deployments, and it can be fronted by a standalone dashboard or internal integration.

### SSE support

The `/api/v1/stream` endpoint exposes a live event stream over Server-Sent Events with:

- bounded per-client queues
- subscriber-cap enforcement
- heartbeat pings
- cleanup on disconnect

This makes it suitable for lightweight real-time dashboards without needing a separate event broker.

### CORS

Browser clients on different origins can be enabled through:

- `FORGE_API_ALLOWED_ORIGINS`

Example:

```bash
FORGE_API_ALLOWED_ORIGINS="http://localhost:5173,https://example.com"
```

## Dashboard backend

`DashboardBackend` is a convenience wrapper that runs the API server in-process and can attach itself to a `MetaOrchestrator`.

What it does:

- starts a background `uvicorn` server
- exposes the server URL and docs URL
- patches orchestrator `run()` calls so completed runs are posted into the API backend

This is useful for local demos and simple operator setups.

## Direct SDK instrumentation

Not every user runs through a framework adapter. Some flows call SDKs directly.

`forge_observe.llm_clients.instrument_llm_clients(...)` patches imported SDK entrypoints for the duration of a context manager so direct model calls still emit Forge telemetry.

Supported SDK families in the current implementation:

- Anthropic
- OpenAI
- Google Generative AI

Example:

```python
from forge_observe.llm_clients import instrument_llm_clients

with instrument_llm_clients(interceptor, agent_id="writer"):
    ...
```

Design intent:

- only patch SDKs already imported by the user
- revert patches cleanly on exit
- preserve telemetry for sync and async call paths
- avoid breaking user execution if instrumentation fails

## Label sanitization

Metrics backends are vulnerable to high-cardinality labels. `LabelSanitizer` exists to keep observability from turning into a cardinality bomb.

It enforces:

- label allowlists
- per-key unique-value caps
- deterministic hashing for over-cap values
- normalization of empty and missing values

This is especially important for fields like `agent_id`, where unbounded variation can destroy Prometheus-style backends.

## Package boundaries

`forge-observe` focuses on observability and runtime reporting.

Related packages:

- `forge-core`
  - run model, event bus, protocols, orchestrator, guards
- `forge-memory`
  - memory storage and retrieval
- `forge-adapters`
  - framework integrations
- `forge-cli`
  - CLI entrypoints and user-facing commands

That separation keeps `forge-observe` reusable in monitoring-oriented deployments.

## Public API at a glance

Top-level exports from `forge_observe`:

- `DefaultCostModel`
- `ForgeTracer`
- `MetricsCollector`

Primary modules:

- `forge_observe.interceptor`
- `forge_observe.tracer`
- `forge_observe.metrics`
- `forge_observe.cost_model`
- `forge_observe.llm_clients`
- `forge_observe.labels`
- `forge_observe.dashboard_backend`
- `forge_observe.exporters.api`
- `forge_observe.exporters.console`
- `forge_observe.exporters.otlp`

## Recommended usage patterns

### Attach tracing to an orchestrator

```python
from forge_observe.tracer import attach_tracer_to_orchestrator

tracer, subscription = attach_tracer_to_orchestrator(
    orchestrator,
    enable_console=True,
    otlp_endpoint=None,
)
```

### Aggregate metrics from completed runs

```python
from forge_observe.metrics import MetricsCollector

collector = MetricsCollector()
collector.record_run(result)
summary = collector.summary_dict()
```

### Start the dashboard backend locally

```python
from forge_observe.dashboard_backend import DashboardBackend

backend = DashboardBackend(host="127.0.0.1", port=8787)
backend.start_background()
backend.attach_orchestrator(orchestrator)
```

## Testing

From the repository root:

```bash
pytest packages/forge-observe/tests -q
```

The test suite covers:

- REST and SSE API behavior
- memory API integration points
- event fan-out for live subscribers
- metrics aggregation from real Forge test harness runs
- interceptor circuit-breaker behavior
- label-sanitization guarantees
- SDK monkey-patch instrumentation and restoration

## License

Apache-2.0

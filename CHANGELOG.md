# Changelog

All notable changes to Forge are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

---

## [0.1.0] — 2026-04-18

First public release. Backend + CLI only; a web UI on top of the observe REST + SSE API is planned for v0.2.0.

### Added

**`forge-core`**
- `MetaOrchestrator` with run lifecycle, EventBus, context propagation via `contextvars`
- `LLMCallInterceptor` protocol — framework-agnostic token / cost capture
- Self-evolution FSM: `IDLE → OBSERVING → PROPOSING → APPLYING → EVALUATING → COMMITTING | ROLLING_BACK`
- Real mutators: `PromptRewriteMutator` (LLM structured output), `ParameterTuneMutator`
  (applies diff to `RunConfig`), `AgentCullMutator` (snapshot-backed rollback)
- `TopologySnapshot` for atomic rollback
- `EvalSuite` with deterministic + synthetic evaluation
- `EvolutionJournal` with append-only provenance
- `CostBudgetGuard` + timeout enforcement via `asyncio.wait_for`
- `CircuitBreaker` (closed → open → half-open) — ships with the class; wired in ζ.5
- `ForgeTestHarness` + `MockLLMProvider` for deterministic end-to-end tests

**`forge-memory`**
- `HybridMemory` fusing vector (ChromaDB), graph (NetworkX), and symbolic layers
- `MemoryContextInjector` — memory actually flows into the next run's envelope
- Vector-clock versioning with conflict detection
- `IngestionPipeline` with regex + optional LLM-backed entity extraction
- NetworkX backend persists via SQLite (WAL) with atomic writes

**`forge-adapters`**
- `LangGraphAdapter` — injects `ForgeLangChainCallback` into `config`
- `CrewAIAdapter` — instruments `Agent.execute_task` / `BaseTool._run`
- `AutoGenAdapter` — subscribes to `TRACE_LOGGER_NAME` structured records
- `GenericCallableAdapter` — monkey-patches Anthropic / OpenAI / Gemini SDKs in-scope
- Auto-detection via import inspection

**`forge-observe`**
- `ForgeLLMInterceptor` publishes `RunEvent`s to the bus with real tokens + cost
- `CostModel` with April-2026 pricing table (Claude 4, GPT-4o, Gemini 1.5)
- `MetricsCollector` with `LabelSanitizer` (allowlist + hashed buckets) for
  cardinality-safe OTel export
- FastAPI observe backend: `/runs`, `/evolution`, `/memory`, SSE `/runs/{id}/stream`
- OTLP exporter for Grafana / Datadog
- Rich console exporter (the terminal demo)

**`forge-cli`**
- `forge wrap`, `run`, `evolve`, `observe`, `memory` commands
- `forge doctor` (η.D/ζ.13) — diagnose Python version, optional adapter extras,
  default memory backends, and optional network egress (`--network`)
- `forge evolve run` / `status` / `resume` (η.D/ζ.15) — read the evolution
  journal and re-arm the circuit breaker from the CLI
- Rich live display: cost ticker, topology table, evolution journal

**Hardening (η.D)**
- SSE endpoint now caps live subscribers at 50 (HTTP 503 beyond that) and the
  `/api/v1/health` response reports `live_subscribers` / `max_live_subscribers`
  (ζ.12)
- Runtime dependencies pinned with upper bounds: `pydantic<3.0`,
  `pydantic-settings<3.0`, `structlog<26.0`, `opentelemetry-sdk<2.0`,
  `fastapi<0.120` (ζ.14)

**`forge-os`**
- New meta-package. `pip install forge-os` installs the five runtime packages
  together. Extras: `[langgraph]`, `[crewai]`, `[autogen]`, `[all]`

**Infrastructure**
- CI: ruff + mypy + pytest on Python 3.11 / 3.12 / 3.13
- Release workflow publishes all six packages to PyPI on tag
- Apache-2.0 license, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`

### Feature flags (default off unless noted)

- `FORGE_ENABLE_EVOLUTION_AUTO` — auto-trigger evolution after N runs
- `FORGE_ENABLE_MEMORY_INJECTION` — inject memory into run envelopes
- `FORGE_ENABLE_LLM_JUDGE` — quality scoring via LLM
- `FORGE_ENABLE_LLM_ENTITY_EXTRACTION` — LLM-backed entity extraction (fallback: regex)
- `FORGE_ENABLE_GENERIC_INSTRUMENTATION` — monkey-patch LLM SDKs
- `FORGE_LLM_CIRCUIT_BREAKER` — default **on**; breaker around the LLM interceptor
- `FORGE_COST_HARD_FAIL` — raise `BudgetExceeded` vs. warn

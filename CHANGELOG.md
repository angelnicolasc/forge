# Changelog

All notable changes to Forge are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

Forge Check development notes are recorded in the [architecture decisions](docs/decisions/index.md).

---

## Forge Check 0.4.0 — 2026-11-20

The standalone distribution is `forge-os-check`. Install it in a dedicated Python 3.11+ environment because the historical harness also provides the `forge` executable.

- `forge check` compares two recorded revisions against a frozen set of eligible cases. The report includes per-case reasons, evidence coverage and a CI exit code.
- The single-host runtime records governed calls in SQLite and uses independent observations to evaluate conditional inventory rules before promotion. The optional Code Mode extra supports explicitly enabled read bindings on Linux x86_64.
- Read-cache expiry in this release starts when the host settles the result. Set the TTL to zero for operations that require a fresh lookup on every new request.

[Published wheel](https://pypi.org/project/forge-os-check/0.4.0/#files) · [Release source](https://github.com/angelnicolasc/forge/commit/6fc58c30ca50d3d9bda1e14e0764e77bb123df56) · [Publication receipt](https://github.com/angelnicolasc/forge/actions/runs/44823397253)

## Historical harness

## [0.2.0] — 2026-05-13

This release adds `forge-spec` for checking recorded runs against specifications and
`forge-review` for policy-gated lifecycle hooks. It extends event coverage and type
contracts across the harness packages.

### Added

**`forge-spec` — Run specifications** (`pip install 'forge-os[spec]'`)

- `SpecVerifier` supports ASSERTION, REGEX and CONTAINS criteria, with per-criterion weights.
  ASSERTION evaluates Python expressions from trusted specifications with restricted built-ins.
- `SpecAttester` produces an unsigned verification report containing the spec's SHA-256
  digest and counts of passed, failed and skipped criteria.
- `SpecConstraintGuard` intercepts evolution mutations before application and raises
  `ConstraintViolationError` when a mutation violates a declared spec constraint
- YAML-based spec authoring with round-trip serialization and deterministic `spec_hash`
  generation from canonical JSON
- `RunEventKind.SPEC_VERIFIED` published to the EventBus after each verification pipeline
- CLI: `forge spec init`, `forge spec show`, `forge spec verify`, `forge spec attest`

**`forge-review` — Review Agents and Policy Gates** (`pip install 'forge-os[review]'`)

- P0–P3 severity taxonomy: P0 blocks unconditionally (security and data-loss findings);
  P1 blocks by default (quality gates); P2–P3 are observational
- `CascadingReviewer` runs heuristic reviewers in parallel first; LLM-backed reviewers are
  skipped when blocking findings are already present, preserving token budget
- `PolicyGate` with configurable `block_on` severity list and minimum confidence threshold
- `ReviewRunner` with four lifecycle hooks: `on_plan`, `pre_apply`, `pre_stop`, `pre_merge`
- Bundled reviewers: `ErrorRateReviewer`, `CostCeilingReviewer`, `SpecConstraintReviewer`,
  `StatusReviewer`
- `SpecConstraintReviewer` integrates forge-spec constraint checking at policy evaluation time
  without introducing a package dependency cycle (lazy import)
- `run_parallel` invariant: an exception in any individual reviewer produces a P2 finding and
  continues; no reviewer can silence another
- `RunEventKind.REVIEW_COMPLETE` published to the EventBus after each gate evaluation
- CLI: `forge review run`, `forge review show`

**`forge-core` — Streaming, Typing, and Concurrency**

- Thinking tokens and cached input tokens tracked in `RunEvent`, `CostModel`, and
  `LLMCallInterceptor` protocol (4-tuple pricing model per model family)
- `RunEventKind.LLM_TOKEN` for per-token streaming events, enabling real-time SSE fan-out of
  live token output from the EventBus
- `RunEventKind.SPEC_VERIFIED` and `RunEventKind.REVIEW_COMPLETE` added to the event taxonomy
- `TaskEnvelopeContext` TypedDict for structured, type-safe context annotation on
  `TaskEnvelope.context`
- 5 asyncio concurrency tests verifying at-most-once trigger semantics for the evolution FSM
- 23 unit tests for `DefaultCostModel` covering thinking tokens, cached-token pricing,
  override table loading, local cache, and TTL expiry

**`forge-observe` — Cost Model and Configurability**

- Three-tier pricing resolution: `FORGE_PRICING_TABLE_PATH` environment variable →
  `~/.forge/pricing.json` local cache with 7-day TTL → bundled pricing table. Unknown models
  continue to warn loudly rather than silently billing $0
- `FORGE_OBSERVE_MAX_SSE_SUBSCRIBERS` is now runtime-configurable via environment variable;
  the HTTP 503 enforcement cap remains in place

**`forge-memory`**

- `GraphBackend` defines the interface for graph storage. NetworkX is the bundled implementation.
- `HybridMemory` accepts a custom backend through its `graph_backend` parameter.

**`forge-adapters`**

- `ADAPTER_DEGRADED` event published to the EventBus when `GenericCallableAdapter` SDK
  instrumentation fails; replaces silent log-only behavior

**`forge-cli`**

- `forge doctor update-pricing` — refreshes the local pricing cache from the upstream table
  on demand without waiting for the 7-day TTL to expire
- `forge mcp serve` — exposes Forge memory, evolution state, and run history as MCP tools
  via `ForgeMCPServer`, making the harness itself accessible to any MCP-capable client

---

## [0.1.2] — 2026-05-04

Introduces two new orchestration packages: `forge-mcp` for governed Model Context Protocol
tool dispatch, and `forge-skills` for intent-driven skill invocation. Extends the core event
model with `TOOL_CALL` and `SKILL_CALL` telemetry.

### Added

**`forge-mcp` — MCP tool calls** (`pip install 'forge-os[mcp]'`)

- Tool calls pass through a router, caller policy, budget check, result cache and circuit
  breaker. The server redacts recognized credential patterns and records dispatch events.
- Deny-by-default tool policy with per-caller allowlist; `ToolDeniedError` raised on explicit
  policy violation, distinguished from upstream errors encapsulated in `ToolResult.error`
- SHA-256 keyed TTL result cache; cache key normalized by sorted argument keys for
  deterministic hits regardless of argument ordering
- Thread-safe per-tool call-count budget accounting with `BudgetExceededError`
- Secret redaction covering six credential patterns: API keys, JWTs, GitHub PATs, GitLab
  tokens, Bearer tokens, and OpenAI keys
- `ToolRouter` bidirectional index for O(1) tool resolution; last-registration-wins for
  overlapping tool names across multiple servers
- YAML server configuration with `${VAR}` environment variable expansion and full round-trip
  serialization
- `MCPClientProtocol` structural protocol with `FakeMCPClient` test double for deterministic
  upstream simulation in CI
- CLI: `forge mcp list-tools`, `forge mcp call`, `forge mcp status`

**`forge-skills` — Skills Runtime** (`pip install 'forge-os[skills]'`)

- SKILL.md frontmatter format (YAML between `---` markers) for skill authoring; compatible
  with the Claude Code skills ecosystem (fields: name, description, examples, parameters,
  tools, timeout_seconds, max_calls, tags)
- `SkillRuntime` validates arguments and checks the call budget before invoking a handler
  with a configurable timeout. Each invocation gets its own `agent_id` under the parent
  `run_id`. Successful invocations record budget usage and emit `SKILL_CALL`.
- `IntentDispatcher` backed by `IntentClassifier` with dual-threshold enforcement; both the
  classifier and the dispatcher enforce the confidence threshold independently for robustness
  against custom classifier implementations
- Tag-based skill discovery and filtering in `SkillRegistry`; last-write-wins on name
  collision
- CLI: `forge skills list`, `forge skills explain`, `forge skills run`

**`forge-core`**

- `RunEventKind.TOOL_CALL` emitted after each successful MCP upstream dispatch
- `RunEventKind.SKILL_CALL` emitted after each successful skill invocation

---

## [0.1.1] — 2026-04-26

Adds rule packs, a shared intent classifier and an HTTP endpoint that serves an agent card.

### Added

**`forge-rules` — Rules Engine** (`pip install 'forge-os[rules]'`)

- Formal conflict resolution with a fixed deny > require > suggest precedence lattice
  (inter-category) and two intra-category strategies: `MOST_SPECIFIC_WINS` (default; path
  pattern specificity as proxy) and `PRIORITY_FIRST_MATCH` (explicit integer priority opt-in)
- Static scope intersection detection at lint time: identifies pairs of rules whose glob
  scope patterns can match the same path before any agent runs, using glob-to-regex
  compilation and probe-path derivation. Runs in O(N²) during `forge rules lint` only, not
  at inference time
- `RulesEngine` API: `load_pack()`, `unload_pack()`, `select()`, `inject()`, `lint()`,
  `validate()`
- `inject()` emits `RunEventKind.CONTEXT_INJECTED` for downstream context token accounting
- YAML round-trip serialization (`load_yaml` / `dump_yaml`) for rule packs and individual
  rules
- CLI: `forge rules validate`, `forge rules lint`, `forge rules explain`, `forge rules diff`

**`forge-core` — Agent cards and intent classification**

- SPIFFE identity field (`spiffe_id`) on `AgentCard` with URI format validation
  (`spiffe://<trust-domain>/<workload>`); the operator issues SVIDs, Forge validates the URI
- `AgentCard` gains `url`, `version` and `provider` fields.
- `RunStatus.INPUT_REQUIRED` for human-in-the-loop workflow pauses
- `RunEventKind.CONTEXT_INJECTED` and `RunEvent.context_tokens` for context token accounting
  across the platform
- `IntentClassifier` and `get_intent_classifier()` singleton backed by sentence-transformers;
  exposed as a shared primitive so rules, skills, and governance layers share a single
  embedding engine without duplicating model loading
- `Classifier` protocol (`async classify(text) → tuple[str, float]`) for
  dependency-injectable classification in downstream packages
- `ForgeConfig.intent_model` and `intent_confidence_threshold` configuration fields
- Optional `[intent]` package extra: `sentence-transformers>=3.0`, `numpy>=1.26`

**`forge-observe` — A2A Discovery**

- `GET /.well-known/agent.json` A2A discovery endpoint integrated into the observe FastAPI
  application
- Set `ObserveConfig.agent_base_url` to include the `url` field in the response. Without
  it, the endpoint serves the remaining card fields.

---

## [0.1.0] — 2026-04-18

First public release of the Python harness, CLI and HTTP API.

### Added

**`forge-core`**
- `MetaOrchestrator` with run lifecycle, EventBus, context propagation via `contextvars`
- `LLMCallInterceptor` protocol — framework-agnostic token / cost capture
- Self-evolution FSM: `IDLE → OBSERVING → PROPOSING → APPLYING → EVALUATING → COMMITTING | ROLLING_BACK`
- `PromptRewriteMutator` (LLM structured output), `ParameterTuneMutator`
  (applies diff to `RunConfig`), `AgentCullMutator` (snapshot-backed rollback)
- `TopologySnapshot` for atomic rollback
- `EvalSuite` with deterministic + synthetic evaluation
- `EvolutionJournal` with append-only provenance
- `CostBudgetGuard` + timeout enforcement via `asyncio.wait_for`
- `CircuitBreaker` with closed, open and half-open states
- `ForgeTestHarness` + `MockLLMProvider` for deterministic end-to-end tests

**`forge-memory`**
- `HybridMemory` fusing vector (ChromaDB), graph (NetworkX), and symbolic layers
- `MemoryContextInjector` adds recalled items to the next run's envelope
- Vector-clock versioning with conflict detection
- `IngestionPipeline` with regex + optional LLM-backed entity extraction
- NetworkX backend persists via SQLite (WAL) with atomic writes

**`forge-adapters`**
- `LangGraphAdapter` — injects `ForgeLangChainCallback` into `config`
- `CrewAIAdapter` — instruments `Agent.execute_task` / `BaseTool._run`
- `AutoGenAdapter` — subscribes to `TRACE_LOGGER_NAME` structured records
- `GenericCallableAdapter` — monkey-patches Anthropic / OpenAI / Gemini SDKs in-scope
- Framework detection helpers inspect imports in a source file.

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
- `forge doctor` — diagnose Python version, installed adapter extras,
  default memory backends, and optional network egress (`--network`)
- `forge evolve run` / `status` / `resume` — read the evolution
  journal and re-arm the circuit breaker from the CLI
- Rich live display: cost ticker, topology table, evolution journal

**Hardening**
- SSE endpoint caps live subscribers at 50; returns HTTP 503 beyond the limit;
  `/api/v1/health` reports `live_subscribers` and `max_live_subscribers`
- Runtime dependencies pinned with upper bounds: `pydantic<3.0`,
  `pydantic-settings<3.0`, `structlog<26.0`, `opentelemetry-sdk<2.0`,
  `fastapi<0.120`

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

# Forge Architecture

This document is the honest map of what Forge actually does at the
component level. Every subsystem described here is backed by tests in
`packages/*/tests/`; if a claim is made here and isn't exercised by a
test, it's a bug in the docs.

## High-level picture

```
┌───────────────────────────────┐
│        User's agent flow      │   (LangGraph, CrewAI, AutoGen,
│   (unchanged by Forge)        │    or any async callable)
└──────────────┬────────────────┘
               │  wrapped by
┌──────────────▼────────────────┐
│     Adapter (forge-adapters)  │   Detects framework, injects
│                               │   callbacks/interceptors.
└──────────────┬────────────────┘
               │  runs under
┌──────────────▼────────────────┐        ┌─────────────────────┐
│   MetaOrchestrator            │◀──────▶│  EventBus           │
│   (forge-core.harness)        │ publish│  (pub/sub fan-out)  │
└─┬────────────┬────────────────┘        └──┬──────────┬───────┘
  │            │                            │          │
  │ RunContext │ LLMCallInterceptor         │ subscribe│
  │ via        │ (forge-observe)            │          │
  │contextvars │                            ▼          ▼
  │            │                        Tracer     Metrics
  │            │                        (OTel)     (cardinality-safe)
  │            ▼
  │       CostModel + token counting
  │
  │       ┌────────────────────┐          ┌──────────────────────┐
  ├──────▶│ MemoryContextInject│───read──▶│ HybridMemory          │
  │       │ (pre-run hook)     │          │  (vector+graph+symb.) │
  │       └────────────────────┘          └──────────────────────┘
  │
  │       ┌────────────────────┐
  └──────▶│  EvolutionLoop     │───capture snapshot─┐
          │ (IDLE→OBSERVING→…) │                    │
          └─────────┬──────────┘                    ▼
                    │                      TopologySnapshot
                    │                      (immutable, hashed)
                    ▼
                Mutators (prompt rewrite, param tune,
                          agent cull, topology change)
```

---

## Core abstractions

### `RunContext` + `run_scope`

Defined in `forge_core.context`. A `ContextVar` carries `run_id`,
`agent_id`, `span_id`, and the task envelope through any async stack
depth. This is how an interceptor called from deep inside a third-party
graph knows which Forge run it's inside, without the adapter having to
plumb identifiers through every call.

```python
from forge_core.context import run_scope, current_run

with run_scope(RunContext(run_id=..., task_id=...)):
    ...  # current_run() works here, even across awaits
```

### `EventBus`

Defined in `forge_core.events`. Fan-out, async, failure-isolated
(handlers run under `gather(return_exceptions=True)`). Supports wildcard
subscriptions via the literal `"*"` or `ALL_EVENTS`. Publishing a
populated `RunEvent` is the canonical way any subsystem declares a fact
about the run ("an LLM was called for X tokens at $Y"); anything that
wants to observe runs — OTel tracer, metrics collector, memory
ingester, dashboard SSE endpoint — subscribes instead of receiving
callbacks.

### `LLMCallInterceptor`

Protocol in `forge_core.protocols`. Canonical implementation in
`forge_observe.interceptor.ForgeLLMInterceptor`. All adapters route
their LLM observations through this single protocol. A call produces:

1. `on_llm_start(model, agent_id, ...)` → opens an OTel span child of
   the active run span; returns a `span_id`.
2. `on_llm_end(span_id, input_tokens, output_tokens, output)` → closes
   the span, publishes an `LLM_CALL` event carrying real token counts
   and a real cost (from `CostModel`).

The interceptor does **not** estimate tokens. Adapters that can't
extract them from the SDK pass `0` and emit a structured warning.

### Adapters

Under `forge-adapters`. Each implements `Adapter` (load, run, stream,
topology, set_interceptor, set_bus). Framework-specific work:

- **LangGraph** — attaches `ForgeLangChainCallback` to the
  `config.callbacks` of `ainvoke/astream`. All LangChain chat models
  inside the graph now emit token usage to Forge.
- **CrewAI** — monkey-patches `crewai.Agent.execute_task` and
  `crewai.tools.BaseTool._run` inside a scoped context manager. LangChain
  callbacks cover the model layer.
- **AutoGen** — subscribes a log handler to `TRACE_LOGGER_NAME`, parses
  the structured events AutoGen emits internally, and republishes them
  on the bus.
- **Generic callable** — a context manager monkey-patches
  `anthropic.*.messages.create`, `openai.*.chat.completions.create`, and
  `google.generativeai.GenerativeModel.generate_content`. Best-effort:
  SDKs not installed are skipped silently; API shape changes degrade
  with a warning rather than fail.

---

## Evolution (FSM)

`forge_core.evolution.loop.EvolutionLoop` is an explicit state machine:

```
IDLE → OBSERVING → PROPOSING → APPLYING → EVALUATING → COMMITTING ─┐
                                                     │             │
                                                     └─ROLLING_BACK┘
                                                                   ↓
                                                                 IDLE
```

Every transition is guarded. `APPLYING` captures a `TopologySnapshot`
(frozen dataclass: `agents`, `config`, `prompts_map`, monotonic
`version`, content hash) before calling `mutator.apply`. A rollback is
`orchestrator.restore_snapshot(pre)` — replacement, not
reconstruction, so there's no way to partially restore. Rollbacks are
append-only in the journal; the version counter never rewinds.

Mutators share a single typed protocol over `TopologyState`:

```python
class Mutator(Protocol):
    mutation_kind: ClassVar[MutationKind]
    def propose(self, run_results) -> Mutation | None
    async def apply(self, mutation, state: TopologyState) -> TopologyState
    async def rollback(self, mutation, snapshot: TopologySnapshot) -> None
```

Real implementations:

- `PromptRewriteMutator` uses `LLMClient.structured_call` with a
  Pydantic schema (`PromptRewriteResponse`) — no regex, no free-form
  text.
- `ParameterTuneMutator` walks `MutationDiff.field_path` into
  `RunConfig` via a bidirectional resolver.
- `AgentCullMutator` removes agents; rollback is `restore_snapshot`.

Auto-trigger (in `MetaOrchestrator.run`) is gated by
`FORGE_ENABLE_EVOLUTION_AUTO` + per-run config (`min_history_for_evolution`,
`min_interval_seconds`, `trigger_every_n_runs`).

---

## Memory (hybrid + injection)

`HybridMemory` orchestrates three layers:

- Vector (ChromaDB default, Qdrant production).
- Graph (NetworkX dev, Neo4j prod). NetworkX persists through SQLite in
  WAL mode with normalized tables, not a pickle.
- Symbolic forward-chaining rule engine (\~200 LOC, strict DSL).

Results fuse through Reciprocal Rank Fusion.

`MemoryContextInjector` (`forge_memory.injection`) is the pre-run hook:
gated by `FORGE_ENABLE_MEMORY_INJECTION` + per-config flag, it runs
`HybridMemory.query` with a heuristic query-text extractor over the
`TaskEnvelope`, filters by `min_relevance`, and writes back into:

- `envelope.memory_context` — a formatted string snippet the flow can
  read;
- `envelope.context["forge_memory"]` — the structured entries;
- `envelope.metadata["memory_hits"]` — the count, useful for metrics.

Backend errors never propagate out of the injector: they log
`memory_injection.backend_error` and pass the envelope through.

### Versioning

`MemoryVersionStore` owns a `VectorClock` per entry. `submit_write`
compares incoming clocks:

- unseen entry → accept.
- `before` / `equal` → discard (merge clock state only).
- `after` → replace.
- `concurrent` → delegate to a `ConflictResolver`. Default is
  `LastWriterWinsResolver` with a deterministic lex tiebreak on
  `writer_id`, so two nodes racing converge to the same winner without
  a coordinator.

---

## Observability

- `ForgeLLMInterceptor` (forge-observe) — OTel spans, cost calc.
- `CostModel` — real pricing table for Claude / GPT / Gemini families
  at April 2026 prices, with a well-defined unknown-model fallback
  (`$0.00` + `cost_model.unknown_model` warning).
- `MetricsCollector` — aggregates bus events into per-agent / per-model
  / session metrics. **Agent IDs pass through a `LabelSanitizer`** so a
  unique-id-per-run caller can't grow the dimension without bound.
- `LabelSanitizer` — allowlist + per-key unique-value cap. Over-cap
  values hash into a stable `bucket_<sha1prefix>` bucket so observers
  still see distinct-ish series without a metrics-store meltdown.

### Dashboard backend

FastAPI app in `forge_observe.exporters.api`:

- `GET /api/v1/health`
- `GET /api/v1/runs` + `POST /api/v1/runs` (ingest)
- `GET /api/v1/runs/{id}`
- `GET /api/v1/evolution/journal`
- `GET /api/v1/evolution/stats` — fitness series + by-kind totals
- `GET /api/v1/memory/query?q=&top_k=` — unified query (503 if no
  provider attached via `set_memory_provider`)
- `GET /api/v1/memory/graph` — React Flow JSON from NetworkX
- `GET /api/v1/stream` — SSE live events, 15-second heartbeat, bounded
  per-subscriber queue

---

## Guards

- `CostBudgetGuard` (`forge_core.guards`) subscribes to `LLM_CALL`
  events, accumulates cost, and fires an `asyncio.Event` when over the
  ceiling. `MetaOrchestrator.run` races the guard against the adapter
  task; whoever wins dictates the outcome (`BudgetExceeded` vs normal
  result). Timeout is a simple `asyncio.wait_for` above the race.
- `CircuitBreaker` (`forge_core.circuit_breaker`) is a three-state
  breaker used for:
  - LLM API calls (trip on repeated SDK failures so hung endpoints stop
    starving the loop),
  - memory backend queries (so ChromaDB/Neo4j outages degrade to empty
    retrieval instead of blocking runs),
  - the evolution loop (auto-suspend after repeated rollbacks until the
    operator calls `reset()`).

All three breakers are ordinary `CircuitBreaker` instances; the policy
lives at the call site, not in the breaker.

---

## Testing scaffolding

`forge_core.testing` ships two primitives that every acceptance test in
this repo uses and that downstream users should reach for too:

- `MockLLMProvider` — programmable scripted responses with token
  counts. Implements the `LLMClient.structured_call` contract so the
  evolution loop can be driven deterministically.
- `ForgeTestHarness` — composes an `EventBus` + a test-only
  interceptor; its `run_flow(callable, input)` wraps every
  `llm.complete(...)` call with `on_llm_start` / `on_llm_end`, so the
  flow author gets realistic event streams without writing interceptor
  glue.

Integration tests (`test_full_stack.py`,
`test_full_stack_metrics.py`) use these to assert on the complete
pipeline without a network call.

---

## Feature flags (all default OFF)

| Flag | Package | Effect |
|------|---------|--------|
| `FORGE_ENABLE_EVOLUTION_AUTO` | forge-core | Auto-trigger after N runs |
| `FORGE_ENABLE_MEMORY_INJECTION` | forge-core | Inject memory into envelopes |
| `FORGE_ENABLE_LLM_JUDGE` | forge-core | Quality score via LLM |
| `FORGE_ENABLE_LLM_ENTITY_EXTRACTION` | forge-memory | Entities via LLM vs regex |
| `FORGE_ENABLE_GENERIC_INSTRUMENTATION` | forge-observe | Monkey-patch LLM SDKs |
| `FORGE_COST_HARD_FAIL` | forge-core | `BudgetExceeded` raise vs warn |

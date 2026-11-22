# Historical harness architecture

This guide describes the earlier multi-package harness. The [historical test reference](feature-map.md) links selected behaviors to their tests. For the standalone Forge Check runtime, see the [binding guide](governed.md) and [architecture decisions](decisions/index.md).

## High-level picture

```
┌───────────────────────────────┐
│        User's agent flow      │   (LangGraph, CrewAI, AutoGen,
│   (unchanged by Forge)        │    or any async callable)
└──────────────┬────────────────┘
               │  wrapped by
┌──────────────▼────────────────┐
│     Adapter (forge_adapters)  │   Detects framework, injects
│                               │   callbacks/interceptors.
└──────────────┬────────────────┘
               │  runs under
┌──────────────▼────────────────┐        ┌─────────────────────┐
│   MetaOrchestrator            │◀──────▶│  EventBus           │
│   (forge_core.harness)        │ publish│  (pub/sub fan-out)  │
└─┬────────────┬────────────────┘        └──┬──────────┬───────┘
  │            │                            │          │
  │ RunContext │ LLMCallInterceptor         │ subscribe│
  │ via        │ (forge-observe)            │          │
  │contextvars │                            ▼          ▼
  │            │                        Tracer     Metrics
  │            │                        (OTel)     (sanitized labels)
  │            ▼
  │       CostModel + token usage
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

A `ContextVar` carries the run, task and span identifiers across awaits. An interceptor can read those identifiers while the adapter executes the graph.

```python
from forge_core.context import RunContext, current_run, run_scope

with run_scope(RunContext(run_id="r1", task_id="t1")):
    assert current_run().task_id == "t1"
```

### `EventBus`

Subscribers register for a particular event kind or the wildcard `ALL_EVENTS`. On publication, `forge_core.events.EventBus` awaits the matching handlers concurrently and logs handler exceptions. A slow handler can delay publication.

### `LLMCallInterceptor`

The protocol is defined in `forge_core.protocols`. The observe package implements it in `ForgeLLMInterceptor`. When an adapter reports a call, `on_llm_start` opens a span and returns its ID. `on_llm_end` records the usage supplied by the adapter and publishes an `LLM_CALL` event. The cost model estimates the charge from the recorded token counts and its configured price table. Missing token counts can appear as zero.

### Adapters

The `forge-os-adapters` distribution provides the following integration points.

- **LangGraph** — attaches `ForgeLangChainCallback` to the
  `config.callbacks` of `ainvoke/astream`. Token counts depend on the usage metadata the model integration passes to the callback.
- **CrewAI** — monkey-patches `crewai.Agent.execute_task` and
  `crewai.tools.BaseTool._run` inside a scoped context manager. LangChain
  callbacks cover the model layer.
- **AutoGen** — subscribes a log handler to `TRACE_LOGGER_NAME`, parses
  the structured events AutoGen emits internally, and republishes them
  on the bus.
- **Generic callable** — a context manager monkey-patches
  `anthropic.*.messages.create`, `openai.*.chat.completions.create`, and
  `google.generativeai.GenerativeModel.generate_content`. Best-effort:
  the hook applies to the supported SDK methods found in the process.

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

`APPLYING` captures a `TopologySnapshot`
(frozen dataclass: `agents`, `config`, `prompts_map`, monotonic
`version`, content hash) before calling `mutator.apply`. A rollback calls `orchestrator.restore_snapshot(pre)` to restore the captured topology. The journal records the rollback and the version counter continues forward.

A mutator proposes a change from recent run results and applies it to a `TopologyState`. Its rollback method takes the post-mutation state and returns a state. The orchestrator can also restore a captured topology snapshot. [Mutator protocol](https://github.com/angelnicolasc/forge/blob/main/packages/forge-core/src/forge_core/protocols.py)

- `PromptRewriteMutator` uses `LLMClient.structured_call` with a
  Pydantic schema (`PromptRewriteResponse`).
- `ParameterTuneMutator` walks `MutationDiff.field_path` into
  `RunConfig` via a bidirectional resolver.
- `AgentCullMutator` removes agents; rollback is `restore_snapshot`.

Auto-trigger (in `MetaOrchestrator.run`) is gated by
`FORGE_ENABLE_EVOLUTION_AUTO` + per-run config (`min_history_for_evolution`,
`min_interval_seconds`, `trigger_every_n_runs`).

---

## Memory (hybrid + injection)

`HybridMemory` orchestrates three layers:

- ChromaDB stores vector entries.
- NetworkX stores graph data in SQLite using WAL.
- The symbolic rule engine filters or reranks retrieved entries.

The query combines vector and graph rankings with reciprocal rank fusion. An application can inject its own backend. The shipped Neo4j class is an interface stub.

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

- `ForgeLLMInterceptor` records spans and usage for observed calls.
- `DefaultCostModel` uses a local price table. For an unknown model it records zero and logs `cost_model.unknown_model`. A run summary can therefore understate the provider bill.
- `MetricsCollector` groups events by agent, model and session. `LabelSanitizer` keeps allowlisted keys and the first configured number of raw values per key. Additional values get an eight-character SHA-1 prefix. Distinct overflow values can still produce distinct metric series.

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
  - LLM instrumentation (after repeated failures, the interceptor can skip opening a span),
  - memory backend queries (so ChromaDB/Neo4j outages degrade to empty
    retrieval instead of blocking runs),
  - the evolution loop (auto-suspend after repeated rollbacks until the
    operator calls `reset()`).

Each caller configures its own breaker.

---

## Testing scaffolding

`forge_core.testing` supplies fixtures for harness integration tests.

- `MockLLMProvider` — programmable scripted responses with token
  counts. Implements the `LLMClient.structured_call` contract so the
  evolution loop can be driven deterministically.
- `ForgeTestHarness` — composes an `EventBus` + a test-only
  interceptor; its `run_flow(callable, input)` wraps every
  `llm.complete(...)` call with `on_llm_start` / `on_llm_end`, to produce repeatable test events.

The harness integration suite uses these fixtures to check run events and aggregate metrics offline.

---

## Feature flags (all default OFF)

| Flag | Package | Effect |
|------|---------|--------|
| `FORGE_ENABLE_EVOLUTION_AUTO` | forge-core | Auto-trigger after N runs |
| `FORGE_ENABLE_MEMORY_INJECTION` | forge-memory | Inject memory into envelopes |
| `FORGE_ENABLE_LLM_JUDGE` | forge-core | Quality score via LLM |
| `FORGE_ENABLE_LLM_ENTITY_EXTRACTION` | forge-memory | Entities via LLM vs regex |

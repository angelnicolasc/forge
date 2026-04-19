<div align="center">

# forge-core

**The orchestration engine behind Forge.**

Production-grade primitives for wrapping agent systems with a uniform run model, structured events, budget guards, topology snapshots, and an opt-in self-evolution loop.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-orange.svg)](../../LICENSE)
[![Package](https://img.shields.io/badge/package-forge--core-111827.svg)](https://github.com/angelnicolasc/forge/tree/main/packages/forge-core)

</div>

---

## Why `forge-core`

Most agent stacks start as framework-specific flows and end up needing the same hard parts:

- a stable task envelope
- a consistent run result model
- structured events and telemetry hooks
- budget and timeout enforcement
- adapter boundaries that do not lock you into one framework
- a safe way to evolve topology and prompts over time

`forge-core` is the package that standardizes those concerns. It gives the rest of Forge a shared contract, and it is also useful on its own if you are building adapters, custom harnesses, or evaluation infrastructure around agent workflows.

## What lives in this package

- `MetaOrchestrator` for loading adapters, running flows, aggregating events, and coordinating evolution
- Shared types such as `TaskEnvelope`, `RunResult`, `RunEvent`, `AgentCard`, and `RunConfig`
- Protocol-based extension points for adapters, memory backends, mutators, interceptors, evaluators, and cost models
- `EventBus` for in-process fan-out to observers
- Runtime guards for cost ceilings and timeouts
- `CircuitBreaker` for dependency protection
- Topology snapshotting and mutation support for the self-evolution loop
- Test harness utilities for deterministic LLM-driven flow testing without real SDK calls

## Installation

Inside the Forge workspace:

```bash
uv sync
```

Standalone:

```bash
pip install forge-core
```

Optional extras:

```bash
pip install 'forge-core[evolution]'
```

Requirements:

- Python 3.11+
- `pydantic`
- `pydantic-settings`
- `structlog`
- `opentelemetry-api`
- `opentelemetry-sdk`

## Quickstart

The main entrypoint is `MetaOrchestrator`. It runs a loaded adapter under a consistent task and result model.

```python
import asyncio

from forge_core.harness import MetaOrchestrator
from forge_core.types import TaskEnvelope


async def main() -> None:
    orchestrator = MetaOrchestrator()
    await orchestrator.load("my_flow.py")

    result = await orchestrator.run(
        TaskEnvelope(
            input={"query": "Find retrieval optimization ideas"},
        )
    )

    print(result.status)
    print(result.duration_ms)
    print(result.cost.total_cost)
    print(result.output)


asyncio.run(main())
```

## Core ideas

### 1. One run model for every adapter

`forge-core` defines a single contract:

```text
TaskEnvelope -> Adapter -> RunResult
```

That contract lets different frameworks plug into the same harness without changing the surrounding orchestration code.

### 2. Events are the source of truth

The orchestrator publishes `RunEvent` objects onto an `EventBus`. Observers subscribe independently, so tracing, metrics, memory ingestion, dashboards, and evolution triggers do not have to be hard-wired into the adapter itself.

This keeps the runtime composable and makes instrumentation easier to reason about.

### 3. Guardrails are first-class

`run_with_guards()` enforces:

- wall-clock timeouts
- per-run cost ceilings

These are not post-hoc warnings. They are active runtime controls designed to stop runaway flows before they keep spending.

### 4. Evolution is snapshot-based

The evolution loop works against captured topology state, applies mutations, evaluates them, and commits or rolls them back. The rollback path is snapshot-driven, which gives the package a safer foundation for automatic changes than mutating live objects in place.

## Architecture

```text
source file / module
        |
        v
   Registry.detect_adapter()
        |
        v
   MetaOrchestrator
        |
        +--> run_scope(RunContext)
        +--> EventBus.publish(RUN_STARTED / LLM_CALL / RUN_COMPLETED / ...)
        +--> run_with_guards(cost, timeout)
        +--> aggregate RunResult
        +--> optional EvolutionLoop.step()
```

## Public API at a glance

Top-level exports from `forge_core`:

- `AgentCard`
- `CostSummary`
- `Mutation`
- `MutationKind`
- `RunConfig`
- `RunEvent`
- `RunEventKind`
- `RunResult`
- `RunStatus`
- `TaskEnvelope`
- `ToolRef`

Primary modules:

- `forge_core.harness`
- `forge_core.types`
- `forge_core.protocols`
- `forge_core.events`
- `forge_core.guards`
- `forge_core.circuit_breaker`
- `forge_core.registry`
- `forge_core.testing`
- `forge_core.evolution`

## `MetaOrchestrator`

`MetaOrchestrator` is the main runtime object. It is responsible for:

- loading or accepting an adapter
- wiring the event bus and interceptor
- executing runs under `run_scope`
- applying memory injection when attached
- enforcing budgets and timeouts
- collecting run history
- exposing topology
- coordinating automatic evolution when enabled

Key methods:

- `load(source)`
- `set_adapter(adapter)`
- `run(envelope)`
- `stream(envelope)`
- `topology()`
- `attach_memory(memory)`
- `attach_evolution_loop(loop)`
- `capture_snapshot()`
- `swap_topology(...)`
- `restore_snapshot(snapshot)`

## Protocol-first extension model

`forge-core` uses Python `Protocol`s rather than inheritance-heavy base classes. If an object satisfies the expected methods, it can participate in the system.

Important protocols include:

- `Adapter`
- `MemoryBackend`
- `Mutator`
- `LLMCallInterceptor`
- `CostModel`
- `Evaluator`

This makes it easier to integrate framework-specific adapters and local infrastructure without forcing everything through a rigid class hierarchy.

## Shared types

The shared type system is one of the most valuable parts of the package.

Important models include:

- `TaskEnvelope`
  - universal task input, with config, tags, context, and memory context
- `RunResult`
  - final output, status, cost, duration, events, topology, and errors
- `RunEvent`
  - structured trace unit for run lifecycle, LLM calls, tool calls, memory, and errors
- `RunConfig`
  - per-run limits and feature toggles
- `AgentCard`
  - portable agent topology representation
- `MemoryEntry` and `MemoryQuery`
  - memory types shared with `forge-memory`
- `Mutation`, `MutationDiff`, and `FitnessScore`
  - evolution primitives

## Event bus

`EventBus` is an in-process async fan-out bus for `RunEvent` objects.

Highlights:

- exact-kind subscriptions
- wildcard subscriptions through `ALL_EVENTS`
- failure isolation for subscribers
- simple introspection for tests and dashboards

Example:

```python
from forge_core.events import ALL_EVENTS, EventBus

bus = EventBus()


async def handle(event):
    print(event.kind, event.run_id)


subscription = bus.subscribe(ALL_EVENTS, handle)
```

## Runtime guards

`forge_core.guards` provides the runtime protection layer.

- `CostBudgetGuard` subscribes to `LLM_CALL` events and trips when a run reaches its ceiling
- `run_with_guards()` races the adapter execution against cost and timeout limits
- `BudgetExceeded` and `TimeoutExceeded` give callers explicit failure modes

This is a useful layer even outside the rest of Forge if you need hard operational boundaries around agent execution.

## Circuit breakers

`CircuitBreaker` implements a classic:

```text
CLOSED -> OPEN -> HALF_OPEN -> CLOSED
```

Forge uses it to isolate unhealthy dependencies such as:

- memory backends
- evolution auto-trigger paths
- external service integrations

The implementation is async-friendly and designed around `time.monotonic()` to avoid wall-clock drift problems.

## Evolution engine

The self-evolution loop lives under `forge_core.evolution`.

The loop follows:

```text
Observe -> Hypothesize -> Mutate -> Evaluate -> Commit / Rollback
```

Included pieces:

- `EvolutionLoop`
- `EvalSuite`
- `EvalCase`
- `TopologySnapshot`
- mutation infrastructure
- evolution journal
- evaluator support

The package supports both suggest-only mode and automatic application paths, with rollback thresholds and breaker-based suspension when repeated failures occur.

## Testing utilities

`forge_core.testing` ships test helpers intended for downstream users, not just Forge itself.

Included utilities:

- `MockLLMProvider`
- `MockResponse`
- `ForgeTestHarness`
- `HarnessRun`

This makes it possible to test agent flows with:

- deterministic responses
- explicit token counts
- synthetic but realistic LLM events
- no dependency on live model providers

Example:

```python
from forge_core.testing import ForgeTestHarness

harness = ForgeTestHarness()
harness.llm.program(["draft answer", "final answer"])


async def flow(inp, llm):
    plan = await llm.complete(prompt=inp["q"], agent_id="planner")
    result = await llm.complete(prompt=plan.content, agent_id="executor")
    return result.content


run = await harness.run_flow(flow, {"q": "Design a retrieval strategy"})
assert run.total_cost > 0
assert len(run.events) == 2
```

## Package boundaries

`forge-core` is intentionally focused on orchestration contracts and runtime primitives.

It does not try to own every concern itself:

- `forge-memory` handles hybrid memory and context injection backends
- `forge-observe` provides the default LLM interceptor and observability stack
- `forge-adapters` provides framework-specific adapter implementations
- `forge-cli` exposes the command-line interface

That split keeps the core package reusable in lighter deployments.

## Testing

From the repository root:

```bash
pytest packages/forge-core/tests -q
```

The test suite covers:

- circuit breaker state transitions
- event-driven cost tracking
- guard enforcement
- topology snapshots and swaps
- mutation proposals and rollbacks
- full-stack harness behavior
- deterministic evaluation flows
- testing utilities

## License

Apache-2.0

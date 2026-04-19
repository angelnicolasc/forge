<div align="center">

# forge-adapters

**Framework adapters for Forge.**

Drop-in wrappers for LangGraph, CrewAI, AutoGen, and generic Python callables, all normalized into the same Forge runtime contract.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-orange.svg)](../../LICENSE)
[![Package](https://img.shields.io/badge/package-forge--adapters-111827.svg)](https://github.com/angelnicolasc/forge/tree/main/packages/forge-adapters)

</div>

---

## Why `forge-adapters`

Every agent framework brings its own execution model, callback system, and topology representation.

That becomes a problem the moment you want one operational layer across all of them:

- one orchestrator
- one task envelope
- one result type
- one event model
- one tracing and cost path

`forge-adapters` is the compatibility layer that makes that possible. It converts framework-specific flows into Forge's shared `Adapter` contract so the rest of the stack can stay uniform.

## What ships in this package

- `LangGraphAdapter`
- `CrewAIAdapter`
- `AutoGenAdapter`
- `GenericCallableAdapter`
- `BaseAdapter`
- framework discovery helpers
- LangChain callback integration for LangGraph and CrewAI

## Installation

Inside the Forge workspace:

```bash
uv sync
```

Standalone:

```bash
pip install forge-adapters
```

Framework-specific extras:

```bash
pip install 'forge-adapters[langgraph]'
pip install 'forge-adapters[crewai]'
pip install 'forge-adapters[autogen]'
pip install 'forge-adapters[all]'
```

Requirements:

- Python 3.11+
- `forge-core==0.1.0`

## The adapter contract

Every adapter in this package targets the same shape:

```text
TaskEnvelope -> Adapter.run(...) -> RunResult
```

That means the orchestrator can load and run very different frameworks without changing the outer runtime model.

In practice, each adapter is responsible for:

- detecting or loading a user flow
- extracting a topology when possible
- executing the framework-specific runtime
- emitting or surfacing `RunEvent` data
- cooperating with the shared Forge interceptor and event bus

## Supported adapters

### `LangGraphAdapter`

Wraps compiled LangGraph graphs and injects Forge's LangChain callback into runtime config so model calls are observed through the shared interceptor.

What it does:

- detects `langgraph` imports in Python source
- loads a graph from a `graph` variable, `build_graph()` function, or a graph-like object in module scope
- extracts graph nodes into `AgentCard` topology entries
- instruments `ainvoke()` or `invoke()` execution paths
- supports streaming through `astream_events()` or `astream()`

This is the most direct path when a user already has a LangGraph workflow and wants Forge-level observability without rewriting the flow.

### `CrewAIAdapter`

Wraps CrewAI crews and instruments execution through scoped monkey-patching plus LangChain callback injection.

What it does:

- detects `crewai` imports
- loads a crew from a `crew` variable or `build_crew()` function
- extracts agents, roles, descriptions, and model names into topology
- monkey-patches `Agent.execute_task` to emit agent-boundary events
- monkey-patches `BaseTool._run` to emit tool events
- injects the shared LangChain callback into `Agent.llm.callbacks`
- runs `crew.kickoff()` in a worker thread so the async runtime stays responsive

This adapter is designed for end-to-end observability without requiring changes inside the CrewAI flow itself.

### `AutoGenAdapter`

Wraps AutoGen agent teams and instruments them through structured log handling.

What it does:

- detects `autogen` and `autogen_agentchat` imports
- loads a team from a `team` variable or `build_team()` function
- extracts participant topology when available
- attaches a temporary handler to AutoGen's structured trace logger
- converts AutoGen lifecycle and LLM log events into Forge events
- computes cost locally for LLM events before publishing them to the bus

This adapter is intentionally defensive around event payload drift, because structured logging schemas can move across framework versions.

### `GenericCallableAdapter`

Wraps any async or sync callable into the Forge adapter interface.

This is the escape hatch for custom flows that do not live inside LangGraph, CrewAI, or AutoGen.

What it does:

- accepts any Python callable
- emits top-level `AGENT_START` and `AGENT_END` boundaries
- supports async callables directly
- supports sync callables via `asyncio.to_thread`
- scopes SDK monkey-patching for supported LLM clients during execution

That last point is the key differentiator. When the user calls SDKs directly inside their function, Forge can still capture telemetry through temporary instrumentation of common client libraries.

## Framework discovery

`forge_adapters.discovery` provides file-based auto-detection for known frameworks.

Current signatures:

- `langgraph`
- `crewai`
- `autogen`
- `autogen_agentchat`

Example:

```python
from forge_adapters.discovery import discover_adapter

adapter = discover_adapter("my_flow.py")
```

This is the mechanism higher-level Forge components use to turn a Python file into the right adapter automatically.

## Quickstart

### Wrap a generic callable

```python
import asyncio

from forge_adapters.generic import GenericCallableAdapter
from forge_core.types import TaskEnvelope


async def flow(payload: dict) -> dict:
    return {"answer": f"hello {payload['name']}"}


async def main() -> None:
    adapter = GenericCallableAdapter(fn=flow, adapter_name="hello_flow")
    await adapter.load("<inline>")
    result = await adapter.run(TaskEnvelope(input={"name": "forge"}))
    print(result.output)


asyncio.run(main())
```

### Detect a framework from source

```python
from forge_adapters.discovery import discover_adapter

adapter = discover_adapter("my_langgraph_flow.py")
await adapter.load("my_langgraph_flow.py")
```

## Shared execution model

All concrete adapters build on `BaseAdapter`, which provides:

- load lifecycle
- error handling
- run timing
- `RunResult` construction
- topology access
- default stream behavior

Framework-specific adapters override:

- `detect(...)`
- `_load_impl(...)`
- `_execute(...)`

This keeps the package consistent without forcing every integration to reinvent the same boilerplate.

## Instrumentation strategy by framework

One of the most important aspects of `forge-adapters` is that instrumentation is not uniform across frameworks, because the frameworks themselves are not uniform.

### LangGraph

Uses callback injection.

- installs `ForgeLangChainCallback`
- routes model lifecycle into the shared interceptor
- lets the orchestrator aggregate costs from bus events

### CrewAI

Uses monkey-patching plus callback injection.

- patches agent execution boundaries
- patches tool execution boundaries
- injects callbacks into `Agent.llm`

### AutoGen

Uses structured log consumption.

- listens to AutoGen's trace logger
- reconstructs LLM and agent events from `LogRecord.event`
- publishes populated Forge events onto the bus

### Generic callable

Uses SDK monkey-patching.

- patches supported LLM SDK entrypoints inside a scoped context
- restores original methods after the run
- works for both async and sync user functions

This split is important because it shows the design is framework-aware rather than pretending every ecosystem can be instrumented the same way.

## SDK patching in the generic adapter

The generic adapter is especially useful for direct SDK users because it scopes monkey-patching during the run and restores the original SDK methods afterward.

That gives you a path from:

```text
plain Python function calling model SDKs directly
```

to:

```text
Forge-compatible run with LLM_CALL events, token counts, and costs
```

without forcing the user to refactor their code into a specific framework.

## Topology extraction

When possible, adapters convert framework-native structures into `AgentCard` entries.

Examples:

- LangGraph nodes become graph-node agents
- CrewAI agents surface role, goal, backstory, and model
- AutoGen participants become team agents
- Generic callable adapters expose a single synthetic node representing the wrapped flow

This gives dashboards and mutation systems a framework-independent topology view.

## Public API at a glance

Top-level exports from `forge_adapters`:

- `BaseAdapter`
- `GenericCallableAdapter`
- `discover_adapter`

Primary modules:

- `forge_adapters.base`
- `forge_adapters.discovery`
- `forge_adapters.generic`
- `forge_adapters.langgraph_adapter`
- `forge_adapters.crewai_adapter`
- `forge_adapters.autogen_adapter`
- `forge_adapters.langchain_callback`

## Package boundaries

`forge-adapters` focuses on framework integration only.

Related packages:

- `forge-core`
  - protocols, types, orchestrator, event model
- `forge-observe`
  - interceptor, tracer, metrics, cost model
- `forge-memory`
  - memory backends and context injection
- `forge-cli`
  - user-facing commands and package composition

This separation keeps adapter code focused on translating framework behavior rather than owning the entire runtime.

## Recommended usage pattern

In a full Forge application, adapters are usually not used standalone. The typical flow is:

1. `MetaOrchestrator` detects or receives an adapter
2. The orchestrator wires the shared interceptor and bus into that adapter
3. The adapter executes the framework-specific flow
4. Costs, spans, and events flow back through the shared Forge runtime

That design is what makes framework diversity manageable without fragmenting observability or orchestration logic.

## Testing

From the repository root:

```bash
pytest packages/forge-adapters/tests -q
```

The test suite covers:

- generic callable wrapping
- SDK monkey-patching and restoration
- CrewAI agent and tool instrumentation
- AutoGen structured-log instrumentation
- framework detection paths
- topology extraction behavior
- degraded execution when optional wiring is absent

## License

Apache-2.0

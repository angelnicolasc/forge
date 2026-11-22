<div align="center">

# forge-os-adapters

**Framework adapters for Forge.**

Drop-in wrappers for LangGraph, CrewAI, AutoGen, and generic Python callables, all normalized into the same Forge runtime contract.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-orange.svg)](https://github.com/angelnicolasc/forge/blob/main/LICENSE)

</div>

Part of the earlier Forge agent harness. For the standalone CI check, see [Forge Check](https://pypi.org/project/forge-os-check/).

---

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
pip install forge-os-adapters
```

Framework-specific extras:

```bash
pip install 'forge-os-adapters[langgraph]'
pip install 'forge-os-adapters[crewai]'
pip install 'forge-os-adapters[autogen]'
pip install 'forge-os-adapters[all]'
```

Requirements:

- Python 3.11+
- `forge-os-core==0.2.0`

### Migrating from the earlier package name

The old `forge-adapters==0.2.0` wheel depends on an unrelated distribution named `forge-core`. For this harness, replace `forge-adapters` with `forge-os-adapters` in your requirements, keeping any framework extra. Replace any direct `forge-core` requirement intended for this harness with `forge-os-core`.

Regenerate the lock file and rebuild the virtual environment. An existing exact pin or lock entry can still select the old wheel even after a PyPI yank. Inspect the new lock for `forge-os-adapters` and its dependency `forge-os-core`. The Python import remains `forge_adapters`.

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

The [LangGraph example](https://github.com/angelnicolasc/forge/tree/main/examples/langgraph_research) selects this adapter explicitly.

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

This helper can be called explicitly from Python. The 0.2.0 CLI uses a separate entry-point lookup, and the published adapters lack those registrations.

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

## Instrumentation strategy by framework

One of the most important aspects of `forge-os-adapters` is that instrumentation is not uniform across frameworks, because the frameworks themselves are not uniform.

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

`forge-os-adapters` focuses on framework integration only.

Related packages:

- `forge-os-core`
  - protocols, types, orchestrator, event model
- `forge-os-observe`
  - interceptor, tracer, metrics, cost model
- `forge-os-memory`
  - memory backends and context injection
- `forge-os-cli`
  - user-facing commands and package composition

This separation keeps adapter code focused on translating framework behavior rather than owning the entire runtime.

## Recommended usage pattern

In a full Forge application, adapters are usually not used standalone. The typical flow is:

1. The caller loads an adapter and registers it with `MetaOrchestrator.set_adapter()`
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

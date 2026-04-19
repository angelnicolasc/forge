<div align="center">

# forge-cli

**The command-line entrypoint for Forge.**

Run, wrap, inspect, diagnose, and evolve multi-agent systems from a single CLI built on top of Forge's orchestration, memory, observability, and adapter packages.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-orange.svg)](../../LICENSE)
[![Package](https://img.shields.io/badge/package-forge--cli-111827.svg)](https://github.com/angelnicolasc/forge/tree/main/packages/forge-cli)

</div>

---

## Why `forge-cli`

The rest of Forge is a set of composable Python packages.

`forge-cli` is the operational surface that makes those packages usable from the terminal:

- detect a flow
- run it with one command
- inspect memory
- bring up the observe API
- diagnose a local install
- trigger or inspect evolution behavior

It is the "front door" of the project, but it is still thin by design. The CLI delegates to the underlying packages rather than reimplementing orchestration logic in shell-shaped code.

## What exists today

The CLI currently exposes these command groups:

- `forge wrap`
- `forge run`
- `forge memory`
- `forge observe`
- `forge doctor`
- `forge evolve`

Those commands are wired through Typer and call into:

- `forge-core`
- `forge-adapters`
- `forge-observe`
- `forge-memory`

## Installation

Inside the Forge workspace:

```bash
uv sync
```

Standalone:

```bash
pip install forge-cli
```

If you want the full experience, install the whole stack through the meta-package instead:

```bash
pip install forge-os
```

The CLI entrypoint installed by this package is:

```bash
forge
```

## Command overview

### `forge wrap`

`forge wrap` is the highest-level execution command in the current CLI.

What it does today:

- prints the Forge banner
- auto-detects the framework for a source file
- loads the flow through `MetaOrchestrator`
- builds a `TaskEnvelope`
- runs the flow
- renders Rich output for run status, topology, and cost
- optionally ingests output into memory
- optionally starts the observe API in the background
- optionally triggers one evolution step after the run

Representative usage:

```bash
forge wrap my_flow.py
forge wrap my_flow.py --input '{"query":"What is RAG?"}'
forge wrap my_flow.py --budget 1.50 --observe
forge wrap my_flow.py --evolution
```

### `forge run`

`forge run` is the more direct execution path. It loads a flow, constructs a task envelope, and runs it with optional streaming.

What it does today:

- loads the source through the orchestrator
- supports JSON input from a string or file
- supports cost ceilings
- supports memory and evolution toggles in run config
- can stream emitted events to stdout instead of rendering the final dashboard

Representative usage:

```bash
forge run my_flow.py --input '{"query":"Summarize this"}'
forge run my_flow.py --input-file payload.json
forge run my_flow.py --stream
```

### `forge memory`

The memory command group currently includes:

- `forge memory query`
- `forge memory ingest`
- `forge memory status`

What those do today:

- query a local `HybridMemory` instance
- ingest a text file into memory
- print health and stats for the default memory backends

Representative usage:

```bash
forge memory query "retrieval optimization ideas"
forge memory query "RAG" --entity LLM --entity OpenAI
forge memory ingest notes.txt --run-id abc123 --tag topic=rag
forge memory status
```

### `forge observe`

`forge observe` starts the observe REST + SSE API server.

What it does today:

- launches the FastAPI app through `uvicorn`
- prints the docs URL
- prints the SSE stream endpoint
- can optionally open the docs in a browser

Important rigor note:

- the current command starts the API server
- it does **not** ship a first-party web dashboard UI in this package today
- the command help itself states that a web UI is planned for a later release

Representative usage:

```bash
forge observe
forge observe --host 0.0.0.0 --port 8787 --no-open
```

### `forge doctor`

`forge doctor` is an environment diagnostic.

What it checks today:

- Python version
- importability of the core Forge packages
- presence of default memory backends
- presence of optional framework extras
- optional network reachability for Anthropic and OpenAI endpoints

Representative usage:

```bash
forge doctor
forge doctor --network
```

### `forge evolve`

The evolution command group currently includes:

- `forge evolve run`
- `forge evolve status`
- `forge evolve resume`

What it does today:

- manually runs the evolution loop against a source flow
- reads a JSONL journal to show prior mutation outcomes
- resets the evolution circuit breaker after it opened

Important rigor note:

- `forge evolve run` loads the flow and calls the evolution loop
- the loop only meaningfully proceeds when there is enough run history
- `forge evolve status` is a read-side journal view, not a live controller UI

Representative usage:

```bash
forge evolve run my_flow.py --mode suggest
forge evolve run my_flow.py --mode auto --max-mutations 3
forge evolve status
forge evolve resume my_flow.py
```

## Quickstart

### 1. Wrap a local flow

```bash
forge wrap examples/langgraph_research/flow.py --input '{"query":"Latest agent harness papers"}'
```

### 2. Run with a budget

```bash
forge wrap my_flow.py --budget 0.50
```

### 3. Inspect memory

```bash
forge memory status
forge memory query "planner output from last run"
```

### 4. Start the observe API

```bash
forge observe --no-open
```

### 5. Diagnose the installation

```bash
forge doctor
```

## CLI design principles

### Thin command layer

The CLI is intentionally thin. It mostly parses arguments, builds config and input objects, and delegates to the runtime packages.

That matters because it keeps the terminal surface aligned with the Python API instead of inventing a parallel execution model.

### Rich output, not vague output

The package uses Rich-based rendering from `forge-observe.exporters.console` so command output is structured and readable:

- run banners
- run summaries
- cost panels
- topology trees
- evolution proposal rendering

### Graceful degradation

Where the underlying packages degrade gracefully, the CLI generally mirrors that behavior:

- missing optional extras produce hints instead of hard crashes where possible
- memory ingest or evolution can be skipped with a visible message
- diagnostics show optional modules as warnings rather than fatal errors

## Public surface

Entry module:

- `forge_cli.main`

Command modules:

- `forge_cli.commands.wrap`
- `forge_cli.commands.run`
- `forge_cli.commands.memory`
- `forge_cli.commands.observe`
- `forge_cli.commands.doctor`
- `forge_cli.commands.evolve`

Installed script:

- `forge`

## Package boundaries

`forge-cli` is the operational shell around the rest of the project.

It does not own the core runtime logic itself:

- `forge-core` owns the orchestrator, types, events, guards, and evolution loop
- `forge-adapters` owns framework-specific execution integration
- `forge-memory` owns storage and retrieval
- `forge-observe` owns cost tracking, tracing, console rendering, and the observe API

That split is important because it keeps the CLI ergonomic without making it the single source of truth.

## Examples of current behavior

Claims in this README are intentionally scoped to what the code implements today:

- `forge observe` starts an API server and exposes docs and SSE endpoints
- `forge doctor` performs local import and backend checks, with optional network checks
- `forge memory` uses the default local `HybridMemory`
- `forge wrap` can optionally start the observe API, ingest to memory, and attempt evolution
- `forge evolve status` reads from a journal file and renders a summary table

This README does **not** claim:

- a complete first-party browser dashboard ships in `forge-cli`
- every evolution run will apply mutations automatically
- every wrapped run persists memory through orchestrator-attached memory integration

Where behavior is conditional, the README keeps that conditional language on purpose.

## Testing

From the repository root:

```bash
pytest packages/forge-cli/tests -q
```

The current package tests cover:

- `forge doctor` output and stability
- `forge evolve status` rendering and empty-journal behavior
- breaker-open journal rendering for `forge evolve status`

## License

Apache-2.0

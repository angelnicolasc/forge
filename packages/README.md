<div align="center">

# Forge Packages

**A quick map of the Forge monorepo.**

If the root README is the product overview, this directory is the source map: each package has a narrow job, and together they make up the full Forge stack.

</div>

<p align="center">
  <a href="https://github.com/angelnicolasc/forge">Repository</a> ·
  <a href="https://github.com/angelnicolasc/forge/tree/main#quickstart">Quick Start</a> ·
  <a href="https://github.com/angelnicolasc/forge/tree/main/packages/forge-core">forge-core</a> ·
  <a href="https://github.com/angelnicolasc/forge/tree/main/packages/forge-adapters">forge-adapters</a> ·
  <a href="https://github.com/angelnicolasc/forge/tree/main/packages/forge-memory">forge-memory</a> ·
  <a href="https://github.com/angelnicolasc/forge/tree/main/packages/forge-observe">forge-observe</a> ·
  <a href="https://github.com/angelnicolasc/forge/tree/main/packages/forge-cli">forge-cli</a>
</p>

---

## TL;DR

Forge is split into focused packages instead of one large blob:

- `forge-core` is the runtime brain
- `forge-adapters` is the framework compatibility layer
- `forge-memory` is the hybrid memory system
- `forge-observe` is observability and FinOps
- `forge-cli` is the terminal front door
- `forge-os` is the meta-package that installs the full stack

If you are new here and want the shortest path:

1. Start with [`forge-cli`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-cli) if you want to run Forge.
2. Start with [`forge-core`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-core) if you want to understand the architecture.
3. Start with [`forge-adapters`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-adapters) if you want to see how frameworks plug in.

## Package guide

### [`forge-core`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-core)

The orchestration engine behind Forge.

This package defines the contracts the rest of the system builds on:

- `MetaOrchestrator`
- shared types like `TaskEnvelope`, `RunResult`, and `RunEvent`
- protocol-based extension points
- event bus
- runtime guards
- topology snapshots
- self-evolution loop primitives

Read this first if you want the architectural center of gravity.

### [`forge-adapters`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-adapters)

Drop-in wrappers for supported frameworks and generic callables.

Current adapter families:

- LangGraph
- CrewAI
- AutoGen
- generic Python callables

Read this first if you care about framework integration and execution compatibility.

### [`forge-memory`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-memory)

The living collaborative memory layer.

Current implementation includes:

- vector retrieval
- graph retrieval
- symbolic filtering
- versioning and provenance
- context injection hooks

Read this first if you care about cross-run memory and retrieval behavior.

### [`forge-observe`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-observe)

Observability and cost tracking for Forge.

Current scope includes:

- LLM call interception
- OpenTelemetry tracing
- metrics aggregation
- pricing-aware cost calculation
- REST + SSE observe API
- Rich console rendering

Read this first if you care about telemetry, runtime visibility, and FinOps.

### [`forge-cli`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-cli)

The command-line interface.

Current command groups include:

- `forge wrap`
- `forge run`
- `forge memory`
- `forge observe`
- `forge doctor`
- `forge evolve`

Read this first if you want the most practical, hands-on entrypoint.

### [`forge-os`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-os)

The meta-package.

Its job is simple: install the full Forge stack through one package name rather than making users compose the package graph manually.

Read this if you want installation semantics rather than subsystem internals.

## How the pieces fit together

```text
forge-cli
   |
   v
forge-core
   |
   +--> forge-adapters
   +--> forge-memory
   +--> forge-observe

forge-os
   |
   +--> installs the full stack
```

This is intentionally simplified, but it is directionally accurate:

- `forge-cli` is the user-facing surface
- `forge-core` is the runtime center
- adapters, memory, and observability plug into that runtime
- `forge-os` is the packaging convenience layer

## Which README to read next

- If you want to understand the system: read [`forge-core`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-core)
- If you want to run something now: read [`forge-cli`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-cli)
- If you want to integrate a framework: read [`forge-adapters`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-adapters)
- If you want memory: read [`forge-memory`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-memory)
- If you want telemetry: read [`forge-observe`](https://github.com/angelnicolasc/forge/tree/main/packages/forge-observe)

## Installation

Most users should install the full stack:

```bash
pip install forge-os
```

If you only want one subsystem, install the package directly from its own README.

## Notes on scope

This README is intentionally introductory.

It is not meant to replace the package READMEs. Its job is to make the package layout easy to digest for someone landing in `packages/` and trying to orient themselves quickly.

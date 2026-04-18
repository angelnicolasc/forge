<div align="center">

<h1>⚡ Forge</h1>
<p><strong>Universal Agent Harness — Drop your agents. Watch them evolve, remember and win.</strong></p>

[![CI](https://github.com/forge-ai/forge/actions/workflows/ci.yml/badge.svg)](https://github.com/forge-ai/forge/actions/workflows/ci.yml)
[![PyPI version](https://badge.fury.io/py/forge-os.svg)](https://badge.fury.io/py/forge-os)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-orange.svg)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-Join-7289DA.svg)](https://discord.gg/forge-ai)
[![Docs](https://img.shields.io/badge/docs-forge--ai.dev-teal.svg)](https://forge-ai.dev)

<br/>

> **2026 is not the year of models. It's the year of harnesses.**
> The model is commodity. The harness is the moat.

</div>

---

## What is Forge?

Forge is the first **open-source, enterprise-grade harness** that transforms any multi-agent flow into a **self-evolving, memory-powered, production-observable system** — in one command.

> Every feature claim below maps to a test — see [docs/feature-map.md](docs/feature-map.md). Items not yet shipped live in the [Roadmap](#roadmap) section at the bottom.

```bash
pip install forge-os
forge wrap my_langgraph_flow.py --input '{"query": "What is RAG?"}'
```

**47 seconds later:**

```
⚡ FORGE — Universal Agent Harness
  Drop your agents. Watch them evolve, remember and win.

✓ Detected: langgraph  (11ms)

Running task_id=a3f8b2c1...

╭─ Run Info ──────────────────────────────────────╮
│ Status    ✓ COMPLETED                           │
│ Task ID   a3f8b2c1d4e5...                       │
│ Duration  4.2s                                  │
╰─────────────────────────────────────────────────╯

╭─ Cost Breakdown ────────────────────────────────╮
│ agent: researcher   $0.00420                    │
│ agent: writer       $0.00180                    │
│ TOTAL               $0.00600                   │
╰─────────────────────────────────────────────────╯

╭─ Agent Topology ────────────────────────────────╮
│ ⚙ Agent Topology                               │
│ ├── [research] Researcher  (claude-sonnet)      │
│ └── [writing]  Writer      (claude-haiku)       │
╰─────────────────────────────────────────────────╯
```

---

## Why Forge?

| | Build from scratch | Forge |
|---|:---:|:---:|
| Wrap any framework | ✗ Manual | ✓ 1 command |
| Real-time cost tracking | ✗ Manual | ✓ Per-agent |
| Self-optimization loop | ✗ You | ✓ Opt-in, snapshot-safe |
| Cross-run memory | ✗ None | ✓ Hybrid KB |
| Enterprise audit trail | ✗ Logs | ✓ Provenance |
| Production-ready | ✗ Weeks | ✓ Day 1 |

---

## Features

### 🎯 Universal Drop-in Adapter
Wraps **LangGraph**, **CrewAI**, **AutoGen**, or any async callable. No migration required. Auto-detects your framework from imports.

### 🧬 Self-Evolution Loop
Every run generates telemetry. The evolution loop analyzes it and proposes (or auto-applies) mutations:
- **Prompt rewrites** — fix recurring error patterns via LLM-structured output (`PromptRewriteMutator`)
- **Model swaps** — downgrade expensive models for simple tasks
- **Agent culling** — remove dead agents from the topology, with atomic rollback from a `TopologySnapshot` on regression
- **Parameter tuning** — adjust timeouts, max steps

Auto-trigger is **off by default**; enable with `FORGE_ENABLE_EVOLUTION_AUTO=1`. Three consecutive failed mutations open a breaker and suspend the loop until an operator calls `orchestrator.resume_evolution()`.

```bash
forge evolve my_flow.py --mode auto
# ⚗  Evolution proposed: Swap researcher from claude-opus → claude-sonnet
# ↳ Applied automatically. Fitness: 0.72 → 0.91 (+0.19)
```

### 🧠 Living Collaborative Memory
**The feature nobody else has production-ready.** A hybrid knowledge base that:
- **Vector layer** (ChromaDB): semantic search across all agent outputs
- **Graph layer** (NetworkX/Neo4j): entity-relationship traversal with temporal validity
- **Symbolic layer**: forward-chaining rule engine for deterministic business rules that override statistical retrieval
- **Full provenance**: every entry has creator, version, and evidence chain

```bash
forge memory query "optimization techniques the researcher found last week"
forge memory ingest my_document.txt --tag topic=RAG
```

### 📊 Production Observability + FinOps
- OpenTelemetry tracing with per-agent spans
- Real-time cost breakdown by agent and model
- Budget enforcement (`--budget 1.50`)
- OTLP wiring via `opentelemetry-sdk` — point it at any OTLP-compatible collector (Grafana Tempo, Datadog, Jaeger) _(see [Roadmap](#roadmap) for a first-party integration test)_
- REST + SSE API for the dashboard (`forge observe`). The Next.js dashboard itself is on the [Roadmap](#roadmap) — v0.1.0 ships the backend only.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     forge wrap my_flow.py                    │
└────────────────────────────┬─────────────────────────────────┘
                             │
                  ┌──────────▼──────────┐
                  │   MetaOrchestrator  │   forge-core
                  │   Auto-detect +     │
                  │   Instrument + Run  │
                  └──┬──────────────────┘
                     │
       ┌─────────────┼──────────────────────┐
       │             │                      │
  ┌────▼────┐  ┌─────▼──────┐  ┌───────────▼──────────────┐
  │ Adapter │  │  Observe   │  │  Living Collaborative    │
  │         │  │ OTel+RichUI│  │  Memory                  │
  │ Lang    │  │ FinOps API │  │  Vector+Graph+Symbolic   │
  │ Graph / │  └────────────┘  │  Versioned+Provenance    │
  │ CrewAI /│                  └──────────────────────────┘
  │ AutoGen │       │
  └─────────┘  ┌────▼─────────────────────┐
               │   Evolution Loop         │   forge-core
               │  Observe → Hypothesize   │
               │  → Mutate → Evaluate     │
               │  → Commit / Rollback     │
               └──────────────────────────┘
```

---

## Installation

```bash
# Core + CLI
pip install forge-os

# With specific adapters
pip install 'forge-os[langgraph]'
pip install 'forge-os[crewai]'
pip install 'forge-os[autogen]'
pip install 'forge-os[all]'      # everything
```

**Requirements**: Python 3.11+, no Docker required for dev.

---

## Quickstart

### 1. Wrap an existing flow

```python
# my_flow.py
from langgraph.graph import StateGraph, END
from typing import TypedDict

class State(TypedDict):
    query: str
    answer: str

def researcher(state: State) -> State:
    return {"answer": f"Research: {state['query']}"}

graph = StateGraph(State)
graph.add_node("researcher", researcher)
graph.set_entry_point("researcher")
graph.add_edge("researcher", END)
app = graph.compile()
```

```bash
forge wrap my_flow.py --input '{"query": "What is RAG?"}'
```

### 2. Enable memory + evolution

```bash
forge wrap my_flow.py \
  --input '{"query": "Latest agent harness papers"}' \
  --evolution \
  --dashboard
```

### 3. Use the Python SDK

```python
import asyncio
from forge_core.harness import MetaOrchestrator
from forge_core.types import TaskEnvelope

async def main():
    orchestrator = MetaOrchestrator()
    await orchestrator.load("my_flow.py")

    result = await orchestrator.run(
        TaskEnvelope(input={"query": "What is Forge?"})
    )
    print(f"Cost: ${result.cost.total_cost:.5f}")
    print(f"Duration: {result.duration_ms:.0f}ms")
    print(f"Output: {result.output}")

asyncio.run(main())
```

---

## Packages

| Package | Description | PyPI |
|---------|-------------|------|
| `forge-core` | Types, protocols, MetaOrchestrator, evolution loop | [![PyPI](https://badge.fury.io/py/forge-core.svg)](https://badge.fury.io/py/forge-core) |
| `forge-memory` | Living Collaborative Memory (vector+graph+symbolic) | [![PyPI](https://badge.fury.io/py/forge-memory.svg)](https://badge.fury.io/py/forge-memory) |
| `forge-adapters` | LangGraph, CrewAI, AutoGen adapters | [![PyPI](https://badge.fury.io/py/forge-adapters.svg)](https://badge.fury.io/py/forge-adapters) |
| `forge-observe` | OpenTelemetry + FinOps + Dashboard API | [![PyPI](https://badge.fury.io/py/forge-observe.svg)](https://badge.fury.io/py/forge-observe) |
| `forge-cli` | The `forge` command | [![PyPI](https://badge.fury.io/py/forge-cli.svg)](https://badge.fury.io/py/forge-cli) |
| `forge-os` | Meta-package: installs everything | [![PyPI](https://badge.fury.io/py/forge-os.svg)](https://badge.fury.io/py/forge-os) |

---

## Roadmap

- [x] Phase 1: Universal adapter + cost tracking + CLI (47-second demo)
- [x] Phase 2: Multi-framework + Living Memory
- [x] Phase 3: Self-evolution loop + dashboard
- [ ] Phase 4: Production backends (Neo4j, Qdrant), Helm chart, RBAC
- [ ] Agent-to-agent payments (A2A protocol)
- [ ] Cross-org memory federation
- [ ] Forge Cloud (managed enterprise)

---

## Roadmap

v0.1.0 is **backend + CLI only**. The items below are intentionally
out of scope for this tag and are tracked as first-week / first-month
hotfixes. Anything in this list that graduates to a shipped feature
must land with a test row in
[docs/feature-map.md](docs/feature-map.md).

- **Next.js dashboard** (`apps/forge-dashboard/`) — REST + SSE backend is live in v0.1.0; the UI ships in v0.2.0.
- **First-party OTLP integration test** — OTLP export is wired via `opentelemetry-sdk`; we add a collector-level smoke test in v0.1.x.
- **Neo4j + Qdrant backends** — scaffolding is in `forge-memory/`; production-hardened adapters ship in v0.2.x.
- **RBAC / multi-tenancy** — v0.2.x.
- **`forge doctor`** — environment diagnostics (Python version, installed extras, network egress). Ships in v0.1.1.

---

## Community

- **Discord**: [discord.gg/forge-ai](https://discord.gg/forge-ai) — #show-your-evolution
- **X**: [@forgeharness](https://x.com/forgeharness)
- **Docs**: [forge-ai.dev](https://forge-ai.dev)
- **Contributing**: [CONTRIBUTING.md](CONTRIBUTING.md)

---

## License

Apache 2.0 — enterprise-friendly, commercial use allowed.

---

<div align="center">
<sub>Built with ⚡ by the Forge contributors. Drop your agents. Watch them evolve.</sub>
</div>

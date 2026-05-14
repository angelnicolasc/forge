<div align="center">
  <img width="1500" height="276" alt="graymatter-banner" src=".github/assets/forge_banner.jpg" />
</div>



<div align="center">

<p><strong>Universal Agent Harness — Drop your agents. Watch them evolve, remember and win.</strong></p>

[![CI](https://github.com/angelnicolasc/forge/actions/workflows/ci.yml/badge.svg)](https://github.com/angelnicolasc/forge/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-orange.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-260%20passing-brightgreen.svg)](#)
[![Coverage](https://img.shields.io/badge/coverage-%E2%89%A575%25-brightgreen.svg)](#)

<br/>

</div>

---

## What is Forge?

Forge is an **open-source, enterprise-grade harness** that wraps any multi-agent flow and gives it real-time cost tracking, cross-run memory, OpenTelemetry tracing, and an opt-in self-evolution loop — in one command.

> Every feature bullet below maps to a test. See [docs/feature-map.md](docs/feature-map.md). Items not yet shipped live in the [Roadmap](#roadmap) at the bottom.

```bash
pip install forge-os
forge wrap my_langgraph_flow.py --input '{"query": "What is RAG?"}'
```

**A few seconds later:**

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
│ TOTAL               $0.00600                    │
╰─────────────────────────────────────────────────╯

╭─ Agent Topology ────────────────────────────────╮
│ ⚙ Agent Topology                                │
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
| Audit trail | ✗ Logs | ✓ Provenance |
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

Auto-trigger is **off by default**; enable with `FORGE_ENABLE_EVOLUTION_AUTO=1`. Three consecutive failed mutations open a breaker and suspend the loop until an operator calls `forge evolve resume <source>` or `orchestrator.resume_evolution()`.

```bash
forge evolve run my_flow.py --mode auto
# ⚗  Evolution proposed: Swap researcher from claude-opus → claude-sonnet
# ↳ Applied automatically. Fitness: 0.72 → 0.91 (+0.19)

forge evolve status   # read the journal
forge evolve resume my_flow.py   # re-arm the breaker
```

### 🧠 Living Collaborative Memory
A hybrid knowledge base that persists knowledge across runs:
- **Vector layer** (ChromaDB): semantic search across all agent outputs
- **Graph layer** (NetworkX, SQLite-backed with WAL): entity-relationship traversal with temporal validity
- **Symbolic layer**: forward-chaining rule engine for deterministic business rules that override statistical retrieval
- **Full provenance**: every entry has creator, version, and evidence chain
- **Vector-clock conflict resolution** for concurrent writes

```bash
forge memory query "optimization techniques the researcher found last week"
forge memory ingest my_document.txt --tag topic=RAG
```

### 📊 Observability + FinOps
- OpenTelemetry tracing with per-agent spans
- Real-time cost breakdown by agent and model (pricing table shipped; unknown models warn loudly, they don't silently cost $0)
- Budget enforcement (`--budget 1.50`) — runs are cancelled when the ceiling is hit
- OTLP wiring via `opentelemetry-sdk` — point it at any OTLP-compatible collector (Grafana Tempo, Datadog, Jaeger)
- REST + SSE API for metrics and run traces (`forge observe`) — see [Roadmap](#roadmap) for the web UI

### 🩺 `forge doctor`
Diagnose a local install in one command: Python version, installed adapter extras, default memory backends, and optional network egress (`--network`).

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

Full write-up in [docs/architecture.md](docs/architecture.md).

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

**Requirements**: Python 3.11+. No Docker required for dev.

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
  --evolution
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

Forge ships as eleven composable packages. Installing `forge-os` pulls the core set; optional extras add the rest.

| Package | Description |
|---------|-------------|
| `forge-os-core` | Types, protocols, `MetaOrchestrator`, evolution loop + FSM |
| `forge-os-memory` | Living Collaborative Memory (vector + graph + symbolic) |
| `forge-os-adapters` | LangGraph, CrewAI, AutoGen, generic-callable adapters |
| `forge-os-observe` | OpenTelemetry tracing, FinOps cost model, REST + SSE API |
| `forge-os-cli` | The `forge` command (`wrap`, `run`, `evolve`, `memory`, `doctor`, `observe`) |
| `forge-os-mcp` | Model Context Protocol server and client bridges |
| `forge-rules` | Context-aware rule packs with conflict resolution |
| `forge-skills` | Intent-based skill dispatch and SKILL.md loading |
| `forge-spec` | Spec-driven acceptance criteria and SLSA Level 2 attestation |
| `forge-review` | Parallel review agents and severity-based policy gates |
| `forge-os` | Meta-package: installs everything |

---

## Environment Variables

Feature flags and runtime configuration via environment variables.

| Variable | Default | Description |
|---|---|---|
| `FORGE_ENABLE_EVOLUTION_AUTO` | `0` | Auto-apply evolution mutations without human approval. Off by default; enable only in trusted environments. |
| `FORGE_ENABLE_LLM_JUDGE` | `0` | Evaluate run quality with an LLM call after each run. **Adds ~1 LLM call per run.** Model configurable via `FORGE_LLM_JUDGE_MODEL` (default: `claude-haiku-4-20250514`). Expected cost: ~$0.001/evaluation at Haiku pricing. |
| `FORGE_LLM_JUDGE_MODEL` | `claude-haiku-4-20250514` | Model used by the LLM-as-judge scorer when `FORGE_ENABLE_LLM_JUDGE=1`. |
| `FORGE_LLM_CIRCUIT_BREAKER` | `1` | Enable the LLM circuit breaker (trips after 5 consecutive failures; 30s recovery). Set to `0` to disable in tests. |
| `FORGE_OBSERVE_MAX_SSE_SUBSCRIBERS` | `50` | Maximum concurrent SSE subscribers on `/api/v1/stream`. Returns HTTP 503 beyond this limit. |
| `FORGE_API_ALLOWED_ORIGINS` | `` | Comma-separated CORS origins for the observe API (e.g. `http://localhost:5173`). Empty = same-origin only. |
| `FORGE_PRICING_TABLE_PATH` | `` | Path to a JSON pricing table that overrides the bundled prices. Useful when model prices change before a new release ships. |

---

## Roadmap

v0.1.0 is **backend + CLI only**. The items below are out of scope for this tag
and tracked as follow-up releases. Anything that graduates to a shipped feature
must land with a test row in [docs/feature-map.md](docs/feature-map.md).

- **Web dashboard UI** — the REST + SSE backend ships in v0.1.0; a web UI is planned for v0.2.0.
- **First-party OTLP collector integration test** — OTLP export is wired via `opentelemetry-sdk`; a collector-level smoke test lands in v0.1.x.
- **Production memory backends** (Neo4j, Qdrant) — scaffolding is in `forge-memory/`; production-hardened adapters ship in v0.2.x.
- **RBAC / multi-tenancy** — v0.2.x.
- **Agent-to-agent payments (A2A)** — exploratory.

---

## Contributing

Issues, bug reports, and pull requests are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) and open an issue at
[github.com/angelnicolasc/forge/issues](https://github.com/angelnicolasc/forge/issues).

Security reports: please read [SECURITY.md](SECURITY.md).

---

## License

Apache 2.0 — enterprise-friendly, commercial use allowed. See [LICENSE](LICENSE).

---

<div align="center">
<sub>Built by <a href="https://github.com/angelnicolasc">Angel DiCerutti</a>. Drop your agents. Watch them evolve.</sub>
</div>

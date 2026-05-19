<div align="center">
  <img width="1500" height="276" alt="forge-banner" src=".github/assets/forge_banner.jpg" />
</div>

<div align="center">

<p><strong>Universal Agent Harness — Drop your agents. Watch them evolve, remember and win.</strong></p>

[![CI](https://github.com/angelnicolasc/forge/actions/workflows/ci.yml/badge.svg)](https://github.com/angelnicolasc/forge/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/forge-os.svg)](https://pypi.org/project/forge-os/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-orange.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-420%2B%20passing-brightgreen.svg)](#)
[![Coverage](https://img.shields.io/badge/coverage-%E2%89%A577%25-brightgreen.svg)](#)

<br/>

</div>

---

## What is Forge?

Forge is an **open-source, enterprise-grade agent harness** that wraps any multi-agent flow and gives it real-time cost tracking, cross-run memory, OpenTelemetry tracing, spec-driven governance, and an opt-in self-evolution loop — in one command.

> Every feature in this document maps to a test. See [docs/feature-map.md](docs/feature-map.md). Items not yet shipped live in the [Roadmap](#roadmap).

```bash
pip install forge-os
forge wrap my_langgraph_flow.py --input '{"query": "What is RAG?"}'
```

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
│ ├── [research] Researcher  (claude-sonnet)      │
│ └── [writing]  Writer      (claude-haiku)       │
╰─────────────────────────────────────────────────╯
```

---

## Why Forge?

| | Build from scratch | Forge |
|---|:---:|:---:|
| Wrap any framework | ✗ Manual | ✓ 1 command |
| Real-time cost tracking | ✗ Manual | ✓ Per-agent, per-model |
| Self-optimization loop | ✗ You build it | ✓ Opt-in, snapshot-safe |
| Cross-run memory | ✗ None | ✓ Hybrid vector + graph + symbolic |
| Tool governance (MCP) | ✗ None | ✓ 8-layer pipeline |
| Spec-driven quality gates | ✗ Manual | ✓ SLSA Level 2 attestation |
| Audit trail | ✗ Log files | ✓ Full provenance chain |
| Production-ready | ✗ Weeks | ✓ Day 1 |

---

## Features

### 🎯 Universal Drop-in Adapter

Wraps **LangGraph**, **CrewAI**, **AutoGen**, or any async callable. No migration required. Framework is auto-detected from imports.

### 🧬 Self-Evolution Loop

Every run generates telemetry. The evolution loop analyzes it and proposes (or auto-applies) mutations:
- **Prompt rewrites** — fix recurring error patterns via LLM-structured output (`PromptRewriteMutator`)
- **Model swaps** — downgrade expensive models for lower-complexity tasks (`ModelSwapMutator`)
- **Agent culling** — remove underperforming agents with atomic rollback via `TopologySnapshot` (`AgentCullMutator`)
- **Parameter tuning** — adjust timeouts, max steps, thresholds (`ParameterTuneMutator`)

Auto-trigger is **off by default**. Three consecutive failed mutations open a circuit breaker and suspend the loop until an operator calls `forge evolve resume` or `orchestrator.resume_evolution()`.

```bash
forge evolve run my_flow.py --mode auto
# ⚗  Proposed: Swap researcher from claude-opus → claude-sonnet
# ↳ Applied. Fitness: 0.72 → 0.91 (+0.19)

forge evolve status          # read the journal
forge evolve resume my_flow.py   # re-arm the breaker
```

### 🧠 Living Collaborative Memory

A hybrid knowledge base that accumulates and retrieves knowledge across runs:
- **Vector layer** (ChromaDB + sentence-transformers): semantic search over all agent outputs
- **Graph layer** (NetworkX, SQLite-backed WAL): entity-relationship traversal with temporal validity
- **Symbolic layer**: forward-chaining rule engine for deterministic business rules that override statistical retrieval
- **Reciprocal Rank Fusion**: unified scoring across all three layers via `HybridMemory`
- **Full provenance**: every entry carries creator, version, and evidence chain
- **Vector-clock conflict resolution** for concurrent writes

```bash
forge memory query "optimization techniques the researcher found last week"
forge memory ingest my_document.txt --tag topic=RAG
forge memory status
```

### 📊 Observability + FinOps

- OpenTelemetry tracing with per-agent spans — wire to any OTLP-compatible collector (Grafana Tempo, Datadog, Jaeger)
- Real-time cost breakdown by agent and model — thinking tokens and cached input tokens tracked separately
- Three-tier pricing resolution: `FORGE_PRICING_TABLE_PATH` → `~/.forge/pricing.json` (7-day TTL) → bundled table; unknown models warn loudly instead of silently billing $0
- Budget enforcement (`--budget 1.50`) — runs cancelled at the ceiling
- REST + SSE API for live metrics and run traces

```bash
forge observe                  # open the live dashboard
forge doctor                   # diagnose your installation
forge doctor update-pricing    # refresh the local pricing cache
```

### 🔧 MCP Meta-Orchestrator

Routes, secures, and budgets all Model Context Protocol tool calls through an 8-layer governance pipeline:
1. Tool routing (O(1) bidirectional index)
2. Policy enforcement (deny-by-default allowlist per caller)
3. Per-tool call-count budget
4. TTL result cache (SHA-256 keyed, normalized arg order)
5. Circuit breaker
6. Upstream dispatch
7. Secret redaction (API keys, JWTs, PATs, Bearer tokens)
8. Post-call telemetry

```bash
forge mcp list-tools           # show available tools
forge mcp call <tool> <args>   # invoke a tool
forge mcp serve                # expose Forge memory + evolution as MCP tools
forge mcp status               # inspect budget and cache state
```

### 📋 Rules Engine

Context-aware rule packs with a fixed precedence lattice and compile-time scope analysis:
- **Deny > Require > Suggest** — precedence enforced at merge time, not at inference
- **Scope intersection detection** — overlapping glob patterns flagged at lint time (O(N²), not per-request)
- **YAML round-trip serialization** — author, version, and diff rule packs as code
- Conflict resolution strategies: `MOST_SPECIFIC_WINS` or `PRIORITY_FIRST_MATCH`

```bash
forge rules validate my_rules.yaml
forge rules lint my_rules.yaml       # find scope overlaps
forge rules explain --path "src/**"  # show which rules apply
forge rules diff v1.yaml v2.yaml
```

### 🎓 Skills Runtime

Intent-based skill dispatch with structured SKILL.md loading:
- **SKILL.md format**: YAML frontmatter with name, description, examples, parameters, tools, timeout, budget, tags — compatible with Claude Code skill ecosystem
- **IntentDispatcher**: dual-threshold confidence enforcement before invocation
- **Child scope isolation**: each skill invocation gets its own `agent_id` scoped to the parent `run_id`
- **Call-count budgets** per skill, configurable timeouts via `asyncio.wait_for`

```bash
forge skills list              # show loaded skills
forge skills explain <name>    # describe parameters and examples
forge skills run <name> <args> # invoke a skill
```

### 📐 Spec-Driven Development

Structured acceptance criteria, deterministic attestation, and evolution guardrails:
- **SpecVerifier**: ASSERTION, REGEX, CONTAINS checks with per-criterion weighting
- **SpecAttester**: SLSA Level 2-inspired provenance with canonical SHA-256 spec hash
- **SpecConstraintGuard**: intercepts evolution mutations and raises `ConstraintViolationError` before any spec-breaking change is applied
- YAML authoring with full round-trip serialization

```bash
forge spec init my_feature.yaml    # scaffold a new spec
forge spec verify --spec my_feature.yaml --result run.json
forge spec attest --spec my_feature.yaml   # generate provenance
```

### 🔍 Review Agents & Policy Gates

Parallel review pipeline with severity-based blocking:
- **P0** — blocks unconditionally (security, data loss)
- **P1** — blocks by default (quality gates)
- **P2–P3** — observational findings
- **CascadingReviewer**: heuristic reviewers run first; LLM reviewers are skipped if blocking findings already exist (saves tokens)
- **Four lifecycle hooks**: `on_plan`, `pre_apply`, `pre_stop`, `pre_merge`
- Bundled reviewers: `ErrorRateReviewer`, `CostCeilingReviewer`, `SpecConstraintReviewer`, `StatusReviewer`

```bash
forge review run --context run.json
forge review show --id <review_id>
```

### 🩺 forge doctor

Diagnose a local install in one command: Python version, installed adapter extras, active memory backends, environment variable state, and optional network egress (`--network`).

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│                        forge wrap my_flow.py                          │
└───────────────────────────────┬───────────────────────────────────────┘
                                │
                     ┌──────────▼──────────┐
                     │   MetaOrchestrator  │   forge-os-core
                     │   Detect + Instrument│
                     │   + EventBus + Run  │
                     └──┬──────────────────┘
                        │
        ┌───────────────┼──────────────────────┐
        │               │                      │
   ┌────▼────┐    ┌──────▼──────┐   ┌──────────▼───────────────┐
   │ Adapter │    │   Observe   │   │  Living Collaborative    │
   │         │    │ OTel + Cost │   │  Memory                  │
   │ Lang    │    │ REST + SSE  │   │  Vector+Graph+Symbolic   │
   │ Graph / │    └─────────────┘   │  HybridMemory + RRF      │
   │ CrewAI /│                      └──────────────────────────┘
   │ AutoGen │
   └─────────┘
        │
   ┌────▼────────────────────────────────────────────────────────┐
   │   Governance Layer                                          │
   │                                                             │
   │  ┌────────────┐  ┌──────────────┐  ┌──────────────────┐   │
   │  │ forge-rules│  │  forge-spec  │  │  forge-review    │   │
   │  │ Deny/Req/  │  │ SpecVerifier │  │ P0–P3 severity   │   │
   │  │ Suggest    │  │ + Attester   │  │ CascadingReviewer│   │
   │  └────────────┘  └──────────────┘  └──────────────────┘   │
   │                                                             │
   │  ┌────────────┐  ┌──────────────┐                          │
   │  │ forge-mcp  │  │ forge-skills │                          │
   │  │ 8-layer    │  │ IntentDispatch│                         │
   │  │ pipeline   │  │ + SKILL.md   │                          │
   │  └────────────┘  └──────────────┘                          │
   └─────────────────────────────────────────────────────────────┘
        │
   ┌────▼─────────────────────────┐
   │   Evolution Loop             │   forge-os-core
   │  Observe → Hypothesize       │
   │  → Mutate → Evaluate         │
   │  → Commit / Rollback         │
   └──────────────────────────────┘
```

Full write-up in [docs/architecture.md](docs/architecture.md).

---

## Installation

```bash
# Core harness + CLI (LangGraph, CrewAI, AutoGen adapters require extras)
pip install forge-os

# With framework adapters
pip install 'forge-os[langgraph]'
pip install 'forge-os[crewai]'
pip install 'forge-os[autogen]'
pip install 'forge-os[all]'        # all adapters

# Governance and tooling extras
pip install 'forge-os[rules]'      # context-aware rule packs
pip install 'forge-os[mcp]'        # MCP meta-orchestrator
pip install 'forge-os[skills]'     # skills runtime
pip install 'forge-os[spec]'       # spec-driven development
pip install 'forge-os[review]'     # review agents + policy gates
```

**Requirements**: Python 3.11+. No Docker required for local development.

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

### 2. Enable memory and evolution

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

### 4. Add a spec and enforce it through evolution

```bash
forge spec init acceptance.yaml
# edit acceptance.yaml — define ASSERTION/REGEX/CONTAINS criteria

forge wrap my_flow.py --spec acceptance.yaml --evolution
# SpecConstraintGuard now blocks any mutation that would violate the spec
```

---

## Packages

Forge ships as eleven composable packages. Installing `forge-os` pulls the core set; optional extras add the governance and tooling layers.

| Package | PyPI | Description |
|---------|------|-------------|
| `forge-os-core` | [![PyPI](https://img.shields.io/pypi/v/forge-os-core.svg)](https://pypi.org/project/forge-os-core/) | Types, protocols, `MetaOrchestrator`, evolution loop + FSM, circuit breaker |
| `forge-os-memory` | [![PyPI](https://img.shields.io/pypi/v/forge-os-memory.svg)](https://pypi.org/project/forge-os-memory/) | Living Collaborative Memory — vector + graph + symbolic with RRF fusion |
| `forge-os-adapters` | [![PyPI](https://img.shields.io/pypi/v/forge-os-adapters.svg)](https://pypi.org/project/forge-os-adapters/) | LangGraph, CrewAI, AutoGen, and generic async callable adapters |
| `forge-os-observe` | [![PyPI](https://img.shields.io/pypi/v/forge-os-observe.svg)](https://pypi.org/project/forge-os-observe/) | OpenTelemetry tracing, three-tier FinOps cost model, REST + SSE API |
| `forge-os-cli` | [![PyPI](https://img.shields.io/pypi/v/forge-os-cli.svg)](https://pypi.org/project/forge-os-cli/) | `forge` command — 10 command groups, 30+ subcommands |
| `forge-os-mcp` | [![PyPI](https://img.shields.io/pypi/v/forge-os-mcp.svg)](https://pypi.org/project/forge-os-mcp/) | MCP meta-orchestrator with 8-layer governance pipeline |
| `forge-rules` | *coming soon* | Context-aware rule packs — Deny/Require/Suggest with conflict resolution |
| `forge-skills` | *coming soon* | Intent-based skill dispatch and SKILL.md loading |
| `forge-spec` | *coming soon* | Spec-driven acceptance criteria and SLSA Level 2 attestation |
| `forge-review` | *coming soon* | Parallel review agents with P0–P3 severity and cascading policy gates |
| `forge-os` | [![PyPI](https://img.shields.io/pypi/v/forge-os.svg)](https://pypi.org/project/forge-os/) | Meta-package — installs core, CLI, adapters, memory, and observe |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FORGE_ENABLE_EVOLUTION_AUTO` | `0` | Auto-apply evolution mutations without human approval. Off by default; enable only in trusted environments. |
| `FORGE_ENABLE_LLM_JUDGE` | `0` | Score run quality with an LLM call after each run. **Adds ~1 LLM call per run.** Expected cost: ~$0.001/evaluation at Haiku pricing. |
| `FORGE_LLM_JUDGE_MODEL` | `claude-haiku-4-20250514` | Model used by the LLM-as-judge scorer when `FORGE_ENABLE_LLM_JUDGE=1`. |
| `FORGE_LLM_CIRCUIT_BREAKER` | `1` | Enable the LLM circuit breaker (trips after 5 consecutive failures; 30s recovery window). |
| `FORGE_ENABLE_MEMORY_INJECTION` | `0` | Inject relevant memory context into each run before agent execution. |
| `FORGE_ENABLE_LLM_ENTITY_EXTRACTION` | `0` | Use an LLM to extract entities for the graph memory layer after each run. |
| `FORGE_INTENT_MODEL` | *(configured)* | Model used by the `IntentClassifier` for skill and rule routing. |
| `FORGE_INTENT_CONFIDENCE_THRESHOLD` | *(configured)* | Minimum confidence score before the intent classifier dispatches a skill. |
| `FORGE_OBSERVE_MAX_SSE_SUBSCRIBERS` | `50` | Maximum concurrent SSE subscribers on the live event stream. Returns HTTP 503 beyond this limit. |
| `FORGE_API_ALLOWED_ORIGINS` | *(empty)* | Comma-separated CORS origins for the observe API. Empty = same-origin only. |
| `FORGE_PRICING_TABLE_PATH` | *(empty)* | Path to a JSON pricing table that overrides the bundled prices. |
| `FORGE_PRICING_TABLE_URL` | *(configured)* | URL used by `forge doctor update-pricing` to refresh the local pricing cache. |
| `FORGE_OBSERVE_AGENT_BASE_URL` | *(empty)* | Base URL published in the A2A Agent Card at `/.well-known/agent.json`. |

---

## Roadmap

**v0.2.0 is live** — the full governance platform (forge-spec, forge-review, forge-rules, forge-skills, forge-os-mcp) ships in this release alongside the core harness, adapters, memory, and observability stack.

Planned for upcoming releases:

- **Web dashboard UI** — the REST + SSE backend is live; a first-party web UI is planned for v0.3.x.
- **Production memory backends** — Neo4j and Qdrant adapter scaffolding exists in forge-os-memory; production-hardened backends land in v0.3.x.
- **First-party OTLP integration test** — OTLP export is wired; a collector-level smoke test against a real Grafana Tempo instance is planned for v0.3.x.
- **RBAC and multi-tenancy** — per-team rule pack isolation and identity-aware budget enforcement, targeted for v0.4.x.
- **Agent-to-agent payments (A2A)** — exploratory; no committed timeline.

Anything that graduates to shipped must land with a row in [docs/feature-map.md](docs/feature-map.md).

---

## Contributing

Issues, bug reports, and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and open an issue at [github.com/angelnicolasc/forge/issues](https://github.com/angelnicolasc/forge/issues).

Security reports: please read [SECURITY.md](SECURITY.md).

---

## License

Apache 2.0 — enterprise-friendly, commercial use allowed. See [LICENSE](LICENSE).

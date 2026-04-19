# Forge

**Universal Agent Harness — Drop your agents. Watch them evolve, remember and win.**

---

!!! tip "2026 is not the year of models. It's the year of harnesses."
    Models are commodity. The harness is the new moat.

## What is Forge?

Forge is the **first open-source, enterprise-grade harness** that:

- **Wraps any framework** (LangGraph, CrewAI, AutoGen, or any async callable) in a single command
- **Self-evolves** — rewrites prompts, swaps models, culls dead agents, and optimizes topology automatically
- **Remembers everything** — Living Collaborative Memory (vector + graph + symbolic) persists knowledge across runs, projects, and teams
- **Ships production-ready** — OpenTelemetry tracing, per-agent FinOps cost tracking, provenance-backed audit trails

## Quickstart

```bash
pip install forge-os
forge wrap my_langgraph_flow.py --input '{"query": "What is RAG?"}'
```

A few seconds later you'll see:

```
⚡ FORGE — Universal Agent Harness

✓ Detected: langgraph  (12ms)

Running task_id=a3f8b2c1...

╭─ Run Info ────────────────────────────────╮
│ Status   ✓ COMPLETED                      │
│ Duration 4.2s                             │
╰───────────────────────────────────────────╯

╭─ Cost Breakdown ──────────────────────────╮
│ agent: researcher   $0.00420              │
│ agent: writer       $0.00180              │
│ TOTAL               $0.00600             │
╰───────────────────────────────────────────╯

╭─ Agent Topology ──────────────────────────╮
│ ⚙ [orchestrator] Supervisor              │
│ ├── [research] Researcher (claude-sonnet) │
│ └── [writing]  Writer (claude-haiku)      │
╰───────────────────────────────────────────╯
```

## Why Forge?

| | Build from scratch | Forge |
|---|---|---|
| Multi-framework support | ✗ Manual | ✓ Drop-in |
| Cost tracking | ✗ Manual | ✓ Real-time |
| Self-optimization | ✗ You | ✓ Autonomous |
| Cross-run memory | ✗ None | ✓ Hybrid KB |
| Enterprise audit trail | ✗ Logs | ✓ Provenance |
| Production-ready | ✗ Weeks | ✓ Day 1 |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    forge wrap my_flow.py                │
└────────────────────────┬────────────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │   MetaOrchestrator  │  (forge-core)
              │   Auto-detects and  │
              │   instruments flow  │
              └──┬──────────────────┘
                 │
    ┌────────────┼────────────────────────┐
    │            │                        │
┌───▼───┐  ┌────▼────┐  ┌───────────────▼─────────────┐
│Adapter│  │Observe  │  │  Living Collaborative Memory │
│       │  │OTel +   │  │  Vector + Graph + Symbolic   │
│Lang   │  │FinOps   │  │  Versioned + Provenance      │
│Graph/ │  │Dashboard│  └─────────────────────────────┘
│CrewAI/│  └─────────┘
│AutoGen│       │
└───────┘  ┌────▼────────────────────┐
           │   Evolution Loop        │
           │   Observe → Hypothesize │
           │   → Mutate → Evaluate   │
           │   → Commit / Rollback   │
           └─────────────────────────┘
```

## Community

- **GitHub**: [github.com/angelnicolasc/forge](https://github.com/angelnicolasc/forge)
- **Issues & feature requests**: [github.com/angelnicolasc/forge/issues](https://github.com/angelnicolasc/forge/issues)

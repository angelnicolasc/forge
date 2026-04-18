# Feature Map

Every feature claim in [`README.md`](../README.md) maps to a concrete test
or an honest roadmap entry. The plan ζ.9 gate is: **no row in the
`Status` column may be 🚧**. If something is partial, it either has a
test covering the shipped slice or moves to the Roadmap section at the
bottom of the README.

Last updated: 2026-04-18 (v0.1.0 launch).

| Feature claim (README) | Primary test | Status |
|---|---|---|
| Wrap LangGraph flows | `packages/forge-adapters/tests/test_crewai_adapter.py`, `packages/forge-core/tests/test_e2e_cost_tracking.py` | ✅ tested |
| Wrap CrewAI flows | `packages/forge-adapters/tests/test_crewai_adapter.py` | ✅ tested |
| Wrap AutoGen flows | `packages/forge-adapters/tests/test_autogen_adapter.py` | ✅ tested |
| Wrap any async callable (Generic) | `packages/forge-adapters/tests/test_generic_adapter.py`, `tests/e2e/test_wrap_demo.py` | ✅ tested |
| Real-time cost tracking, per agent | `packages/forge-core/tests/test_e2e_cost_tracking.py`, `packages/forge-observe/tests/test_interceptor_breaker.py` | ✅ tested |
| Cost model with live pricing table | `packages/forge-observe/tests/test_full_stack_metrics.py` (and `cost_model.py` unit surface) | ✅ tested |
| Budget enforcement (`cost_ceiling` / `--budget`) | `packages/forge-core/tests/test_guards.py` | ✅ tested |
| Prompt-rewrite mutator | `packages/forge-core/tests/test_mutations_v2.py`, `test_evolution_fullcycle.py` | ✅ tested |
| Parameter-tune mutator | `packages/forge-core/tests/test_mutations_v2.py` | ✅ tested |
| Agent-cull mutator (with rollback) | `packages/forge-core/tests/test_mutations_v2.py`, `test_topology_snapshot.py` | ✅ tested |
| Model-swap mutator | `packages/forge-core/tests/test_mutations_v2.py` | ✅ tested |
| Auto-trigger evolution after N runs | `packages/forge-core/tests/test_evolution_auto_trigger.py` | ✅ tested (flag-gated) |
| Evolution loop with snapshot + rollback | `packages/forge-core/tests/test_topology_snapshot.py`, `test_evolution_fullcycle.py` | ✅ tested |
| Auto-suspend evolution after N failed rollbacks | `packages/forge-core/tests/test_evolution_breaker.py` | ✅ tested |
| Vector memory layer (ChromaDB) | `packages/forge-memory/tests/test_memory.py` | ✅ tested |
| Graph memory layer (NetworkX, SQLite-backed) | `packages/forge-memory/tests/test_graph_persistence.py` | ✅ tested |
| Symbolic rule layer (forward-chaining) | `packages/forge-memory/tests/test_memory.py` (symbolic suite) | ✅ tested |
| Memory context injection into next run | `packages/forge-memory/tests/test_injection.py` | ✅ tested (flag-gated) |
| Memory versioning + vector clocks | `packages/forge-memory/tests/test_versioning.py` | ✅ tested |
| LLM-based entity extraction (w/ regex fallback) | `packages/forge-memory/tests/test_ingestion_llm.py` | ✅ tested (flag-gated) |
| LLM-as-judge fitness scorer | `packages/forge-core/tests/test_llm_judge.py` | ✅ tested (flag-gated) |
| OpenTelemetry tracing, nested spans | `packages/forge-core/tests/test_e2e_cost_tracking.py` | ✅ tested |
| Cardinality-safe metrics labels | `packages/forge-observe/tests/test_labels.py` | ✅ tested |
| Circuit breakers (LLM / evolution / memory) | `packages/forge-observe/tests/test_interceptor_breaker.py`, `packages/forge-core/tests/test_evolution_breaker.py`, `packages/forge-memory/tests/test_hybrid_breaker.py` | ✅ tested |
| REST API (`/runs`, `/evolution`, `/memory`) | `packages/forge-observe/tests/test_api.py` | ✅ tested |
| SSE live stream endpoint | `packages/forge-observe/tests/test_api.py::test_stream_generator_registers_and_cleans_up` | ✅ tested |
| `forge wrap` CLI end-to-end demo | `tests/e2e/test_wrap_demo.py` | ✅ tested |
| Next.js dashboard (`apps/forge-dashboard/`) | — | 📋 roadmap (post-v0.1.0) |
| OTLP exporter to Grafana/Datadog | wiring via opentelemetry-sdk; no integration test | 📋 roadmap (0.1.x) |
| Neo4j / Qdrant production backends | adapters stubbed | 📋 roadmap (0.2.x) |
| RBAC / multi-tenant | — | 📋 roadmap (0.2.x) |

## Reading this table

- **✅ tested** — the behavior named in the README has an assertion in
  the linked test and fails loudly on regression. "Flag-gated" means the
  behavior only activates with the named `FORGE_ENABLE_*` env var; the
  default path is also tested.
- **📋 roadmap** — intentionally not in v0.1.0. The README's Roadmap
  section is the single source of truth for expected delivery.

Adding a new feature bullet to the README? Add a row here in the same
PR, or the CI gate will treat the claim as aspirational and the PR is
blocked at review.

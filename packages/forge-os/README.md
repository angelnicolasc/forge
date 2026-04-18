# forge-os

**Meta-package for Forge — the universal agent harness.**

Installing `forge-os` pulls in the five runtime packages together:

- [`forge-core`](../forge-core) — orchestration engine, evolution FSM, types
- [`forge-cli`](../forge-cli) — the `forge` command
- [`forge-observe`](../forge-observe) — tracing, metrics, cost model, dashboard backend
- [`forge-memory`](../forge-memory) — hybrid vector / graph / symbolic memory
- [`forge-adapters`](../forge-adapters) — LangGraph / CrewAI / AutoGen / generic adapters

## Install

```bash
pip install forge-os
```

Framework extras:

```bash
pip install "forge-os[langgraph]"
pip install "forge-os[crewai]"
pip install "forge-os[autogen]"
pip install "forge-os[all]"
```

## Quickstart

```bash
forge wrap examples/langgraph_research/flow.py --input '{"query":"What is RAG?"}'
```

Or use the Python API:

```python
import forge_os
print(forge_os.__version__)

from forge_os import MetaOrchestrator, TaskEnvelope
```

For the full feature matrix and architecture, see the [project README](../../README.md)
and [`docs/architecture.md`](../../docs/architecture.md).

## License

Apache-2.0

# forge-rules

Rules Engine for Forge — context-aware rule packs with fixed-lattice conflict resolution and scope intersection detection at lint time.

## Features

- **Fixed lattice**: `deny > require > suggest` (non-configurable inter-category precedence)
- **Intra-category strategies**: `MOST_SPECIFIC_WINS` (default) and `PRIORITY_FIRST_MATCH` (opt-in)
- **Scope intersection detection**: lint-time detection of overlapping glob patterns (original contribution)
- **YAML authoring**: human-friendly rule pack format with round-trip support
- **A2A/EventBus integration**: emits `CONTEXT_INJECTED` events for KPI tracking
- **CLI**: `forge rules validate`, `lint`, `explain`, `diff`

## Quick start

```python
from forge_rules import RulesEngine, load_yaml

engine = RulesEngine()
engine.load_yaml("rules/python-safety.yaml")

selection = engine.select(path="src/api/views.py", intent="code_review")
context_block = await engine.inject(selection, run_id="run-001")
```

## Part of Forge

This package is part of [Forge](https://github.com/angelnicolasc/forge) — the universal AI agent harness.

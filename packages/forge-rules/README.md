# forge-rules

Part of the earlier Forge agent harness. For the standalone CI check, see [Forge Check](https://pypi.org/project/forge-os-check/).

Rules Engine for Forge — context-aware rule packs with fixed-lattice conflict resolution and scope intersection detection at lint time.

## Features

- **Fixed lattice**: `deny > require > suggest` (non-configurable inter-category precedence)
- **Intra-category strategies**: `MOST_SPECIFIC_WINS` (default) and `PRIORITY_FIRST_MATCH` (opt-in)
- **Scope intersection detection**: tests example paths against scope patterns and reports potential overlaps it finds
- **YAML authoring**: human-friendly rule pack format with round-trip support
- **A2A/EventBus integration**: emits `CONTEXT_INJECTED` events for KPI tracking
- **CLI**: `forge rules validate`, `lint`, `explain`, `diff`

## Quick start

```python
import asyncio

from forge_rules import RulesEngine

engine = RulesEngine()
engine.load_yaml("""
name: review-rules
rules:
  - id: include-test
    action: suggest
    description: Request a test for Python changes
    content: Include a regression test.
    scope: ["**/*.py"]
""")

selection = engine.select(path="src/api/views.py", intent="code_review")
print(asyncio.run(engine.inject(selection, run_id="run-001")))
```

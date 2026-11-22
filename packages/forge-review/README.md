# forge-review

Part of the earlier Forge agent harness. For the standalone CI check, see [Forge Check](https://pypi.org/project/forge-os-check/).

Review Agents and Policy Gates for the Forge agent harness.

Define parallel reviewers (heuristic and LLM-based), cascade them for efficiency,
and gate mutations or run results with severity-based policy decisions (P0–P3).

## Installation

```bash
pip install forge-review
# With spec-constraint reviewer:
pip install 'forge-review[spec]'
```

## Quick start

```python
import asyncio

from forge_core.types import RunResult
from forge_review import ReviewRunner

run = RunResult(task_id="review-demo", output="draft")
result, decision = asyncio.run(ReviewRunner().pre_merge(run))
print(decision.allowed, len(result.findings))
```

## Severity levels

| Level | Meaning | Blocks by default |
|-------|---------|-------------------|
| P0 | Critical — security risk, data loss | Yes |
| P1 | Error — run quality issue | Yes |
| P2 | Warning — informational concern | No |
| P3 | Info | No |

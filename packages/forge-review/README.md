# forge-review

Review Agents and Policy Gates for the Forge agent harness.

Define parallel reviewers (heuristic and LLM-based), cascade them for efficiency,
and gate mutations or run results with severity-based policy decisions (P0–P3).

## Installation

```bash
pip install forge-review
# With spec-constraint reviewer:
pip install forge-review[spec]
```

## Quick start

```python
from forge_review import ReviewRunner

runner = ReviewRunner()
result, decision = await runner.pre_merge(run_result)
if not decision.allowed:
    raise RuntimeError(decision.reason)
print(f"Review passed — {len(result.findings)} finding(s)")
```

## Severity levels

| Level | Meaning | Blocks by default |
|-------|---------|-------------------|
| P0 | Critical — security risk, data loss | Yes |
| P1 | Error — run quality issue | Yes |
| P2 | Warning — informational concern | No |
| P3 | Info | No |

## Technical debt

See DEVLOG.md for DT entries introduced in Phase 5.

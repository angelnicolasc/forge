# forge-spec

Part of the earlier Forge agent harness. For the standalone CI check, see [Forge Check](https://pypi.org/project/forge-os-check/).

Spec-Driven Development for the Forge agent harness.

Define criteria in `spec.yaml`, evaluate them against a `RunResult`, and export an unsigned report of the criteria, results and specification hash.

## Installation

```bash
pip install forge-spec
# With LLM-assisted spec refinement:
pip install 'forge-spec[llm]'
```

The published `forge-spec==0.2.0` wheel can fail when constructing `SpecDef` with Pydantic 2.11.10. That wheel imports the required `MutationKind` only during type checking. The source tree includes the runtime import.

## Verify an output label

This example checks a synthetic harness result for the substring `needs_review`. Its report describes the supplied output. To establish that a ticket reached a queue, collect an observation from the ticket system.

```python
import asyncio

from forge_core.types import RunResult
from forge_spec import AcceptanceCriterion, SpecDef, SpecRunner

spec = SpecDef(
    name="review-label",
    acceptance_criteria=[
        AcceptanceCriterion(
            id="has-review-label",
            description="The run output contains the review label",
            check_type="contains",
            expected="needs_review",
        )
    ],
)
run = RunResult(task_id="ticket-07", output="needs_review")
verification, report = asyncio.run(SpecRunner().run_pipeline(spec, run))
print(report.passed_criteria, report.failed_criteria)  # 1 0
```

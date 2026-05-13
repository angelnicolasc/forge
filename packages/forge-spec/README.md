# forge-spec

Spec-Driven Development for the Forge agent harness.

Define what an agent run must achieve (`spec.yaml`), verify it against real
`RunResult` output, and generate SLSA Level 2-inspired attestations for
compliance and audit trails.

## Installation

```bash
pip install forge-spec
# With LLM-assisted spec refinement:
pip install forge-spec[llm]
```

## Quick start

```python
from forge_spec import SpecDef, AcceptanceCriterion, SpecRunner

spec = SpecDef(
    name="no-show-optimizer",
    description="Reduce appointment no-show rate",
    objectives=["Reduce no-show rate by 15%"],
    acceptance_criteria=[
        AcceptanceCriterion(
            id="ac-001",
            description="Run completes successfully",
            check_type="assertion",
            expected="run_result.status == 'completed'",
        ),
        AcceptanceCriterion(
            id="ac-002",
            description="Output contains recommendations",
            check_type="contains",
            expected="recommendation",
        ),
    ],
)

runner = SpecRunner()
verify_result, attestation = await runner.run_pipeline(spec, run_result)
print(f"Compliance: {verify_result.compliance_rate:.0%}")
```

## spec.yaml format

```yaml
name: no-show-optimizer
version: "0.1.0"
description: Reduce appointment no-show rate
objectives:
  - Reduce no-show rate by at least 15%
constraints:
  - id: no-model-swap
    description: Preserve the primary LLM model during evolution
    mutation_kinds_blocked:
      - model_swap
acceptance_criteria:
  - id: ac-001
    description: Run must complete successfully
    check_type: assertion
    expected: "run_result.status == 'completed'"
    weight: 2.0
  - id: ac-002
    description: Output mentions recommendations
    check_type: contains
    expected: recommendation
risks:
  - id: risk-001
    description: Model hallucination in recommendations
    likelihood: 0.3
    impact: 0.8
    mitigation: Use structured output validation
```

## Technical debt

See DEVLOG.md for DT entries introduced in Phase 4.

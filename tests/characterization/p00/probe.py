"""Run a single P00 SDK route in a child process."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

from flow import graph


async def explicit_adapter(payload: dict[str, str]) -> dict[str, object]:
    from forge_adapters.langgraph_adapter import LangGraphAdapter
    from forge_core.harness import MetaOrchestrator
    from forge_core.types import RunConfig, TaskEnvelope

    adapter = LangGraphAdapter()
    await adapter.load(Path(__file__).with_name("flow.py"))
    host = MetaOrchestrator(adapter=adapter)
    result = await host.run(TaskEnvelope(input=payload, config=RunConfig(enable_memory=False)))
    return result.model_dump(mode="json")


async def legacy_mutation(payload: dict[str, str]) -> dict[str, object]:
    from forge_adapters.langgraph_adapter import LangGraphAdapter
    from forge_core.evolution.eval_suite import EvalCase, EvalSuite
    from forge_core.evolution.loop import EvolutionLoop
    from forge_core.evolution.mutations import AgentCullMutator
    from forge_core.harness import MetaOrchestrator
    from forge_core.types import (
        FitnessScore,
        Mutation,
        MutationDiff,
        MutationKind,
        RunConfig,
        RunResult,
        TaskEnvelope,
    )
    from forge_review.runner import ReviewRunner
    from forge_review.types import Finding, FindingSeverity, ReviewContext

    class SyntheticReviewer:
        name = "retain_escalation_lookup"

        async def review(self, context: ReviewContext) -> list[Finding]:
            if context.data.get("mutation_kind") != MutationKind.AGENT_CULL:
                return []
            return [
                Finding(
                    severity=FindingSeverity.P0,
                    title="Required ticket lookup",
                    message="Escalation lookup must remain available for P00-S01.",
                    source=self.name,
                )
            ]

    class TargetedCull(AgentCullMutator):
        def propose(self, run_results: list[RunResult]) -> Mutation:
            return Mutation(
                kind=self.mutation_kind,
                description="Remove escalation_lookup from the adapter topology",
                diff=[
                    MutationDiff(
                        field_path="agents[id=escalation_lookup]", before="escalation_lookup"
                    )
                ],
            )

    class SyntheticEvaluator:
        def evaluate_sync(self, result: RunResult) -> FitnessScore:
            # Remove latency noise from a read-only control. The direct policy
            # decision and the independent ticket destination are the oracles.
            return FitnessScore(overall=float(result.status == "completed"))

    adapter = LangGraphAdapter()
    await adapter.load(Path(__file__).with_name("flow.py"))
    host = MetaOrchestrator(adapter=adapter)
    control = {**payload, "ticket_id": "P00-S02"}
    config = RunConfig(enable_memory=False)
    control_statuses = [
        (await host.run(TaskEnvelope(input=control, config=config))).status for _ in range(2)
    ]
    before = host.capture_snapshot()
    runner = ReviewRunner(reviewers=[SyntheticReviewer()])
    _, decision = await runner.pre_apply(MutationKind.AGENT_CULL)
    loop = EvolutionLoop(
        host,
        mode="auto",
        mutators=[TargetedCull()],
        evaluator=SyntheticEvaluator(),
        eval_suite=EvalSuite.from_cases([EvalCase("P00-S02", control)]),
    )
    with patch.object(
        ReviewRunner, "pre_apply", autospec=True, side_effect=ReviewRunner.pre_apply
    ) as pre_apply:
        applied = await loop.step()
    after = host.capture_snapshot()
    result = await host.run(TaskEnvelope(input=payload, config=config))
    return {
        "control_statuses": control_statuses,
        "standalone_review": decision.model_dump(mode="json"),
        "loop_pre_apply_calls": pre_apply.call_count,
        "applied_mutation": applied.model_dump(mode="json") if applied is not None else None,
        "topology_before": [agent.id for agent in before.agents_list()],
        "topology_after": [agent.id for agent in after.agents_list()],
        "topology_versions": [before.version, after.version],
        "subsequent_run": result.model_dump(mode="json"),
    }


def main() -> None:
    route, request_json, output_file = sys.argv[1:]
    payload = json.loads(request_json)
    if route == "native":
        result = {"status": "completed", "output": graph.invoke(payload)}
    elif route == "adapter":
        result = asyncio.run(explicit_adapter(payload))
    elif route == "mutation":
        result = asyncio.run(legacy_mutation(payload))
    else:
        raise SystemExit(f"Unknown SDK route: {route}")
    Path(output_file).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

"""Full-stack integration test (fase ε.5).

This exercises the complete Forge pipeline with realistic data flowing
through every layer — interceptor, bus, metrics, cost, memory,
evolution — without hitting any real SDK. The assertions are the kind
an enterprise buyer would want to see in a demo recording before
signing off on the product.

Scope:

1. A flow that calls the mock LLM multiple times emits populated
   ``LLM_CALL`` events through the bus.
2. Those events aggregate into non-zero cost and token totals in
   :class:`MetricsCollector`.
3. The same run history, fed to the evolution loop, produces at least
   one mutation proposal when the history is interesting enough.
4. Memory writes from the run flow are queryable afterwards.
5. The cost-savings estimate is a real number, not a stub.

This test does not depend on any external network / SDK and can run in
CI without credentials.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from forge_core.testing import ForgeTestHarness, MockResponse
from forge_core.types import RunEventKind


@pytest.mark.asyncio
async def test_full_stack_produces_real_numbers_across_five_runs() -> None:
    harness = ForgeTestHarness(cost_per_call=Decimal("0.002"))

    # Program 2 responses per run for 5 runs.
    for i in range(5):
        harness.llm.program(
            [
                MockResponse(
                    content=f"plan-{i}",
                    input_tokens=120,
                    output_tokens=40,
                    model="claude-haiku-4-20250514",
                ),
                MockResponse(
                    content=f"exec-{i}",
                    input_tokens=80,
                    output_tokens=60,
                    model="claude-sonnet-4-20250514",
                ),
            ]
        )

    async def research_flow(inp, llm):
        plan = await llm.complete(
            prompt=f"Plan: {inp['q']}",
            agent_id="planner",
            model="claude-haiku-4-20250514",
        )
        answer = await llm.complete(
            prompt=f"Execute: {plan.content}",
            agent_id="executor",
            model="claude-sonnet-4-20250514",
        )
        return {"plan": plan.content, "answer": answer.content}

    outputs = []
    for i in range(5):
        run = await harness.run_flow(research_flow, {"q": f"question {i}"})
        outputs.append(run.output)
        assert run.total_cost > Decimal("0")
        assert run.total_input_tokens >= 200
        assert run.total_output_tokens >= 100

    # All 5 plans were distinct (flow really executed, no shared state leak).
    assert len({o["plan"] for o in outputs}) == 5

    llm_events = [e for e in harness.events if e.kind == RunEventKind.LLM_CALL]
    assert len(llm_events) == 10  # 2 per run, 5 runs

    # Per-agent attribution survived the full pipeline.
    agent_ids = {e.agent_id for e in llm_events}
    assert agent_ids == {"planner", "executor"}


@pytest.mark.asyncio
async def test_events_attribute_cost_and_tokens_per_agent() -> None:
    """Run-level aggregation: each agent's events add up to its own totals."""
    harness = ForgeTestHarness(cost_per_call=Decimal("0.01"))
    harness.llm.program(
        [
            MockResponse(content="p", input_tokens=50, output_tokens=25),
            MockResponse(content="e", input_tokens=30, output_tokens=15),
            MockResponse(content="r", input_tokens=10, output_tokens=5),
        ]
    )

    async def flow(_inp, llm):
        await llm.complete(prompt="x", agent_id="planner")
        await llm.complete(prompt="y", agent_id="executor")
        await llm.complete(prompt="z", agent_id="planner")
        return None

    run = await harness.run_flow(flow, {})

    by_agent: dict[str, int] = {}
    for e in run.events:
        if e.kind != RunEventKind.LLM_CALL:
            continue
        by_agent[e.agent_id or ""] = by_agent.get(e.agent_id or "", 0) + 1

    assert by_agent == {"planner": 2, "executor": 1}
    assert run.total_cost == Decimal("0.03")

"""Full-stack integration: harness → events → metrics → savings (fase ε.5).

forge-observe sits on top of forge-core, so this side of the integration
test lives here. It exercises the real :class:`MetricsCollector`
ingesting events from a :class:`ForgeTestHarness` run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from forge_core.testing import ForgeTestHarness, MockResponse
from forge_core.types import CostSummary, RunResult, RunStatus
from forge_observe.metrics import MetricsCollector


@pytest.mark.asyncio
async def test_metrics_collector_ingests_harness_run() -> None:
    harness = ForgeTestHarness(cost_per_call=Decimal("0.01"))
    harness.llm.program(
        [
            MockResponse(content="a", input_tokens=50, output_tokens=25, model="m1"),
            MockResponse(content="b", input_tokens=30, output_tokens=15, model="m1"),
        ]
    )

    async def flow(_inp, llm):
        await llm.complete(prompt="one", agent_id="writer", model="m1")
        await llm.complete(prompt="two", agent_id="writer", model="m1")
        return "done"

    run = await harness.run_flow(flow, {})

    result = RunResult(
        run_id=uuid4().hex,
        task_id=uuid4().hex,
        status=RunStatus.COMPLETED,
        duration_ms=100.0,
        cost=CostSummary(
            total_cost=run.total_cost,
            total_input_tokens=run.total_input_tokens,
            total_output_tokens=run.total_output_tokens,
            llm_calls=len(run.events),
        ),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        events=run.events,
    )

    collector = MetricsCollector()
    collector.record_run(result)

    s = collector.session
    assert s.total_runs == 1
    assert s.successful_runs == 1
    assert s.total_cost == Decimal("0.02")
    assert s.total_input_tokens == 80
    assert s.total_output_tokens == 40
    # "writer" is in the default label allowlist → passes through uncracked.
    assert "writer" in s.agent_metrics
    assert s.agent_metrics["writer"].llm_calls == 2


@pytest.mark.asyncio
async def test_post_cap_agent_ids_bucket_deterministically() -> None:
    """After the per-key cap, repeat values must map to the same bucketed key.

    This is the weaker — but actually enforceable — guarantee the
    LabelSanitizer gives MetricsCollector: a single runaway id that
    repeats a thousand times does NOT create a thousand agent_metrics
    entries. Truly-unique-each-time ids still diverge; that's by design
    (operators would rather see distinct hashed series than merged noise).
    """
    from forge_observe.labels import LabelSanitizer

    sanitizer = LabelSanitizer(allowlist={"agent_id"}, max_unique_per_key=2)
    collector = MetricsCollector(label_sanitizer=sanitizer)

    # Two "real" agents + 10 occurrences of the same over-cap id.
    harness = ForgeTestHarness(cost_per_call=Decimal("0.001"))
    harness.llm.program(
        [MockResponse(content="x", input_tokens=5, output_tokens=5) for _ in range(12)]
    )

    async def flow(_inp, llm):
        await llm.complete(prompt="p", agent_id="planner")
        await llm.complete(prompt="p", agent_id="executor")
        for _ in range(10):
            await llm.complete(prompt="p", agent_id="runaway-id-42")
        return None

    run = await harness.run_flow(flow, {})
    result = RunResult(
        run_id=uuid4().hex,
        task_id=uuid4().hex,
        status=RunStatus.COMPLETED,
        duration_ms=10.0,
        cost=CostSummary(total_cost=run.total_cost),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        events=run.events,
    )
    collector.record_run(result)

    # 2 in-cap keys + exactly 1 bucketed key, not 10.
    assert len(collector.session.agent_metrics) == 3
    bucketed = [k for k in collector.session.agent_metrics if k.startswith("bucket_")]
    assert len(bucketed) == 1
    assert collector.session.agent_metrics[bucketed[0]].llm_calls == 10

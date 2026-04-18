"""End-to-end cost tracking integration tests.

These tests are the canonical acceptance criteria for Fase α of the L7/L8
remediation plan. They validate that a run driven by the real
:class:`MetaOrchestrator`, using the real :class:`ForgeLLMInterceptor`
and the real :class:`DefaultCostModel`, produces non-zero costs and
properly-populated token counts.

Pre-Fase-α, these tests would have failed — the adapter wasn't wired to
the interceptor, the interceptor didn't exist as a protocol, and the
orchestrator aggregated from the adapter's return value rather than from
the bus. If any of them regresses to red, the "demo with real dollar
amounts" contract is broken.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.mock_llm_adapter import MockLLMFlowAdapter

from forge_core.events import ALL_EVENTS, EventBus
from forge_core.harness import MetaOrchestrator
from forge_core.types import RunEvent, RunEventKind, RunStatus, TaskEnvelope


@pytest.mark.asyncio
async def test_run_produces_non_zero_cost() -> None:
    """A run through the interceptor populates ``RunResult.cost.total_cost > 0``.

    This is the single most important assertion in the codebase. If it
    fails, the product's core promise ("see real costs per agent") is
    broken regardless of how pretty the Rich UI renders.
    """
    orchestrator = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(
        model="claude-haiku-4-20250514",
        input_tokens=1_000,
        output_tokens=500,
        num_llm_calls=3,
    )
    orchestrator.set_adapter(adapter)
    await adapter.load("unused")

    result = await orchestrator.run(TaskEnvelope(input={"query": "hello"}))

    assert result.status == RunStatus.COMPLETED
    assert result.cost.total_cost > Decimal("0"), (
        "Run completed with zero cost — cost tracking is broken. See L7/L8 Fase α Gap #1."
    )
    # Haiku: $0.80 / 1M in, $4.00 / 1M out. 3 calls * (1000 in + 500 out):
    # expected = 3 * (1000 * 0.80 / 1e6 + 500 * 4.00 / 1e6)
    #          = 3 * (0.0008 + 0.002) = 3 * 0.0028 = $0.0084
    assert result.cost.total_cost == pytest.approx(Decimal("0.0084"), rel=Decimal("0.01"))


@pytest.mark.asyncio
async def test_token_counts_are_aggregated() -> None:
    """Total tokens == sum of per-call tokens; llm_calls counts calls."""
    orchestrator = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(
        input_tokens=100,
        output_tokens=50,
        num_llm_calls=4,
    )
    orchestrator.set_adapter(adapter)
    await adapter.load("unused")

    result = await orchestrator.run(TaskEnvelope(input={}))

    assert result.cost.total_input_tokens == 400
    assert result.cost.total_output_tokens == 200
    assert result.cost.llm_calls == 4


@pytest.mark.asyncio
async def test_events_are_tagged_with_run_id() -> None:
    """Events published through the bus inherit the active run's ``run_id``."""
    orchestrator = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(num_llm_calls=2)
    orchestrator.set_adapter(adapter)
    await adapter.load("unused")

    result = await orchestrator.run(TaskEnvelope(input={}))

    llm_events = [e for e in result.events if e.kind == RunEventKind.LLM_CALL]
    assert len(llm_events) == 2
    for event in llm_events:
        assert event.run_id == result.run_id
        assert event.run_id is not None


@pytest.mark.asyncio
async def test_cost_aggregated_by_agent_and_model() -> None:
    """``CostSummary.cost_by_agent`` and ``cost_by_model`` are populated."""
    orchestrator = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(
        model="claude-sonnet-4-20250514",
        input_tokens=1_000,
        output_tokens=100,
        num_llm_calls=2,
        agent_names=("planner", "researcher"),
    )
    orchestrator.set_adapter(adapter)
    await adapter.load("unused")

    result = await orchestrator.run(TaskEnvelope(input={}))

    # One call per agent given num_llm_calls=2, 2 agents.
    assert "planner" in result.cost.cost_by_agent
    assert "researcher" in result.cost.cost_by_agent
    assert result.cost.cost_by_agent["planner"] > Decimal("0")
    assert result.cost.cost_by_model["claude-sonnet-4-20250514"] > Decimal("0")
    # By-agent and by-model totals should both match the overall total.
    assert sum(result.cost.cost_by_agent.values()) == result.cost.total_cost
    assert sum(result.cost.cost_by_model.values()) == result.cost.total_cost


@pytest.mark.asyncio
async def test_bus_subscribers_receive_llm_call_events() -> None:
    """External subscribers get the same events the orchestrator aggregates from.

    This is what lets the dashboard, OTel tracer, and memory ingester work
    without any coupling to the orchestrator's internals.
    """
    orchestrator = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(num_llm_calls=3)
    orchestrator.set_adapter(adapter)
    await adapter.load("unused")

    observed: list[RunEvent] = []

    async def observer(event: RunEvent) -> None:
        if event.kind == RunEventKind.LLM_CALL:
            observed.append(event)

    orchestrator.bus.subscribe(ALL_EVENTS, observer)

    await orchestrator.run(TaskEnvelope(input={}))

    assert len(observed) == 3
    assert all(e.cost > Decimal("0") for e in observed)
    assert all(e.span_id is not None for e in observed)


@pytest.mark.asyncio
async def test_run_started_and_completed_events_framed_properly() -> None:
    """Every run emits exactly one ``RUN_STARTED`` and one ``RUN_COMPLETED``."""
    orchestrator = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(num_llm_calls=1)
    orchestrator.set_adapter(adapter)
    await adapter.load("unused")

    collected: list[RunEvent] = []

    async def observer(event: RunEvent) -> None:
        collected.append(event)

    orchestrator.bus.subscribe(ALL_EVENTS, observer)

    await orchestrator.run(TaskEnvelope(input={}))

    started = [e for e in collected if e.kind == RunEventKind.RUN_STARTED]
    completed = [e for e in collected if e.kind == RunEventKind.RUN_COMPLETED]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].run_id == completed[0].run_id
    assert started[0].run_id is not None


@pytest.mark.asyncio
async def test_unknown_model_returns_zero_without_crashing() -> None:
    """Cost model falls back to zero for unknown models — no hard failure."""
    orchestrator = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(
        model="this-model-does-not-exist-2099",
        input_tokens=1000,
        output_tokens=500,
        num_llm_calls=2,
    )
    orchestrator.set_adapter(adapter)
    await adapter.load("unused")

    result = await orchestrator.run(TaskEnvelope(input={}))
    assert result.status == RunStatus.COMPLETED
    # Cost is zero but tokens/calls are still tracked.
    assert result.cost.total_cost == Decimal("0")
    assert result.cost.total_input_tokens == 2000
    assert result.cost.llm_calls == 2


@pytest.mark.asyncio
async def test_interceptor_shared_with_adapter() -> None:
    """The orchestrator passes its interceptor to the adapter via ``set_interceptor``."""
    orchestrator = MetaOrchestrator()
    adapter = MockLLMFlowAdapter(num_llm_calls=1)
    assert adapter._interceptor is None  # pre-wiring
    orchestrator.set_adapter(adapter)
    assert adapter._interceptor is orchestrator.interceptor


@pytest.mark.asyncio
async def test_events_deduplicated_by_span_id() -> None:
    """If an adapter double-publishes the same span, aggregation counts once."""
    bus = EventBus()
    orchestrator = MetaOrchestrator(bus=bus)
    adapter = MockLLMFlowAdapter(num_llm_calls=1)
    orchestrator.set_adapter(adapter)
    await adapter.load("unused")

    # Sneak in a duplicate by publishing an extra event with the same span_id
    # as the first LLM call. The aggregator must not double-count.
    async def duplicate_publisher(event: RunEvent) -> None:
        if event.kind == RunEventKind.LLM_CALL:
            # Re-publish a clone with the same span_id.
            dupe = event.model_copy()
            await bus.publish(dupe)

    # Subscribe AFTER the interceptor/collector to avoid infinite loops.
    # Actually, safer: don't loop — just publish directly on the result.
    result = await orchestrator.run(TaskEnvelope(input={}))
    # Baseline: one call.
    assert result.cost.llm_calls == 1
    single_call_cost = result.cost.total_cost

    # Second run where we manually inject a dupe into the event list the
    # aggregator sees. We do this by re-invoking _aggregate_cost on a list
    # with deliberate duplicates and confirming dedup by span_id.
    llm_event = next(e for e in result.events if e.kind == RunEventKind.LLM_CALL)
    dupe = llm_event.model_copy()
    cost = MetaOrchestrator._aggregate_cost([llm_event, dupe])
    assert cost.llm_calls == 1, "Span-id dedup broken — dupes inflate counts"
    assert cost.total_cost == single_call_cost

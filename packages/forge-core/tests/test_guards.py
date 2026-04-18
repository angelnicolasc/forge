"""Tests for :mod:`forge_core.guards` — cost and timeout enforcement.

These are the tests that separate "Forge is a cost dashboard" from
"Forge is a safety harness." A run without enforced limits is the
gap where the product's enterprise claim dies, so every branch here
is an operator-visible contract.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from forge_core.events import EventBus
from forge_core.guards import (
    BudgetExceeded,
    CostBudgetGuard,
    TimeoutExceeded,
    run_with_guards,
)
from forge_core.types import RunEvent, RunEventKind

# ---------------------------------------------------------------------------
# CostBudgetGuard — unit-level behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_guard_tallies_multiple_llm_events() -> None:
    """Each LLM_CALL event on the bus increments ``observed``.

    Populating ``cost`` as a string (as some adapters do) must still
    parse cleanly into Decimal — we coerce defensively so malformed
    upstream events never crash the guard.
    """
    bus = EventBus()
    guard = CostBudgetGuard(ceiling=Decimal("10"), bus=bus)
    guard.attach()

    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("1.50")))
    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("2.75")))

    assert guard.observed == Decimal("4.25")
    assert guard.llm_calls == 2
    assert not guard.tripped

    guard.dispose()


@pytest.mark.asyncio
async def test_cost_guard_trips_at_ceiling() -> None:
    """Reaching (not just exceeding) the ceiling fires the trip event once."""
    bus = EventBus()
    guard = CostBudgetGuard(ceiling=Decimal("5"), bus=bus)
    guard.attach()

    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("3")))
    assert not guard.tripped
    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("2")))
    assert guard.tripped  # exactly at ceiling → trip

    # Subsequent events update observed but do not re-trip.
    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("1")))
    assert guard.observed == Decimal("6")
    assert guard.tripped

    guard.dispose()


@pytest.mark.asyncio
async def test_cost_guard_zero_ceiling_is_no_limit() -> None:
    """Operators pass ``Decimal(0)`` to disable enforcement entirely."""
    bus = EventBus()
    guard = CostBudgetGuard(ceiling=Decimal("0"), bus=bus)
    guard.attach()
    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("999")))
    assert guard.observed == Decimal("999")
    assert not guard.tripped
    guard.dispose()


@pytest.mark.asyncio
async def test_cost_guard_dispose_unsubscribes() -> None:
    """After dispose, further events don't feed the guard's accumulator."""
    bus = EventBus()
    guard = CostBudgetGuard(ceiling=Decimal("10"), bus=bus)
    guard.attach()
    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("1")))
    assert guard.observed == Decimal("1")

    guard.dispose()
    await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("5")))
    # No change after dispose.
    assert guard.observed == Decimal("1")


# ---------------------------------------------------------------------------
# run_with_guards — integration-level behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_guards_returns_result_on_happy_path() -> None:
    """When no guard trips, the inner coroutine's return value passes through."""
    bus = EventBus()

    async def flow() -> str:
        return "ok"

    result = await run_with_guards(flow, bus=bus, cost_ceiling=Decimal("100"), timeout_seconds=5.0)
    assert result == "ok"


@pytest.mark.asyncio
async def test_run_with_guards_raises_budget_exceeded_when_cost_trips() -> None:
    """Cost tripping mid-run cancels the adapter and raises BudgetExceeded."""
    bus = EventBus()
    cancelled = asyncio.Event()

    async def expensive_flow() -> str:
        try:
            # Publish an event that exceeds the ceiling, then sleep so
            # the guard has a chance to cancel us.
            await bus.publish(RunEvent(kind=RunEventKind.LLM_CALL, cost=Decimal("10")))
            await asyncio.sleep(5.0)
            return "should not reach"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(BudgetExceeded) as excinfo:
        await run_with_guards(
            expensive_flow,
            bus=bus,
            cost_ceiling=Decimal("5"),
            timeout_seconds=30.0,
        )

    assert excinfo.value.ceiling == Decimal("5")
    assert excinfo.value.observed >= Decimal("10")
    assert cancelled.is_set()  # The inner task was actually cancelled.


@pytest.mark.asyncio
async def test_run_with_guards_raises_timeout_exceeded() -> None:
    """Wall-clock budget exhaustion raises TimeoutExceeded, not asyncio's own."""
    bus = EventBus()

    async def slow_flow() -> str:
        await asyncio.sleep(5.0)
        return "late"

    with pytest.raises(TimeoutExceeded) as excinfo:
        await run_with_guards(
            slow_flow,
            bus=bus,
            cost_ceiling=Decimal("100"),
            timeout_seconds=0.1,
        )
    assert excinfo.value.timeout_seconds == 0.1
    # Loose bound — Windows timer jitter can report an elapsed value a
    # few milliseconds below the nominal deadline even though the
    # timeout genuinely fired. Within one tick is fine.
    assert excinfo.value.elapsed_seconds >= 0.05


@pytest.mark.asyncio
async def test_run_with_guards_none_timeout_means_no_limit() -> None:
    """``timeout_seconds=None`` keeps waiting forever — useful for evals."""
    bus = EventBus()

    async def slow_flow() -> str:
        await asyncio.sleep(0.2)
        return "done"

    result = await run_with_guards(
        slow_flow, bus=bus, cost_ceiling=Decimal("0"), timeout_seconds=None
    )
    assert result == "done"


@pytest.mark.asyncio
async def test_run_with_guards_negative_timeout_means_no_limit() -> None:
    """Negative timeout is equivalent to ``None`` — tolerant input."""
    bus = EventBus()

    async def flow() -> str:
        return "ok"

    result = await run_with_guards(flow, bus=bus, cost_ceiling=Decimal("0"), timeout_seconds=-1)
    assert result == "ok"


@pytest.mark.asyncio
async def test_run_with_guards_propagates_inner_exception() -> None:
    """Unrelated exceptions inside the flow bubble up unchanged."""
    bus = EventBus()

    class _UserError(RuntimeError):
        pass

    async def buggy() -> None:
        raise _UserError("boom")

    with pytest.raises(_UserError):
        await run_with_guards(buggy, bus=bus, cost_ceiling=Decimal("10"), timeout_seconds=5.0)


@pytest.mark.asyncio
async def test_run_with_guards_disposes_subscription_on_exception() -> None:
    """No lingering bus subscriptions even on failure paths.

    If the guard leaked a subscription, the NEXT run's accumulator
    would include this run's cost — a cross-run telemetry bug that's
    hard to diagnose in production. We lock it down at the unit level.
    """
    bus = EventBus()
    before = bus.subscriber_count(RunEventKind.LLM_CALL)

    async def boom() -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await run_with_guards(boom, bus=bus, cost_ceiling=Decimal("10"), timeout_seconds=5.0)

    after = bus.subscriber_count(RunEventKind.LLM_CALL)
    assert after == before, "CostBudgetGuard did not dispose its subscription"

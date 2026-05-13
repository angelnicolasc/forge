"""Concurrency tests for the evolution FSM auto-trigger.

These tests verify that ``_should_trigger_evolution``'s "claim" semantics —
mutating ``_last_evolution_at`` inside the guard — are safe under concurrent
``asyncio.gather`` calls, i.e. two runs completing simultaneously must
trigger evolution **at most once**, not twice.

The key race: if two ``run()`` coroutines both reach ``_should_trigger_evolution``
at the same tick and both see the interval guard as unelapsed, both could
return True and schedule two ``step()`` tasks. The existing implementation
uses the write to ``_last_evolution_at`` as a "claim" (no asyncio.Lock
needed because coroutines yield only at ``await`` points and the check +
write are synchronous), but the test validates that claim explicitly.
"""

from __future__ import annotations

import asyncio

import pytest
from fixtures.mock_llm_adapter import MockLLMFlowAdapter

from forge_core.config import EvolutionConfig, ForgeConfig
from forge_core.harness import MetaOrchestrator
from forge_core.types import TaskEnvelope

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _CountingEvolutionLoop:
    """Records how many times step() was called, with an optional delay."""

    def __init__(self, *, step_delay: float = 0.0) -> None:
        self.step_calls = 0
        self._delay = step_delay

    async def step(self) -> None:
        self.step_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)


def _make_orchestrator(
    *,
    every_n: int = 2,
    min_history: int = 2,
    min_interval: float = 0.0,
) -> MetaOrchestrator:
    cfg = ForgeConfig(
        evolution=EvolutionConfig(
            auto_trigger_enabled=True,
            min_history_for_evolution=min_history,
            trigger_every_n_runs=every_n,
            min_interval_seconds=min_interval,
        )
    )
    return MetaOrchestrator(config=cfg, adapter=MockLLMFlowAdapter(num_llm_calls=1))


async def _drain(orch: MetaOrchestrator) -> None:
    if orch.pending_evolution_tasks:
        await asyncio.gather(*orch.pending_evolution_tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Concurrency tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_simultaneous_runs_trigger_evolution_at_most_once() -> None:
    """Two runs completing at the same asyncio tick must schedule ≤1 step().

    Setup: ``trigger_every_n_runs=2``, ``min_history=2``. Both runs are the
    2nd and 3rd in history (after a warm-up run), so both could satisfy the
    modulo gate if the counter increments aren't guarded. The "claim"
    semantics of ``_last_evolution_at`` ensure only one wins.
    """
    orch = _make_orchestrator(every_n=2, min_history=2, min_interval=0.0)
    loop = _CountingEvolutionLoop()
    orch.attach_evolution_loop(loop)

    # Warm-up: get min_history satisfied by a single sequential run
    await orch.run(TaskEnvelope(input={"q": "warm-up"}))

    # Now fire two runs concurrently — both complete at the same asyncio tick
    await asyncio.gather(
        orch.run(TaskEnvelope(input={"q": "concurrent-A"})),
        orch.run(TaskEnvelope(input={"q": "concurrent-B"})),
    )
    await _drain(orch)

    assert loop.step_calls <= 1, (
        f"Expected evolution triggered at most once; got {loop.step_calls} calls. "
        "Two simultaneous runs should not both satisfy the trigger gate."
    )


@pytest.mark.asyncio
async def test_five_simultaneous_runs_trigger_evolution_at_most_once() -> None:
    """Five concurrent runs must not cause an evolution stampede."""
    orch = _make_orchestrator(every_n=1, min_history=1, min_interval=60.0)
    loop = _CountingEvolutionLoop()
    orch.attach_evolution_loop(loop)

    await asyncio.gather(*[orch.run(TaskEnvelope(input={"q": f"run-{i}"})) for i in range(5)])
    await _drain(orch)

    # The interval debouncer (min_interval=60s) ensures that after the first
    # trigger the remaining concurrent runs find the gate closed.
    assert loop.step_calls <= 1, (
        f"Interval debouncer failed: evolution triggered {loop.step_calls} times "
        "across 5 concurrent runs. Expected at most 1."
    )


@pytest.mark.asyncio
async def test_evolution_not_triggered_before_min_history() -> None:
    """Concurrent runs that collectively satisfy min_history still obey the gate."""
    orch = _make_orchestrator(every_n=1, min_history=3, min_interval=0.0)
    loop = _CountingEvolutionLoop()
    orch.attach_evolution_loop(loop)

    # Two concurrent runs: neither alone satisfies min_history=3
    await asyncio.gather(
        orch.run(TaskEnvelope(input={"q": "A"})),
        orch.run(TaskEnvelope(input={"q": "B"})),
    )
    await _drain(orch)

    assert loop.step_calls == 0, (
        f"Evolution should not trigger before min_history=3; "
        f"got {loop.step_calls} calls with only 2 runs."
    )


@pytest.mark.asyncio
async def test_internal_eval_runs_never_trigger_evolution() -> None:
    """Envelopes with forge_internal_eval=True must not re-trigger the loop."""
    orch = _make_orchestrator(every_n=1, min_history=1, min_interval=0.0)
    loop = _CountingEvolutionLoop()
    orch.attach_evolution_loop(loop)

    eval_envelopes = [
        TaskEnvelope(input={"q": f"eval-{i}"}, metadata={"forge_internal_eval": True})
        for i in range(5)
    ]
    await asyncio.gather(*[orch.run(e) for e in eval_envelopes])
    await _drain(orch)

    assert loop.step_calls == 0, (
        "Internal eval envelopes must never trigger auto-evolution "
        f"(reentrancy guard failed: {loop.step_calls} step() calls)."
    )


@pytest.mark.asyncio
async def test_concurrent_runs_all_return_run_results() -> None:
    """Concurrently running 10 flows must all return a RunResult (no exceptions escape)."""
    orch = _make_orchestrator(every_n=5, min_history=5)
    loop = _CountingEvolutionLoop(step_delay=0.01)
    orch.attach_evolution_loop(loop)

    # Load the adapter so runs complete (not just fail with "not loaded")
    if orch._adapter is not None:
        await orch._adapter.load("unused")

    results = await asyncio.gather(
        *[orch.run(TaskEnvelope(input={"q": f"q{i}"})) for i in range(10)]
    )
    await _drain(orch)

    assert len(results) == 10
    for r in results:
        # Every concurrent run must produce a RunResult with a valid status —
        # no unhandled exception should escape from run() under concurrent load.
        assert r.status in ("completed", "failed"), f"Unexpected status: {r.status}"

"""ζ.5b — Evolution auto-suspend breaker.

When three consecutive ``EvolutionLoop.step()`` calls blow up, the
orchestrator's evolution breaker trips to OPEN. Auto-trigger then stops
firing step() until the operator calls ``resume_evolution()``.
"""

from __future__ import annotations

import asyncio

import pytest
from fixtures.mock_llm_adapter import MockLLMFlowAdapter

from forge_core.circuit_breaker import CircuitState
from forge_core.config import EvolutionConfig, ForgeConfig
from forge_core.harness import MetaOrchestrator
from forge_core.types import TaskEnvelope


class _FailingLoop:
    """EvolutionLoop stand-in whose ``step`` always raises."""

    def __init__(self) -> None:
        self.step_calls = 0

    async def step(self) -> None:
        self.step_calls += 1
        raise RuntimeError("mutator exploded")


def _make_orch() -> MetaOrchestrator:
    cfg = ForgeConfig(
        evolution=EvolutionConfig(
            auto_trigger_enabled=True,
            min_history_for_evolution=1,
            trigger_every_n_runs=1,
            min_interval_seconds=0.0,
            mode="suggest",
        )
    )
    return MetaOrchestrator(config=cfg, adapter=MockLLMFlowAdapter(num_llm_calls=1))


async def _drain(orch: MetaOrchestrator) -> None:
    if orch.pending_evolution_tasks:
        await asyncio.gather(*orch.pending_evolution_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_evolution_breaker_trips_after_three_failures() -> None:
    orch = _make_orch()
    loop = _FailingLoop()
    orch.attach_evolution_loop(loop)

    # Three failing step() calls — the breaker counts each and trips on
    # the third. Subsequent runs don't schedule step() at all.
    for _ in range(3):
        await orch.run(TaskEnvelope(input={"q": "r"}))
        await _drain(orch)
    assert loop.step_calls == 3
    assert orch.evolution_breaker.state is CircuitState.OPEN

    # Next runs skip step() entirely thanks to the breaker-open guard.
    for _ in range(5):
        await orch.run(TaskEnvelope(input={"q": "r"}))
        await _drain(orch)
    assert loop.step_calls == 3  # unchanged


@pytest.mark.asyncio
async def test_resume_evolution_reopens_the_path() -> None:
    orch = _make_orch()
    loop = _FailingLoop()
    orch.attach_evolution_loop(loop)

    for _ in range(3):
        await orch.run(TaskEnvelope(input={"q": "r"}))
        await _drain(orch)
    assert orch.evolution_breaker.state is CircuitState.OPEN

    # Operator investigated, confirmed it was transient, and re-armed.
    orch.resume_evolution()
    assert orch.evolution_breaker.state is CircuitState.CLOSED
    assert orch.evolution_breaker.failure_count == 0

    # A further run would again invoke step(); we only assert that the
    # scheduling path is unblocked (not that the mutator magically works).
    await orch.run(TaskEnvelope(input={"q": "r"}))
    await _drain(orch)
    assert loop.step_calls == 4

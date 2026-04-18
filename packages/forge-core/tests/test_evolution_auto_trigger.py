"""Acceptance tests for β.4 — auto-trigger of the evolution loop.

These tests prove every gate in :meth:`MetaOrchestrator._should_trigger_evolution`
and the fire-and-forget scheduler behaves as specified in the L7/L8 plan:

* Default posture is passive — no loop attached ⇒ no trigger.
* The feature flag (``config.evolution.auto_trigger_enabled``) is the
  master switch; with the flag off, even an attached loop stays silent.
* When armed, a background ``step()`` task is created, awaitable via
  :attr:`MetaOrchestrator.pending_evolution_tasks`, and its success or
  failure never poisons the user's ``run()`` call path.
* Reentrancy guard: envelopes marked with ``metadata["forge_internal_eval"]``
  (the loop's own ``_eval_run`` envelopes) skip auto-trigger. Without this,
  evolution would infinitely recurse because every eval run would queue
  another step.
* The cadence + interval debouncers keep the scheduler from stampeding
  when runs arrive in a burst.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fixtures.mock_llm_adapter import MockLLMFlowAdapter

from forge_core.config import EvolutionConfig, ForgeConfig
from forge_core.harness import MetaOrchestrator
from forge_core.types import RunConfig, TaskEnvelope

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEvolutionLoop:
    """Minimal stand-in for :class:`EvolutionLoop` that records step calls.

    The real loop is end-to-end integration-tested in β.6. Here we only
    need to verify the **orchestrator's** wiring: that ``step()`` gets
    awaited exactly when every gate passes, and that an exception inside
    ``step()`` doesn't escape to the user's ``run()``.
    """

    def __init__(self, *, raise_on_step: bool = False) -> None:
        self.step_calls = 0
        self._raise = raise_on_step
        self._barrier: asyncio.Event | None = None

    def arm_barrier(self) -> asyncio.Event:
        """Install a barrier so tests can block step() until released."""
        self._barrier = asyncio.Event()
        return self._barrier

    async def step(self) -> None:
        self.step_calls += 1
        if self._barrier is not None:
            await self._barrier.wait()
        if self._raise:
            raise RuntimeError("simulated evolution failure")


def _make_orchestrator(
    *,
    auto_trigger_enabled: bool = True,
    min_history: int = 2,
    every_n: int = 1,
    min_interval: float = 0.0,
    mode: str = "suggest",
) -> MetaOrchestrator:
    cfg = ForgeConfig(
        evolution=EvolutionConfig(
            auto_trigger_enabled=auto_trigger_enabled,
            min_history_for_evolution=min_history,
            trigger_every_n_runs=every_n,
            min_interval_seconds=min_interval,
            mode=mode,
        )
    )
    orch = MetaOrchestrator(config=cfg, adapter=MockLLMFlowAdapter(num_llm_calls=1))
    return orch


async def _drain_pending(orch: MetaOrchestrator) -> None:
    """Await every still-scheduled evolution task so assertions are deterministic."""
    if orch.pending_evolution_tasks:
        await asyncio.gather(*orch.pending_evolution_tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Basic gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_trigger_when_loop_not_attached() -> None:
    """Default: no evolution loop attached ⇒ trigger is a no-op."""
    orch = _make_orchestrator(auto_trigger_enabled=True, min_history=1)
    for _ in range(3):
        await orch.run(TaskEnvelope(input={"q": "hi"}))
    assert len(orch.pending_evolution_tasks) == 0


@pytest.mark.asyncio
async def test_no_trigger_when_flag_off() -> None:
    """Loop attached but ``auto_trigger_enabled=False`` ⇒ still no trigger."""
    orch = _make_orchestrator(auto_trigger_enabled=False, min_history=1)
    loop = FakeEvolutionLoop()
    orch.attach_evolution_loop(loop)
    for _ in range(3):
        await orch.run(TaskEnvelope(input={"q": "hi"}))
    await _drain_pending(orch)
    assert loop.step_calls == 0


@pytest.mark.asyncio
async def test_trigger_fires_when_all_gates_pass() -> None:
    """Loop attached + flag on + enough history ⇒ step() runs in the background."""
    orch = _make_orchestrator(auto_trigger_enabled=True, min_history=2, every_n=1)
    loop = FakeEvolutionLoop()
    orch.attach_evolution_loop(loop)

    await orch.run(TaskEnvelope(input={"q": "1"}))  # history=1, below min
    await _drain_pending(orch)
    assert loop.step_calls == 0

    await orch.run(TaskEnvelope(input={"q": "2"}))  # history=2, fires
    await _drain_pending(orch)
    assert loop.step_calls == 1


# ---------------------------------------------------------------------------
# Reentrancy guard (THE critical correctness gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_eval_envelope_does_not_retrigger() -> None:
    """Envelopes marked ``forge_internal_eval`` must never spawn a step.

    Without this guard, :class:`EvolutionLoop._eval_run` — which calls
    ``orchestrator.run()`` to measure fitness — would re-enter the auto-
    trigger path and recurse infinitely.
    """
    orch = _make_orchestrator(auto_trigger_enabled=True, min_history=1, every_n=1)
    loop = FakeEvolutionLoop()
    orch.attach_evolution_loop(loop)

    # Simulate a few "internal eval" runs — all of them carry the marker.
    for i in range(5):
        envelope = TaskEnvelope(
            input={"q": f"eval-{i}"},
            config=RunConfig(enable_evolution=False, enable_memory=False),
            metadata={"forge_internal_eval": True},
        )
        await orch.run(envelope)
    await _drain_pending(orch)
    assert loop.step_calls == 0, "Internal eval runs leaked into auto-trigger"


# ---------------------------------------------------------------------------
# Debouncers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modulo_cadence_throttles_triggers() -> None:
    """With ``trigger_every_n_runs=3``, only every 3rd run fires."""
    orch = _make_orchestrator(auto_trigger_enabled=True, min_history=1, every_n=3)
    loop = FakeEvolutionLoop()
    orch.attach_evolution_loop(loop)

    for i in range(9):
        await orch.run(TaskEnvelope(input={"q": f"r{i}"}))
    await _drain_pending(orch)
    # history sizes hit: 1,2,3,4,5,6,7,8,9 → fires at 3, 6, 9
    assert loop.step_calls == 3


@pytest.mark.asyncio
async def test_interval_debouncer_prevents_stampede() -> None:
    """With ``min_interval_seconds`` large, later runs are throttled."""
    orch = _make_orchestrator(
        auto_trigger_enabled=True, min_history=1, every_n=1, min_interval=3600.0
    )
    loop = FakeEvolutionLoop()
    orch.attach_evolution_loop(loop)

    for _ in range(5):
        await orch.run(TaskEnvelope(input={"q": "burst"}))
    await _drain_pending(orch)
    assert loop.step_calls == 1  # first one fires, subsequent ones debounced


@pytest.mark.asyncio
async def test_interval_debouncer_allows_re_trigger_after_window() -> None:
    """Once the interval has elapsed, the next run fires again."""
    orch = _make_orchestrator(
        auto_trigger_enabled=True, min_history=1, every_n=1, min_interval=60.0
    )
    loop = FakeEvolutionLoop()
    orch.attach_evolution_loop(loop)

    await orch.run(TaskEnvelope(input={"q": "a"}))
    await _drain_pending(orch)
    assert loop.step_calls == 1

    # Fast-forward the debouncer manually (equivalent to 2 minutes passing).
    orch._last_evolution_at = datetime.now(UTC) - timedelta(minutes=2)

    await orch.run(TaskEnvelope(input={"q": "b"}))
    await _drain_pending(orch)
    assert loop.step_calls == 2


# ---------------------------------------------------------------------------
# Crash isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_exception_does_not_poison_run() -> None:
    """A failing ``step()`` must be logged but never escalate to the caller."""
    orch = _make_orchestrator(auto_trigger_enabled=True, min_history=1, every_n=1)
    loop = FakeEvolutionLoop(raise_on_step=True)
    orch.attach_evolution_loop(loop)

    # run() returns normally even though the scheduled step raises.
    result = await orch.run(TaskEnvelope(input={"q": "boom"}))
    assert result.status.value in {"completed", "failed"}  # at minimum: returned

    # Draining the pending tasks swallows the exception (return_exceptions=True).
    await _drain_pending(orch)
    assert loop.step_calls == 1
    assert len(orch.pending_evolution_tasks) == 0  # cleanup happened


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_task_set_reflects_in_flight_step() -> None:
    """While ``step()`` is running, the task is visible in pending_evolution_tasks."""
    orch = _make_orchestrator(auto_trigger_enabled=True, min_history=1, every_n=1)
    loop = FakeEvolutionLoop()
    barrier = loop.arm_barrier()
    orch.attach_evolution_loop(loop)

    await orch.run(TaskEnvelope(input={"q": "block"}))

    # Task is created but blocked on the barrier — it's in the set.
    assert len(orch.pending_evolution_tasks) == 1
    task = next(iter(orch.pending_evolution_tasks))
    assert not task.done()
    assert task.get_name().startswith("forge-evolution-step-")

    barrier.set()
    await _drain_pending(orch)
    assert len(orch.pending_evolution_tasks) == 0


@pytest.mark.asyncio
async def test_attach_overwrites_previous_loop() -> None:
    """``attach_evolution_loop`` is idempotent-replace — last call wins."""
    orch = _make_orchestrator(auto_trigger_enabled=True, min_history=1, every_n=1)
    first = FakeEvolutionLoop()
    second = FakeEvolutionLoop()
    orch.attach_evolution_loop(first)
    orch.attach_evolution_loop(second)

    await orch.run(TaskEnvelope(input={"q": "hi"}))
    await _drain_pending(orch)
    assert first.step_calls == 0
    assert second.step_calls == 1

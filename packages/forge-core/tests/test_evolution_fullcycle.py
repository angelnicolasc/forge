"""β.6 — full evolution cycle acceptance tests.

These tests exercise the end-to-end path through :class:`EvolutionLoop`:

    OBSERVE -> HYPOTHESIZE -> MUTATE -> EVALUATE -> COMMIT / ROLLBACK

Individual pieces are tested elsewhere (``test_mutations_v2``,
``test_topology_snapshot``, ``test_eval_suite``, ``test_evolution_auto_trigger``).
This file proves the pieces cohere: a proposed mutation that improves
fitness is committed and survives in the orchestrator's topology; a
proposed mutation that degrades fitness is rolled back, leaving the
topology bit-identical to the pre-snapshot (via ``content_hash``).

The acceptance gate for Fase β as a whole is: **these tests pass, with
a real orchestrator (not a mock) and a real TopologySnapshot-based
rollback path.**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from fixtures.mock_llm_adapter import MockLLMFlowAdapter

from forge_core.evolution.eval_suite import EvalCase, EvalSuite
from forge_core.evolution.llm_client import FakeLLMClient
from forge_core.evolution.loop import EvolutionLoop
from forge_core.evolution.mutations import (
    ModelSwapMutator,
    ParameterTuneMutator,
    PromptRewriteMutator,
    PromptRewriteResponse,
)
from forge_core.harness import MetaOrchestrator
from forge_core.types import (
    AgentCard,
    CostSummary,
    FitnessScore,
    Mutation,
    MutationDiff,
    MutationKind,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class ScriptedEvaluator:
    """Evaluator that returns pre-programmed FitnessScore values in sequence.

    Used to force the loop down the commit path (scores: low then high)
    or the rollback path (scores: high then low) without depending on
    the real cost/latency heuristics.
    """

    scores: list[float]
    _calls: list[FitnessScore] = field(default_factory=list)

    def evaluate_sync(self, result: RunResult) -> FitnessScore:
        idx = min(len(self._calls), len(self.scores) - 1)
        score = self.scores[idx]
        fs = FitnessScore(
            overall=score,
            cost_score=score,
            latency_score=score,
            quality_score=score,
            run_id=result.run_id,
        )
        self._calls.append(fs)
        return fs

    async def evaluate(self, result: RunResult) -> float:
        return self.evaluate_sync(result).overall


async def _seed_orchestrator_with_failing_history(
    orch: MetaOrchestrator,
    *,
    agent_id: str,
    n: int = 3,
) -> None:
    """Populate ``run_history`` with runs carrying matching error events.

    ``PromptRewriteMutator.propose`` needs (a) ``result.errors`` non-empty
    so the error-frequency Counter surfaces a pattern, and (b)
    ``event.error`` populated on at least one event tagged with the agent
    id so ``_pick_target_agent`` returns a real id (not ``None``). Both
    conditions must hold for a candidate to be proposed.
    """
    for i in range(n):
        orch._run_history.append(
            RunResult(
                task_id=f"seed-{i}",
                run_id=f"run-{i}",
                status=RunStatus.COMPLETED,
                cost=CostSummary(total_cost=Decimal("0.01"), llm_calls=1),
                duration_ms=100.0,
                errors=["timeout exceeded"],
                events=[
                    RunEvent(
                        kind=RunEventKind.ERROR,
                        run_id=f"run-{i}",
                        agent_id=agent_id,
                        error="timeout exceeded",
                        data={"message": "timeout exceeded"},
                    ),
                    RunEvent(
                        kind=RunEventKind.AGENT_START,
                        run_id=f"run-{i}",
                        agent_id=agent_id,
                        data={"input": {"q": f"seed-{i}"}},
                    ),
                ],
            )
        )


def _seed_history_with_expensive_model(
    orch: MetaOrchestrator,
    *,
    agent_id: str,
    model: str,
    n: int = 5,
) -> None:
    """Seed history that makes :class:`ModelSwapMutator` propose a downgrade.

    The mutator scans the last five runs' ``LLM_CALL`` events for expensive
    models; we emit events with ``event.model`` and ``event.agent_id``
    populated so the filter fires.
    """
    for i in range(n):
        orch._run_history.append(
            RunResult(
                task_id=f"exp-{i}",
                run_id=f"exp-run-{i}",
                status=RunStatus.COMPLETED,
                cost=CostSummary(
                    total_cost=Decimal("1.00"),
                    cost_by_model={model: Decimal("1.00")},
                    cost_by_agent={agent_id: Decimal("1.00")},
                    llm_calls=2,
                ),
                duration_ms=5000.0,
                events=[
                    RunEvent(
                        kind=RunEventKind.LLM_CALL,
                        run_id=f"exp-run-{i}",
                        agent_id=agent_id,
                        model=model,
                        input_tokens=1000,
                        output_tokens=500,
                        cost=Decimal("0.50"),
                    ),
                    RunEvent(
                        kind=RunEventKind.LLM_CALL,
                        run_id=f"exp-run-{i}",
                        agent_id=agent_id,
                        model=model,
                        input_tokens=800,
                        output_tokens=400,
                        cost=Decimal("0.50"),
                    ),
                ],
            )
        )


def _install_agent(orch: MetaOrchestrator, agent: AgentCard) -> None:
    """Force a specific agent into the adapter's topology for tests."""
    assert orch._adapter is not None
    orch._adapter._agents = [agent]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Suggest mode: no mutation is ever applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_mode_returns_candidate_without_applying() -> None:
    """In ``mode='suggest'``, step() returns the proposal; topology stays identical."""
    adapter = MockLLMFlowAdapter(num_llm_calls=1)
    orch = MetaOrchestrator(adapter=adapter)
    await adapter.load("fake")
    target = AgentCard(
        id="agent-planner",
        name="planner",
        role="planning",
        model="claude-haiku-4-20250514",
        system_prompt="You plan things.",
    )
    _install_agent(orch, target)

    await _seed_orchestrator_with_failing_history(orch, agent_id="planner", n=3)
    pre_snapshot = orch.capture_snapshot()

    loop = EvolutionLoop(orchestrator=orch, mode="suggest")
    candidate = await loop.step()

    # A candidate was proposed (the fixture history guarantees one).
    assert candidate is not None
    # But the orchestrator's topology is unchanged.
    post_snapshot = orch.capture_snapshot()
    assert pre_snapshot.content_hash == post_snapshot.content_hash


# ---------------------------------------------------------------------------
# Auto mode — commit path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_mode_commits_on_fitness_improvement() -> None:
    """Fitness after > before ⇒ mutation committed, topology reflects the change."""
    adapter = MockLLMFlowAdapter(num_llm_calls=1, agent_names=("planner",))
    orch = MetaOrchestrator(adapter=adapter)
    await adapter.load("fake")
    target = AgentCard(
        id="agent-planner",
        name="planner",
        role="planning",
        model="claude-haiku-4-20250514",
        system_prompt="You plan things short.",
    )
    _install_agent(orch, target)

    await _seed_orchestrator_with_failing_history(orch, agent_id="planner", n=3)

    fake_llm = FakeLLMClient()
    fake_llm.queue(
        PromptRewriteResponse,
        {
            "rewritten_prompt": "You are a careful planner. Always check constraints, cite sources, and respect timeouts explicitly."
        },
    )
    mutator = PromptRewriteMutator(llm_client=fake_llm)

    # Evaluator sequence: low baseline, high post-mutation → COMMIT.
    scripted = ScriptedEvaluator(scores=[0.30, 0.90])

    loop = EvolutionLoop(
        orchestrator=orch,
        mode="auto",
        mutators=[mutator],
        evaluator=scripted,
        # Tight bogus suite that runs once per eval phase (cheap).
        eval_suite=EvalSuite.from_cases([EvalCase(case_id="probe", input={"q": "probe"})]),
    )

    result = await loop.step()
    assert result is not None, "Committed mutation expected"

    # Topology now carries the rewritten prompt.
    live = orch.topology()
    assert len(live) == 1
    assert "careful planner" in (live[0].system_prompt or "")

    # Journal recorded the successful apply.
    records = loop.journal.recent(limit=10)
    kinds = {r.kind for r in records}
    assert "applied" in kinds


# ---------------------------------------------------------------------------
# Auto mode — rollback path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_mode_rolls_back_on_fitness_regression() -> None:
    """Fitness after < before by more than threshold ⇒ mutation rolled back.

    The critical assertion is that after rollback, the orchestrator's
    current ``content_hash`` matches the pre-mutation snapshot exactly —
    proving the atomic snapshot-based rollback primitive (β.1 fix for
    Gap #6) works end-to-end inside the loop.
    """
    adapter = MockLLMFlowAdapter(num_llm_calls=1, agent_names=("planner",))
    orch = MetaOrchestrator(adapter=adapter)
    await adapter.load("fake")
    target = AgentCard(
        id="agent-planner",
        name="planner",
        role="planning",
        model="claude-haiku-4-20250514",
        system_prompt="Original prompt that works fine.",
    )
    _install_agent(orch, target)

    await _seed_orchestrator_with_failing_history(orch, agent_id="planner", n=3)
    pre_hash = orch.capture_snapshot().content_hash

    fake_llm = FakeLLMClient()
    fake_llm.queue(
        PromptRewriteResponse,
        {"rewritten_prompt": "A much worse prompt that causes regressions everywhere."},
    )
    mutator = PromptRewriteMutator(llm_client=fake_llm)

    # Evaluator sequence: high baseline, low post-mutation → ROLLBACK.
    scripted = ScriptedEvaluator(scores=[0.90, 0.30])

    loop = EvolutionLoop(
        orchestrator=orch,
        mode="auto",
        mutators=[mutator],
        evaluator=scripted,
        rollback_threshold=0.05,
        eval_suite=EvalSuite.from_cases([EvalCase(case_id="probe", input={"q": "probe"})]),
    )

    result = await loop.step()
    assert result is None, "Rollback expected (fitness dropped below threshold)"

    # After rollback, the topology is bit-identical to the pre-mutation state.
    post_hash = orch.capture_snapshot().content_hash
    assert pre_hash == post_hash, (
        f"Rollback failed to restore snapshot exactly.\npre_hash={pre_hash}\npost_hash={post_hash}"
    )

    # Journal records the rolled-back mutation.
    records = loop.journal.recent(limit=10)
    kinds = {r.kind for r in records}
    assert "rolled_back" in kinds


# ---------------------------------------------------------------------------
# Session limit guardrail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_limit_halts_further_steps() -> None:
    """``max_mutations`` caps how many applies happen in one session."""
    adapter = MockLLMFlowAdapter(num_llm_calls=1, agent_names=("planner",))
    orch = MetaOrchestrator(adapter=adapter)
    await adapter.load("fake")
    target = AgentCard(
        id="agent-planner",
        name="planner",
        role="planning",
        model="claude-opus-4-20250514",  # expensive, so ModelSwap proposes downgrade
        system_prompt="Plan carefully.",
    )
    _install_agent(orch, target)

    _seed_history_with_expensive_model(
        orch, agent_id="agent-planner", model="claude-opus-4-20250514", n=5
    )

    # Scripted evaluator keeps committing (always better).
    scripted = ScriptedEvaluator(scores=[0.20, 0.80])
    mutator = ModelSwapMutator()

    loop = EvolutionLoop(
        orchestrator=orch,
        mode="auto",
        max_mutations=1,
        mutators=[mutator],
        evaluator=scripted,
        eval_suite=EvalSuite.from_cases([EvalCase(case_id="probe", input={"q": "probe"})]),
    )

    r1 = await loop.step()
    r2 = await loop.step()  # Blocked by session limit regardless of propose.

    assert r1 is not None, "First step should have committed"
    assert r2 is None, "Session limit should halt the second step"


# ---------------------------------------------------------------------------
# Mutator failure safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutator_exception_restores_snapshot() -> None:
    """If mutator.apply() raises, the pre-snapshot is restored and the step is skipped."""
    adapter = MockLLMFlowAdapter(num_llm_calls=1, agent_names=("planner",))
    orch = MetaOrchestrator(adapter=adapter)
    await adapter.load("fake")
    target = AgentCard(
        id="agent-planner", name="planner", role="planning", model="claude-haiku-4-20250514"
    )
    _install_agent(orch, target)

    pre_hash = orch.capture_snapshot().content_hash

    # Seed a couple of runs so EvolutionLoop.step() passes the
    # ``len(run_history) < 2`` guard and reaches the mutator path.
    for i in range(2):
        orch._run_history.append(
            RunResult(
                task_id=f"t{i}",
                run_id=f"r{i}",
                status=RunStatus.COMPLETED,
                cost=CostSummary(total_cost=Decimal("0.01"), llm_calls=1),
                duration_ms=100.0,
            )
        )

    class ExplodingMutator:
        mutation_kind = MutationKind.PARAMETER_TUNE

        def propose(self, run_history: list[RunResult]) -> Mutation | None:
            return Mutation(
                kind=MutationKind.PARAMETER_TUNE,
                description="boom",
                confidence=1.0,
                diff=[
                    MutationDiff(
                        field_path="config.max_steps", before=100, after=80, description="trim"
                    )
                ],
            )

        async def apply(self, mutation: Mutation, state: Any) -> Any:
            raise RuntimeError("mutator explosion")

        async def rollback(self, mutation: Mutation, state: Any) -> None:
            return None

    loop = EvolutionLoop(
        orchestrator=orch,
        mode="auto",
        mutators=[ExplodingMutator()],
        evaluator=ScriptedEvaluator(scores=[0.5, 0.5]),
        eval_suite=EvalSuite.from_cases([EvalCase(case_id="probe", input={"q": "probe"})]),
    )

    result = await loop.step()
    assert result is None, "Failed apply should return None"

    post_hash = orch.capture_snapshot().content_hash
    assert pre_hash == post_hash, "Snapshot was not restored after mutator crash"

    # Journal logged a skipped mutation with the error reason.
    records = loop.journal.recent(limit=10)
    kinds = {r.kind for r in records}
    assert "skipped" in kinds


# ---------------------------------------------------------------------------
# Journal provenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_captures_before_and_after_fitness() -> None:
    """An applied mutation's journal record carries both fitness scores."""
    adapter = MockLLMFlowAdapter(num_llm_calls=1, agent_names=("worker",))
    orch = MetaOrchestrator(adapter=adapter)
    await adapter.load("fake")
    target = AgentCard(
        id="agent-worker", name="worker", role="work", model="claude-haiku-4-20250514"
    )
    _install_agent(orch, target)

    orch._run_history.append(
        RunResult(
            task_id="t",
            run_id="r0",
            status=RunStatus.COMPLETED,
            cost=CostSummary(total_cost=Decimal("0.10"), llm_calls=1),
            duration_ms=1000.0,
        )
    )
    orch._run_history.append(
        RunResult(
            task_id="t2",
            run_id="r1",
            status=RunStatus.COMPLETED,
            cost=CostSummary(total_cost=Decimal("0.10"), llm_calls=1),
            duration_ms=1200.0,
        )
    )

    ParameterTuneMutator()
    # ParameterTuneMutator.propose requires timeouts in history; force a mutation:
    candidate = Mutation(
        kind=MutationKind.PARAMETER_TUNE,
        description="increase timeout",
        confidence=0.8,
        diff=[
            MutationDiff(
                field_path="config.timeout_seconds", before=60, after=90, description="bump"
            )
        ],
    )

    class DirectProposeMutator(ParameterTuneMutator):
        def propose(self, run_history: list[RunResult]) -> Mutation | None:
            return candidate

    scripted = ScriptedEvaluator(scores=[0.40, 0.75])
    loop = EvolutionLoop(
        orchestrator=orch,
        mode="auto",
        mutators=[DirectProposeMutator()],
        evaluator=scripted,
        eval_suite=EvalSuite.from_cases([EvalCase(case_id="probe", input={"q": "probe"})]),
    )

    applied = await loop.step()
    assert applied is not None

    # The "applied with fitness_after" record is the last one for this mutation.
    records = [r for r in loop.journal.recent(limit=20) if r.mutation.id == candidate.id]
    assert records, "Journal should have recorded this mutation"
    # `recent()` returns newest-first; find the final "applied" record.
    applied_records = [r for r in records if r.kind == "applied"]
    assert applied_records, "No 'applied' record found"
    final = applied_records[0]
    assert final.fitness_before is not None
    assert final.fitness_after is not None

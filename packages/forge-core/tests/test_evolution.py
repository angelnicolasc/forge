"""Tests for the Forge self-evolution system."""

from __future__ import annotations

from decimal import Decimal

import pytest

from forge_core.evolution.evaluator import DefaultEvaluator
from forge_core.evolution.journal import EvolutionJournal
from forge_core.evolution.mutations import (
    AgentCullMutator,
    ModelSwapMutator,
    PromptRewriteMutator,
)
from forge_core.types import (
    AgentCard,
    CostSummary,
    FitnessScore,
    Mutation,
    MutationKind,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
)

# ---------------------------------------------------------------------------
# Evaluator tests
# ---------------------------------------------------------------------------


class TestDefaultEvaluator:
    def make_result(
        self,
        status: str = "completed",
        cost: float = 0.05,
        duration_ms: float = 2000.0,
        errors: list[str] | None = None,
    ) -> RunResult:
        return RunResult(
            task_id="test",
            status=RunStatus(status),
            duration_ms=duration_ms,
            cost=CostSummary(total_cost=Decimal(str(cost))),
            errors=errors or [],
        )

    @pytest.mark.asyncio
    async def test_completed_run_scores_positively(self) -> None:
        ev = DefaultEvaluator()
        result = self.make_result(cost=0.01, duration_ms=500)
        score = await ev.evaluate(result)
        assert 0.5 < score <= 1.0

    @pytest.mark.asyncio
    async def test_failed_run_scores_zero(self) -> None:
        ev = DefaultEvaluator()
        result = self.make_result(status="failed")
        score = await ev.evaluate(result)
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_expensive_run_scores_lower(self) -> None:
        ev = DefaultEvaluator()
        cheap = self.make_result(cost=0.001)
        expensive = self.make_result(cost=1.0)
        cheap_score = await ev.evaluate(cheap)
        expensive_score = await ev.evaluate(expensive)
        assert cheap_score > expensive_score

    def test_fitness_score_breakdown(self) -> None:
        ev = DefaultEvaluator()
        result = self.make_result(cost=0.05, duration_ms=3000)
        fitness = ev.evaluate_sync(result)
        assert isinstance(fitness, FitnessScore)
        assert 0 <= fitness.overall <= 1
        assert 0 <= fitness.cost_score <= 1
        assert 0 <= fitness.latency_score <= 1
        assert 0 <= fitness.quality_score <= 1


# ---------------------------------------------------------------------------
# Mutator tests
# ---------------------------------------------------------------------------


def make_results_with_errors(error: str, count: int) -> list[RunResult]:
    """Create a list of failed run results with a specific error."""
    results = []
    for i in range(count):
        results.append(
            RunResult(
                task_id=f"test-{i}",
                status=RunStatus.FAILED,
                errors=[error],
                cost=CostSummary(total_cost=Decimal("0.05")),
            )
        )
    return results


class TestPromptRewriteMutator:
    def test_no_proposal_with_single_run(self) -> None:
        mutator = PromptRewriteMutator()
        results = make_results_with_errors("timeout error", 1)
        assert mutator.propose(results) is None

    def test_proposes_on_repeated_error(self) -> None:
        mutator = PromptRewriteMutator()
        results = make_results_with_errors("Rate limit exceeded", 3)
        proposal = mutator.propose(results)
        assert proposal is not None
        assert proposal.kind == MutationKind.PROMPT_REWRITE
        assert proposal.confidence > 0.3

    def test_no_proposal_without_errors(self) -> None:
        mutator = PromptRewriteMutator()
        results = [RunResult(task_id=f"t{i}", status=RunStatus.COMPLETED) for i in range(5)]
        assert mutator.propose(results) is None


class TestModelSwapMutator:
    def make_results_with_expensive_model(self) -> list[RunResult]:
        results = []
        for i in range(4):
            event = RunEvent(
                kind=RunEventKind.LLM_CALL,
                agent_id="agent-abc",
                model="claude-opus-4-20250514",
                input_tokens=1000,
                output_tokens=200,
                cost=Decimal("0.10"),
                run_id=f"run-{i}",
            )
            results.append(
                RunResult(
                    task_id=f"test-{i}",
                    events=[event],
                    cost=CostSummary(total_cost=Decimal("0.10")),
                )
            )
        return results

    def test_proposes_model_downgrade(self) -> None:
        mutator = ModelSwapMutator()
        results = self.make_results_with_expensive_model()
        proposal = mutator.propose(results)
        assert proposal is not None
        assert proposal.kind == MutationKind.MODEL_SWAP
        assert "claude-sonnet" in proposal.description.lower() or "sonnet" in proposal.description

    def test_no_proposal_with_cheap_models(self) -> None:
        mutator = ModelSwapMutator()
        results = []
        for i in range(4):
            event = RunEvent(
                kind=RunEventKind.LLM_CALL,
                agent_id="agent-abc",
                model="claude-haiku-4-20250514",  # Already cheap
                cost=Decimal("0.001"),
                run_id=f"run-{i}",
            )
            results.append(RunResult(task_id=f"t{i}", events=[event]))
        assert mutator.propose(results) is None


class TestAgentCullMutator:
    def test_no_proposal_with_few_runs(self) -> None:
        mutator = AgentCullMutator()
        results = [RunResult(task_id=f"t{i}") for i in range(2)]
        assert mutator.propose(results) is None

    def test_proposes_cull_for_inactive_agent(self) -> None:
        mutator = AgentCullMutator()
        agents = [
            AgentCard(id="active-agent", name="Active", role="worker"),
            AgentCard(id="idle-agent", name="Idle", role="unused"),
        ]
        # Runs where only active-agent emits LLM calls
        results = []
        for i in range(5):
            event = RunEvent(
                kind=RunEventKind.LLM_CALL,
                agent_id="active-agent",
                input_tokens=100,
                output_tokens=50,
                run_id=f"run-{i}",
            )
            results.append(
                RunResult(
                    task_id=f"t{i}",
                    events=[event],
                    topology=agents,
                )
            )
        proposal = mutator.propose(results)
        assert proposal is not None
        assert proposal.kind == MutationKind.AGENT_CULL
        assert "idle-agent" in proposal.description


# ---------------------------------------------------------------------------
# Evolution Journal tests
# ---------------------------------------------------------------------------


class TestEvolutionJournal:
    def make_mutation(self, kind: MutationKind = MutationKind.PROMPT_REWRITE) -> Mutation:
        return Mutation(
            kind=kind,
            description="Test mutation",
            confidence=0.7,
        )

    def test_record_and_retrieve(self, tmp_path: object) -> None:
        journal = EvolutionJournal(persist_path=f"{tmp_path}/journal.jsonl")
        mutation = self.make_mutation()
        entry = journal.record(mutation, kind="proposed")
        assert entry.mutation.id == mutation.id
        assert entry.kind == "proposed"

    def test_recent_filtering(self, tmp_path: object) -> None:
        journal = EvolutionJournal(persist_path=f"{tmp_path}/journal.jsonl")
        m1 = self.make_mutation(MutationKind.PROMPT_REWRITE)
        m2 = self.make_mutation(MutationKind.MODEL_SWAP)
        journal.record(m1, kind="proposed")
        journal.record(m2, kind="applied")

        proposed = journal.recent(kind="proposed")
        assert len(proposed) == 1
        assert proposed[0].mutation.id == m1.id

        all_entries = journal.recent()
        assert len(all_entries) == 2

    def test_stats(self, tmp_path: object) -> None:
        journal = EvolutionJournal(persist_path=f"{tmp_path}/journal.jsonl")
        mutation = self.make_mutation()
        journal.record(mutation, kind="proposed")
        journal.record(mutation, kind="applied")
        journal.record(mutation, kind="rolled_back")
        stats = journal.stats()
        assert stats.get("proposed", 0) == 1
        assert stats.get("applied", 0) == 1
        assert stats.get("rolled_back", 0) == 1

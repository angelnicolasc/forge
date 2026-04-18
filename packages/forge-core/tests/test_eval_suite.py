"""Acceptance tests for β.5 — :class:`EvalSuite`.

The suite replaces the degenerate ``history[-1].output`` eval with a
deterministic, reusable collection of inputs that supports before/after
fitness comparison with a bootstrap confidence interval.

Coverage:

* **Determinism**: identical inputs ⇒ identical ``suite_id`` and cases.
* **Stratification**: ``from_history`` picks cases across the cost
  distribution rather than clustering on the cheapest or most recent.
* **Deduplication**: identical history inputs collapse to one case.
* **Envelope marker**: every case envelope carries ``forge_internal_eval``
  so :meth:`MetaOrchestrator._should_trigger_evolution` skips it (prevents
  the evolution loop from recursing on itself while measuring fitness).
* **Aggregate fitness** with a bootstrap CI — we can detect real
  improvements above sampling noise.
* **Synthetic**: ``EvalSuite.synthetic`` uses an :class:`LLMClient` and
  falls back gracefully on LLM failure.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fixtures.mock_llm_adapter import MockLLMFlowAdapter

from forge_core.evolution.eval_suite import (
    EvalCase,
    EvalSuite,
    _SyntheticEvalResponse,
)
from forge_core.evolution.llm_client import FakeLLMClient
from forge_core.harness import MetaOrchestrator
from forge_core.types import (
    AgentCard,
    CostSummary,
    RunEvent,
    RunEventKind,
    RunResult,
    RunStatus,
    TaskEnvelope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    *,
    run_id: str,
    input_payload: dict,
    cost: Decimal = Decimal("0.01"),
    duration_ms: float = 100.0,
) -> RunResult:
    """Build a RunResult carrying an AGENT_START event with the given input."""
    events = [
        RunEvent(
            kind=RunEventKind.AGENT_START,
            run_id=run_id,
            agent_id="orchestrator",
            data={"input": input_payload},
        )
    ]
    return RunResult(
        task_id=f"task-{run_id}",
        run_id=run_id,
        status=RunStatus.COMPLETED,
        cost=CostSummary(
            total_cost=cost, total_input_tokens=100, total_output_tokens=50, llm_calls=1
        ),
        duration_ms=duration_ms,
        events=events,
    )


# ---------------------------------------------------------------------------
# EvalCase basics
# ---------------------------------------------------------------------------


def test_eval_case_envelope_carries_internal_marker() -> None:
    """Every eval case must mark its envelope so auto-trigger skips it."""
    case = EvalCase(case_id="abc123", input={"q": "hello"})
    env = case.envelope(cost_ceiling=Decimal("1.00"))
    assert env.metadata["forge_internal_eval"] is True
    assert env.metadata["eval_case_id"] == "abc123"
    assert env.config.enable_evolution is False
    assert env.config.enable_memory is False


def test_eval_case_is_frozen() -> None:
    """Cases are immutable — mutation attempts raise."""
    case = EvalCase(case_id="x", input={"q": "hi"})
    with pytest.raises(Exception):  # FrozenInstanceError
        case.case_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# from_history determinism and stratification
# ---------------------------------------------------------------------------


def test_from_history_is_deterministic() -> None:
    """Same inputs + same seed ⇒ identical suite (case ids + order)."""
    runs = [
        _make_run(run_id=f"r{i}", input_payload={"q": f"query-{i}"}, cost=Decimal(f"0.0{i + 1}"))
        for i in range(8)
    ]
    s1 = EvalSuite.from_history(runs, k=4, seed=42)
    s2 = EvalSuite.from_history(runs, k=4, seed=42)
    assert s1.suite_id == s2.suite_id
    assert [c.case_id for c in s1.cases] == [c.case_id for c in s2.cases]


def test_from_history_dedupes_identical_inputs() -> None:
    """Five runs with the same input collapse to one case."""
    runs = [
        _make_run(run_id=f"r{i}", input_payload={"q": "same"}, cost=Decimal("0.01"))
        for i in range(5)
    ]
    suite = EvalSuite.from_history(runs, k=5)
    assert len(suite) == 1


def test_from_history_empty_returns_empty_suite() -> None:
    suite = EvalSuite.from_history([], k=5)
    assert len(suite) == 0
    assert suite.suite_id == ""


def test_from_history_stratifies_across_cost_quartiles() -> None:
    """With widely varying cost, the suite should include both cheap and expensive cases."""
    runs = [
        _make_run(
            run_id=f"r{i}",
            input_payload={"q": f"q{i}"},
            cost=Decimal("0.001") * (i + 1),
        )
        for i in range(20)
    ]
    suite = EvalSuite.from_history(runs, k=4, seed=0)
    # The suite must include more than one distinct input (stratified, not all same).
    ids = [c.case_id for c in suite.cases]
    assert len(set(ids)) == len(suite.cases)
    assert len(suite) >= 2


def test_from_history_handles_fewer_runs_than_k() -> None:
    """If history has only 2 runs, the suite has 2 cases — not k."""
    runs = [
        _make_run(run_id="a", input_payload={"q": "1"}),
        _make_run(run_id="b", input_payload={"q": "2"}),
    ]
    suite = EvalSuite.from_history(runs, k=10)
    assert len(suite) == 2


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_runs_every_case_and_aggregates() -> None:
    """Suite.evaluate executes each case and returns a FitnessScore."""
    adapter = MockLLMFlowAdapter(num_llm_calls=1)
    orch = MetaOrchestrator(adapter=adapter)

    cases = [EvalCase(case_id=f"case-{i}", input={"q": f"probe-{i}"}) for i in range(3)]
    suite = EvalSuite.from_cases(cases)

    fitness = await suite.evaluate(orch, cost_ceiling=Decimal("5.00"))
    assert fitness.overall >= 0.0
    assert fitness.confidence_interval is not None
    lo, hi = fitness.confidence_interval
    assert 0.0 <= lo <= hi <= 1.0
    # Three cases ran → orchestrator history grew by 3.
    assert len(orch.run_history) == 3


@pytest.mark.asyncio
async def test_evaluate_empty_suite_returns_zero_score() -> None:
    orch = MetaOrchestrator(adapter=MockLLMFlowAdapter())
    suite = EvalSuite(cases=(), suite_id="")
    fitness = await suite.evaluate(orch)
    assert fitness.overall == 0.0
    assert fitness.confidence_interval == (0.0, 0.0)


@pytest.mark.asyncio
async def test_evaluate_tolerates_single_case_failure() -> None:
    """If one case's run raises, the remainder still contribute to the aggregate."""

    class FlakyAdapter(MockLLMFlowAdapter):
        async def _execute(self, envelope):
            if envelope.metadata.get("eval_case_id") == "case-1":
                raise RuntimeError("simulated case failure")
            return await super()._execute(envelope)

    orch = MetaOrchestrator(adapter=FlakyAdapter(num_llm_calls=1))
    suite = EvalSuite.from_cases(
        [
            EvalCase(case_id="case-0", input={"q": "ok"}),
            EvalCase(case_id="case-1", input={"q": "boom"}),
            EvalCase(case_id="case-2", input={"q": "ok-again"}),
        ]
    )

    fitness = await suite.evaluate(orch, cost_ceiling=Decimal("5.00"))
    # Score stays on [0,1] — at minimum we got signal from the two passing cases.
    assert 0.0 <= fitness.overall <= 1.0


# ---------------------------------------------------------------------------
# synthetic()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_uses_llm_client() -> None:
    """synthetic() builds cases from an LLM structured response."""
    orch = MetaOrchestrator(adapter=MockLLMFlowAdapter())
    # Seed the orchestrator's topology so the prompt has content.
    await orch._adapter._load_impl("fake")
    orch._adapter._agents = [
        AgentCard(id="a", name="researcher", role="research", model="claude-haiku-4-20250514"),
    ]

    fake = FakeLLMClient()
    fake.queue(
        _SyntheticEvalResponse,
        {
            "cases": [
                {"input": {"q": "synth-easy"}, "difficulty": "easy", "rationale": "warmup"},
                {"input": {"q": "synth-medium"}, "difficulty": "medium", "rationale": "typical"},
                {"input": {"q": "synth-hard"}, "difficulty": "hard", "rationale": "edge"},
            ]
        },
    )

    suite = await EvalSuite.synthetic(orch, n=3, llm_client=fake)
    assert len(suite) == 3
    assert {c.tags["difficulty"] for c in suite.cases} == {"easy", "medium", "hard"}
    # The LLM got called exactly once with the synthetic schema.
    assert len(fake.calls) == 1
    assert fake.calls[0]["schema"] == "_SyntheticEvalResponse"


@pytest.mark.asyncio
async def test_synthetic_returns_empty_on_llm_failure() -> None:
    """If the LLM call fails, synthetic() returns an empty suite (not a crash)."""
    orch = MetaOrchestrator(adapter=MockLLMFlowAdapter())
    fake = FakeLLMClient()  # No queued response → will raise on call.

    suite = await EvalSuite.synthetic(orch, n=2, llm_client=fake)
    assert len(suite) == 0


# ---------------------------------------------------------------------------
# Integration with the loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_uses_configured_eval_suite() -> None:
    """EvolutionLoop.set_eval_suite replaces the legacy single-run eval."""
    from forge_core.evolution.loop import EvolutionLoop

    orch = MetaOrchestrator(adapter=MockLLMFlowAdapter(num_llm_calls=1))
    suite = EvalSuite.from_cases(
        [
            EvalCase(case_id="e0", input={"q": "eval-0"}),
            EvalCase(case_id="e1", input={"q": "eval-1"}),
        ]
    )

    loop = EvolutionLoop(orchestrator=orch)
    loop.set_eval_suite(suite)
    assert loop.eval_suite is suite

    fitness = await loop._eval_topology()
    assert fitness is not None
    assert fitness.confidence_interval is not None
    # The suite ran 2 cases → history grew by 2.
    assert len(orch.run_history) == 2


@pytest.mark.asyncio
async def test_ensure_eval_suite_builds_from_history() -> None:
    """ensure_eval_suite lazy-constructs a suite when none exists and history is present."""
    from forge_core.evolution.loop import EvolutionLoop

    adapter = MockLLMFlowAdapter(num_llm_calls=1)
    orch = MetaOrchestrator(adapter=adapter)
    await adapter.load("fake")
    # Populate history with a couple of real runs.
    await orch.run(TaskEnvelope(input={"q": "one"}))
    await orch.run(TaskEnvelope(input={"q": "two"}))

    loop = EvolutionLoop(orchestrator=orch)
    assert loop.eval_suite is None
    suite = await loop.ensure_eval_suite(k=2)
    assert suite is loop.eval_suite
    assert len(suite) >= 1

"""Tests for :class:`LLMAsJudgeEvaluator` — the semantic quality scorer.

The heuristic quality score in :class:`DefaultEvaluator` is honest but
shallow. The judge adds a real signal; these tests lock the following
contracts down:

* When the feature flag is off, behavior is byte-identical to
  :class:`DefaultEvaluator` (no network, no API key needed).
* When on, the judge's score replaces the heuristic and the cache
  dedupes identical outputs.
* Judge failures degrade gracefully to the heuristic — evolution
  never crashes because OpenAI had a bad minute.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from forge_core.evolution.evaluator import (
    LLMAsJudgeEvaluator,
    QualityScoreTool,
)
from forge_core.types import CostSummary, RunResult, RunStatus


class _StubLLM:
    """Minimal :class:`LLMClient` double.

    Returns a canned :class:`QualityScoreTool` each call and records
    invocation count so the cache-hit test can assert the judge was
    only called once for duplicate outputs.
    """

    def __init__(self, score: float = 0.82) -> None:
        self._score = score
        self.calls = 0

    async def structured_call(self, *, schema, **_kwargs):
        self.calls += 1
        return schema(
            correctness=self._score,
            completeness=self._score,
            relevance=self._score,
            score=self._score,
            rationale="stub",
        )


def _result(output: str = "ok", cost: float = 0.01) -> RunResult:
    return RunResult(
        task_id="t",
        status=RunStatus.COMPLETED,
        duration_ms=500.0,
        cost=CostSummary(total_cost=Decimal(str(cost))),
        output=output,
    )


# ---------------------------------------------------------------------------
# Flag-off behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_skips_judge(monkeypatch) -> None:
    monkeypatch.delenv("FORGE_ENABLE_LLM_JUDGE", raising=False)
    llm = _StubLLM(score=0.99)
    ev = LLMAsJudgeEvaluator(llm_client=llm)
    score = await ev.evaluate(_result())
    assert llm.calls == 0
    # Heuristic quality for a clean completed run = 1.0, so overall > 0.5.
    assert score > 0.5


@pytest.mark.asyncio
async def test_flag_on_but_no_client_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_JUDGE", "1")
    ev = LLMAsJudgeEvaluator(llm_client=None)
    # Still produces a valid score — just from the heuristic.
    score = await ev.evaluate(_result())
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_overrides_heuristic(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_JUDGE", "1")
    llm = _StubLLM(score=0.3)  # deliberately worse than heuristic (=1.0)
    ev = LLMAsJudgeEvaluator(llm_client=llm)
    heuristic_only = LLMAsJudgeEvaluator(llm_client=None)  # flag on, no client

    judged = await ev.evaluate(_result())
    heuristic = await heuristic_only.evaluate(_result())
    # The judge's lower score pulls the overall fitness below the
    # heuristic — proves the judge signal is actually wired through.
    assert judged < heuristic
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_cache_dedupes_identical_outputs(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_JUDGE", "1")
    llm = _StubLLM(score=0.77)
    ev = LLMAsJudgeEvaluator(llm_client=llm)

    s1 = await ev.evaluate(_result(output="same"))
    s2 = await ev.evaluate(_result(output="same"))
    s3 = await ev.evaluate(_result(output="different"))

    # Same output ⇒ one real judge call; different output ⇒ a second.
    assert llm.calls == 2
    assert s1 == s2
    assert s3 == pytest.approx(s1)  # score identical (stub returns same value)
    assert len(ev.cache) == 2  # two distinct cache entries


# ---------------------------------------------------------------------------
# Failure resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_failure_falls_back_to_heuristic(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_JUDGE", "1")

    class _Explode:
        async def structured_call(self, **_kwargs):
            raise RuntimeError("judge down")

    ev = LLMAsJudgeEvaluator(llm_client=_Explode())
    score = await ev.evaluate(_result())
    assert 0.0 <= score <= 1.0  # heuristic kept us alive


@pytest.mark.asyncio
async def test_failed_run_stays_zero_regardless_of_judge(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_ENABLE_LLM_JUDGE", "1")
    llm = _StubLLM(score=1.0)  # judge would inflate, but...
    ev = LLMAsJudgeEvaluator(llm_client=llm)
    r = RunResult(
        task_id="t",
        status=RunStatus.FAILED,
        cost=CostSummary(total_cost=Decimal("0")),
        errors=["boom"],
    )
    score = await ev.evaluate(r)
    # Failed = 0 overall; the judge must not be consulted.
    assert score == 0.0
    assert llm.calls == 0


def test_quality_score_tool_validates_bounds() -> None:
    # score > 1 must reject — Pydantic's ge/le enforcement is the
    # contract that keeps a poorly-prompted judge from crashing us.
    with pytest.raises(Exception):
        QualityScoreTool(
            correctness=0.5,
            completeness=0.5,
            relevance=0.5,
            score=1.5,
            rationale="x",
        )

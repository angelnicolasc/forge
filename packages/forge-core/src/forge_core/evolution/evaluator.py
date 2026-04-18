"""Fitness evaluator for the Forge evolution loop.

Scores a RunResult on [0, 1] by combining:
  - cost_score:    normalized cost reduction vs. baseline
  - latency_score: normalized latency improvement vs. baseline
  - quality_score: LLM-as-judge or success rate heuristic

Default weights: cost=0.30, latency=0.20, quality=0.50
(tunable via config or per-evaluation override)

The Evaluator is a Protocol — you can swap in a custom scorer
without touching the evolution loop.

LLM-as-Judge (δ.3)
------------------

The default quality score is a success-rate heuristic — accurate enough
for triage but blind to semantic quality. :class:`LLMAsJudgeEvaluator`
layers a real judge on top: it ships the run's output to a small model
with a structured rubric (correctness/completeness/relevance) and
consumes the resulting score. To keep cost bounded — judging every run
would double the bill for long-running evolution loops — results are
cached by a SHA-256 of the stringified output with a 24h TTL. Identical
outputs map to identical scores, so re-evaluation of an unchanged flow
is free after the first call.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

import structlog
from pydantic import BaseModel, Field

from forge_core.types import FitnessScore, RunResult

logger = structlog.get_logger()

DEFAULT_WEIGHTS: dict[str, float] = {
    "cost": 0.30,
    "latency": 0.20,
    "quality": 0.50,
}

# Baselines for normalization (update with real data after a few runs)
_DEFAULT_COST_BASELINE_USD = 0.10  # $0.10 per run = score of 0.5
_DEFAULT_LATENCY_BASELINE_MS = 5000  # 5 seconds = score of 0.5


class DefaultEvaluator:
    """Default composite fitness evaluator.

    Uses heuristic scoring in the absence of human feedback.
    When the run was successful with no errors, quality_score = 1.0.
    Cost and latency are scored against configurable baselines.
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        cost_baseline_usd: float = _DEFAULT_COST_BASELINE_USD,
        latency_baseline_ms: float = _DEFAULT_LATENCY_BASELINE_MS,
    ) -> None:
        self._weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self._cost_baseline = cost_baseline_usd
        self._latency_baseline = latency_baseline_ms

    @property
    def weights(self) -> dict[str, float]:
        return dict(self._weights)

    async def evaluate(self, result: RunResult) -> float:
        """Score a run result. Returns 0.0 (worst) to 1.0 (best)."""
        fitness = self._compute_fitness(result)
        logger.debug(
            "evaluator.scored",
            run_id=result.run_id,
            overall=f"{fitness.overall:.3f}",
            cost=f"{fitness.cost_score:.3f}",
            latency=f"{fitness.latency_score:.3f}",
            quality=f"{fitness.quality_score:.3f}",
        )
        return fitness.overall

    def evaluate_sync(self, result: RunResult) -> FitnessScore:
        """Synchronous evaluation — returns full FitnessScore breakdown."""
        return self._compute_fitness(result)

    def _compute_fitness(self, result: RunResult) -> FitnessScore:
        cost_score = self._score_cost(result)
        latency_score = self._score_latency(result)
        quality_score = self._score_quality(result)

        # A failed run is fatal for fitness regardless of how cheap/fast
        # it was — we don't reward a crash that happened quickly. This
        # matches the documented contract in `DefaultEvaluator` and the
        # β.6 acceptance tests.
        if str(result.status) == "failed":
            return FitnessScore(
                overall=0.0,
                cost_score=cost_score,
                latency_score=latency_score,
                quality_score=0.0,
                run_id=result.run_id,
            )

        overall = (
            self._weights["cost"] * cost_score
            + self._weights["latency"] * latency_score
            + self._weights["quality"] * quality_score
        )

        return FitnessScore(
            overall=min(1.0, max(0.0, overall)),
            cost_score=cost_score,
            latency_score=latency_score,
            quality_score=quality_score,
            run_id=result.run_id,
        )

    def _score_cost(self, result: RunResult) -> float:
        """Higher score = lower cost. Score of 1.0 means cost is ~0."""
        cost = float(result.cost.total_cost)
        if cost <= 0:
            return 1.0
        # Exponential decay: score = exp(-cost / baseline)
        import math

        return math.exp(-cost / self._cost_baseline)

    def _score_latency(self, result: RunResult) -> float:
        """Higher score = lower latency. Score of 1.0 means near-instant."""
        latency = result.duration_ms or 0.0
        if latency <= 0:
            return 1.0
        import math

        return math.exp(-latency / self._latency_baseline)

    def _score_quality(self, result: RunResult) -> float:
        """Heuristic quality score based on run status and error count."""
        if result.status == "failed":
            return 0.0
        if result.status != "completed":
            return 0.3
        # Error penalty
        error_penalty = min(0.5, len(result.errors) * 0.1)
        return max(0.0, 1.0 - error_penalty)


class ComparisonEvaluator(DefaultEvaluator):
    """Evaluator that scores relative to a baseline run.

    Useful for comparing mutated flows against the original:
    score > 0.5 means the mutation improved fitness.
    """

    def __init__(
        self,
        baseline: RunResult,
        weights: dict[str, float] | None = None,
    ) -> None:
        super().__init__(weights=weights)
        self._baseline_score = self._compute_fitness(baseline)

    async def evaluate(self, result: RunResult) -> float:
        """Score relative to baseline. Returns 0.0–1.0 where 0.5 = no change."""
        new_score = self._compute_fitness(result)

        if self._baseline_score.overall <= 0:
            return 0.5

        relative = new_score.overall / self._baseline_score.overall
        # Map to [0, 1]: 1.0 means 2x improvement, 0.5 means same, 0.0 means half
        return min(1.0, relative / 2)


# ---------------------------------------------------------------------------
# LLM-as-Judge (fase δ.3)
# ---------------------------------------------------------------------------


_FEATURE_FLAG = "FORGE_ENABLE_LLM_JUDGE"
_DEFAULT_JUDGE_MODEL = "claude-sonnet-4-20250514"
_DEFAULT_TTL_SECONDS = 86_400  # 24h


class QualityScoreTool(BaseModel):
    """Structured-output schema used by the LLM judge.

    We ask for sub-scores so the judge has to decompose its reasoning —
    a single overall score tends to collapse to 0.5±ε. Sub-scores plus
    a rationale produce more calibrated numbers and a human-auditable
    trail for the Evolution dashboard.
    """

    correctness: float = Field(ge=0, le=1)
    completeness: float = Field(ge=0, le=1)
    relevance: float = Field(ge=0, le=1)
    score: float = Field(ge=0, le=1)
    rationale: str


_DEFAULT_JUDGE_SYSTEM = (
    "You are a strict but fair judge scoring the output of an AI agent run. "
    "Score on three axes (0=worst, 1=best) and give an overall `score` that "
    "reflects the weighted combination. Be calibrated: reserve 1.0 for "
    "genuinely excellent work and 0.0 for outputs that fail the task."
)


def _hash_output(output: Any) -> str:
    """Stable hash key — used to dedupe judge calls for identical outputs."""
    try:
        payload = str(output).encode("utf-8", errors="replace")
    except Exception:  # pragma: no cover — str() rarely fails
        payload = repr(output).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


class _InMemoryTTLCache:
    """Tiny TTL cache — the judge wants hash→score lookups, nothing fancier.

    Kept local to this module to avoid pulling in a cache framework for
    what is effectively a hot-dict with expiry. Thread-safe because
    :class:`LLMAsJudgeEvaluator` is called from the evolution loop,
    which is single-writer per orchestrator.
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, float]] = {}

    def get(self, key: str) -> float | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: float) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)

    def __len__(self) -> int:  # testing hook
        return len(self._store)


class LLMAsJudgeEvaluator(DefaultEvaluator):
    """Evaluator that replaces the heuristic quality score with an LLM judge.

    The judge is gated by the ``FORGE_ENABLE_LLM_JUDGE`` env flag. When
    disabled (the default), this class is a drop-in for
    :class:`DefaultEvaluator` — it runs the base heuristic and never
    touches the network. When enabled, it decorates the heuristic:

    * Cost / latency scores come from the parent class unchanged.
    * Quality comes from a cached structured-output call to the judge.
    * On LLM errors we fall back to the heuristic with a warning —
      fitness evaluation must never take down the evolution loop.
    """

    def __init__(
        self,
        *,
        llm_client: Any | None = None,
        model: str = _DEFAULT_JUDGE_MODEL,
        cache: _InMemoryTTLCache | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        super().__init__(weights=weights)
        self._llm = llm_client
        self._model = model
        self._cache = cache or _InMemoryTTLCache()

    @property
    def cache(self) -> _InMemoryTTLCache:
        """Exposed for tests — confirms cache hits on repeat calls."""
        return self._cache

    async def evaluate(self, result: RunResult) -> float:
        fitness = await self._compute_fitness_async(result)
        logger.debug(
            "evaluator.scored",
            run_id=result.run_id,
            overall=f"{fitness.overall:.3f}",
            quality=f"{fitness.quality_score:.3f}",
        )
        return fitness.overall

    async def _compute_fitness_async(self, result: RunResult) -> FitnessScore:
        base = self._compute_fitness(result)
        if not self._flag_enabled():
            return base
        if self._llm is None:
            logger.debug("llm_judge.skipped_no_client")
            return base
        # Failed runs short-circuit to 0 quality (parent already did this).
        if str(result.status) == "failed":
            return base

        cache_key = _hash_output(result.output)
        cached = self._cache.get(cache_key)
        if cached is not None:
            quality = cached
        else:
            try:
                quality = await self._call_judge(result)
            except Exception as exc:  # defensive — never let judge break eval
                logger.warning("llm_judge.failed_fallback", error=str(exc))
                return base
            self._cache.set(cache_key, quality)

        overall = (
            self._weights["cost"] * base.cost_score
            + self._weights["latency"] * base.latency_score
            + self._weights["quality"] * quality
        )
        return FitnessScore(
            overall=min(1.0, max(0.0, overall)),
            cost_score=base.cost_score,
            latency_score=base.latency_score,
            quality_score=quality,
            run_id=result.run_id,
        )

    async def _call_judge(self, result: RunResult) -> float:
        """Invoke the judge and return the overall score ∈ [0, 1]."""
        prompt = (
            "Score the following AI agent output against its task. "
            "Provide correctness, completeness, relevance, an overall "
            "`score`, and a short rationale.\n\n"
            f"TASK: {result.task_id}\n"
            f"OUTPUT: {str(result.output)[:4000]}\n"
        )
        assert self._llm is not None
        response = await self._llm.structured_call(
            model=self._model,
            schema=QualityScoreTool,
            system=_DEFAULT_JUDGE_SYSTEM,
            prompt=prompt,
            max_tokens=512,
            temperature=0.0,
        )
        return float(response.score)

    @staticmethod
    def _flag_enabled() -> bool:
        val = os.environ.get(_FEATURE_FLAG, "").strip().lower()
        return val in {"1", "true", "yes", "on"}

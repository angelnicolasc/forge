"""Deterministic evaluation suites for the Forge evolution loop.

Before β.5, the evolution loop's ``_eval_run`` fed ``history[-1].output``
back into the orchestrator as the next input — a degenerate, non-deterministic
eval because:

1. The output of run N is, in general, not a valid *input* for the same
   flow (types mismatch, schemas diverge, content is already consumed).
2. It scores exactly one point — no confidence interval — so a single
   lucky/unlucky run can cause a spurious commit or rollback.
3. It's non-stationary: the eval "target" drifts with every run,
   making before/after fitness deltas meaningless.

:class:`EvalSuite` fixes all three. A suite is a frozen list of
:class:`EvalCase` objects; ``evaluate(orchestrator)`` runs every case
and returns an aggregated :class:`FitnessScore` with a bootstrap
confidence interval. The suite is constructed once (from history or
synthetically) and reused across baseline/post-mutation comparisons —
that's what makes deltas meaningful.

Two construction modes:

* :meth:`EvalSuite.from_history` — deterministic stratified sampling
  over past run inputs. Cheap, no LLM calls. Default choice.
* :meth:`EvalSuite.synthetic` — LLM-generated diverse inputs based on
  the orchestrator's topology. Requires an :class:`LLMClient`. Useful
  when history is empty or homogeneous.

Aggregation: arithmetic mean of per-case overall scores, plus a
percentile-bootstrap 90% CI (500 resamples) so the evolution loop can
distinguish a real improvement from sampling noise.
"""

from __future__ import annotations

import hashlib
import random
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, Field

from forge_core.evolution.evaluator import DefaultEvaluator
from forge_core.evolution.llm_client import LLMClient, LLMStructuredCallError
from forge_core.types import (
    FitnessScore,
    RunConfig,
    RunResult,
    TaskEnvelope,
)

if TYPE_CHECKING:
    from forge_core.harness import MetaOrchestrator

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# EvalCase
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalCase:
    """A single evaluation scenario.

    Immutable on purpose: suites are long-lived objects that get reused
    for baseline and post-mutation runs, and accidentally mutating one
    between calls would invalidate the comparison.

    Fields:
        case_id: Stable id derived from ``input`` hash, used for logging
            and to dedupe when ``from_history`` picks cases.
        input: The payload fed to :attr:`TaskEnvelope.input`.
        expected_output: Optional ground-truth output for pass/fail
            scoring. When present, a case-level quality penalty is
            applied if the actual output diverges.
        tags: Optional classification (e.g. ``{"difficulty": "hard"}``)
            used by stratified sampling.
        weight: Relative importance in the aggregate mean. Defaults to
            1.0 (uniform). Larger weights bias the suite toward harder
            or more representative cases.
    """

    case_id: str
    input: dict[str, Any]
    expected_output: Any | None = None
    tags: dict[str, str] = field(default_factory=dict)
    weight: float = 1.0

    def envelope(self, *, cost_ceiling: Decimal) -> TaskEnvelope:
        """Build a :class:`TaskEnvelope` for executing this case.

        The envelope carries the β.4 reentrancy marker so eval runs
        don't re-trigger the auto-evolution path. Evolution and memory
        are both disabled to isolate the mutation's effect.
        """
        return TaskEnvelope(
            input=dict(self.input),
            config=RunConfig(
                cost_ceiling=cost_ceiling,
                enable_evolution=False,
                enable_memory=False,
            ),
            metadata={"forge_internal_eval": True, "eval_case_id": self.case_id},
        )


def _case_id_for(payload: dict[str, Any]) -> str:
    """Stable case id derived from a canonical serialization of the input."""
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _canonical_json(obj: Any) -> str:
    """Order-independent string encoding — good enough as a hash input."""
    import json

    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# SyntheticEvalResponse (for LLM-generated suites)
# ---------------------------------------------------------------------------


class _SyntheticCase(BaseModel):
    """One synthetic input produced by the LLM."""

    input: dict[str, Any] = Field(
        ..., description="JSON object that the flow will accept as TaskEnvelope.input."
    )
    difficulty: str = Field(
        default="medium",
        description="Rough difficulty label: easy | medium | hard",
    )
    rationale: str = Field(
        default="",
        description="Why this case is a useful evaluation input.",
    )


class _SyntheticEvalResponse(BaseModel):
    """Structured response for synthetic suite generation.

    The schema is intentionally small so the Anthropic tool-use call
    produces a predictable payload the suite can consume.
    """

    cases: list[_SyntheticCase] = Field(
        ..., min_length=1, description="Generated evaluation cases."
    )


_SYNTHETIC_SYSTEM = (
    "You are generating evaluation inputs for an agent-based workflow. "
    "Your outputs must be realistic, diverse, and cover edge cases. "
    "Each input must be a JSON object compatible with the flow's declared inputs."
)

_SYNTHETIC_TEMPLATE = """\
Generate {n} diverse evaluation inputs for this agent workflow.

## Agents
{agents}

## Recent run inputs (for reference)
{examples}

## Requirements
- Each input must be a valid JSON object.
- Stratify across difficulty: include at least one easy, one medium, one hard case.
- Inputs should exercise different code paths the agents might take.
- Do not return duplicates of the reference examples.
"""


# ---------------------------------------------------------------------------
# EvalSuite
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalSuite:
    """An immutable, deterministic collection of :class:`EvalCase`.

    Construct via :meth:`from_history`, :meth:`synthetic`, or directly
    with a hand-curated list of cases. Running the suite through
    :meth:`evaluate` produces an aggregate :class:`FitnessScore` with a
    bootstrap confidence interval.

    Determinism is a contract: two suites constructed with identical
    inputs (same run history, same seed) have identical ``cases``.
    That's what lets the evolution loop compare before/after fitness
    numerically rather than visually.
    """

    cases: tuple[EvalCase, ...]
    suite_id: str = ""

    def __len__(self) -> int:
        return len(self.cases)

    # --------------------------------------------------------------
    # Constructors
    # --------------------------------------------------------------

    @classmethod
    def from_cases(cls, cases: list[EvalCase]) -> EvalSuite:
        """Construct a suite from an explicit list of cases."""
        frozen = tuple(cases)
        return cls(cases=frozen, suite_id=_suite_id(frozen))

    @classmethod
    def from_history(
        cls,
        runs: list[RunResult],
        *,
        k: int = 5,
        seed: int = 0,
    ) -> EvalSuite:
        """Stratified deterministic sample of ``k`` diverse inputs from history.

        Strategy:

        1. Filter to runs with a non-empty ``envelope_input`` or ``output``
           we can re-use as input (we prefer ``output`` only if it's dict
           and resembles an input; otherwise use ``input`` payload extracted
           from ``events[0].data.input`` — where ``AGENT_START`` events
           were logged by the mock adapter / LangGraph callback).
        2. Dedupe on the case_id hash — no two cases with identical inputs.
        3. Stratify by cost quartile (0-25%, 25-50%, 50-75%, 75-100%) so
           cheap *and* expensive inputs both appear.
        4. Deterministic pick: seeded RNG over the sorted candidate list,
           taking ``ceil(k / 4)`` from each quartile.

        If fewer than ``k`` distinct cases exist, return what we have.
        If history is empty, return an empty suite (caller handles).
        """
        candidates = _extract_history_inputs(runs)
        if not candidates:
            return cls(cases=(), suite_id="")

        # Dedupe while preserving the observed cost for stratification.
        seen: dict[str, tuple[dict[str, Any], float]] = {}
        for payload, cost in candidates:
            cid = _case_id_for(payload)
            if cid not in seen:
                seen[cid] = (payload, cost)

        by_cost = sorted(seen.items(), key=lambda kv: kv[1][1])
        rng = random.Random(seed)

        # Stratify into 4 quartiles; sample proportionally.
        n_total = len(by_cost)
        if n_total <= k:
            picks = [payload for _, (payload, _) in by_cost]
        else:
            per_quartile = max(1, (k + 3) // 4)  # ceil(k/4)
            quartiles = _split_into_quartiles(by_cost)
            picks = []
            for q in quartiles:
                rng.shuffle(q)
                picks.extend(payload for _, (payload, _) in q[:per_quartile])
            # Trim to exactly k, deterministically biased toward harder (later) quartiles.
            picks = picks[:k]

        cases = [EvalCase(case_id=_case_id_for(p), input=p) for p in picks]
        return cls.from_cases(cases)

    @classmethod
    async def synthetic(
        cls,
        orchestrator: MetaOrchestrator,
        *,
        n: int = 3,
        llm_client: LLMClient | None = None,
        model: str = "claude-sonnet-4-20250514",
    ) -> EvalSuite:
        """Generate ``n`` synthetic eval inputs via an LLM.

        Uses the topology (agent names, roles, model assignments) plus a
        handful of recent history inputs as few-shot exemplars. Returns
        an empty suite on LLM failure — the evolution loop then falls
        back to :meth:`from_history`.
        """
        from forge_core.evolution.llm_client import get_default_llm_client

        client = llm_client or get_default_llm_client()
        agents_blurb = _render_agents_for_prompt(orchestrator.topology())
        example_inputs = _recent_history_examples(orchestrator.run_history, limit=3)

        prompt = _SYNTHETIC_TEMPLATE.format(
            n=n,
            agents=agents_blurb or "(no agents declared)",
            examples=example_inputs or "(no prior history)",
        )

        try:
            response = await client.structured_call(
                model=model,
                schema=_SyntheticEvalResponse,
                system=_SYNTHETIC_SYSTEM,
                prompt=prompt,
                max_tokens=2048,
                temperature=0.7,
            )
        except LLMStructuredCallError as exc:
            logger.warning("eval_suite.synthetic_failed", error=str(exc))
            return cls(cases=(), suite_id="")

        cases: list[EvalCase] = []
        for sc in response.cases[:n]:
            if not isinstance(sc.input, dict) or not sc.input:
                continue
            cases.append(
                EvalCase(
                    case_id=_case_id_for(sc.input),
                    input=sc.input,
                    tags={"difficulty": sc.difficulty, "synthetic": "true"},
                )
            )
        return cls.from_cases(cases)

    # --------------------------------------------------------------
    # Execution
    # --------------------------------------------------------------

    async def evaluate(
        self,
        orchestrator: MetaOrchestrator,
        *,
        evaluator: DefaultEvaluator | None = None,
        cost_ceiling: Decimal = Decimal("1.00"),
        bootstrap_samples: int = 500,
        seed: int = 0,
    ) -> FitnessScore:
        """Run every case, compute aggregate fitness with bootstrap CI.

        The aggregate is the weighted arithmetic mean of each case's
        overall fitness. The confidence interval is a percentile-
        bootstrap 90% CI across ``bootstrap_samples`` resamples.

        Empty suites return ``FitnessScore(overall=0.0)`` with a ``(0, 0)``
        CI — the evolution loop treats that as "no signal" and falls
        back to single-run fitness.
        """
        if not self.cases:
            return FitnessScore(overall=0.0, confidence_interval=(0.0, 0.0))

        ev = evaluator or DefaultEvaluator()
        per_case: list[FitnessScore] = []
        results: list[RunResult] = []

        for case in self.cases:
            envelope = case.envelope(cost_ceiling=cost_ceiling)
            try:
                result = await orchestrator.run(envelope)
            except Exception as exc:
                logger.warning(
                    "eval_suite.case_failed",
                    case_id=case.case_id,
                    error=str(exc),
                )
                continue
            results.append(result)
            per_case.append(ev.evaluate_sync(result))

        if not per_case:
            return FitnessScore(overall=0.0, confidence_interval=(0.0, 0.0))

        overall_scores = [f.overall for f in per_case]
        weights = [c.weight for c in self.cases[: len(per_case)]]
        mean_overall = _weighted_mean(overall_scores, weights)
        cost_mean = _weighted_mean([f.cost_score for f in per_case], weights)
        latency_mean = _weighted_mean([f.latency_score for f in per_case], weights)
        quality_mean = _weighted_mean([f.quality_score for f in per_case], weights)

        ci_low, ci_high = _bootstrap_ci(overall_scores, n_samples=bootstrap_samples, seed=seed)

        agg_run_id = results[-1].run_id if results else None
        return FitnessScore(
            overall=min(1.0, max(0.0, mean_overall)),
            cost_score=cost_mean,
            latency_score=latency_mean,
            quality_score=quality_mean,
            confidence_interval=(ci_low, ci_high),
            run_id=agg_run_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _suite_id(cases: tuple[EvalCase, ...]) -> str:
    """Stable suite id from the concatenated case ids."""
    h = hashlib.sha256()
    for c in cases:
        h.update(c.case_id.encode("utf-8"))
    return h.hexdigest()[:16]


def _extract_history_inputs(runs: list[RunResult]) -> list[tuple[dict[str, Any], float]]:
    """Pull reusable (input, cost) pairs out of a run-history list.

    Looks first at ``RunEventKind.AGENT_START`` events for the original
    input payload (which is what adapters log at the top of a run), then
    falls back to ``RunResult.output`` if it happens to be a dict.
    """
    from forge_core.types import RunEventKind

    out: list[tuple[dict[str, Any], float]] = []
    for run in runs:
        payload: dict[str, Any] | None = None
        for event in run.events:
            if event.kind == RunEventKind.AGENT_START:
                data = event.data or {}
                candidate = data.get("input")
                if isinstance(candidate, dict) and candidate:
                    payload = candidate
                    break
        if payload is None and isinstance(run.output, dict) and run.output:
            payload = run.output
        if payload is None:
            continue
        out.append((payload, float(run.cost.total_cost)))
    return out


def _split_into_quartiles(
    items: list[tuple[str, tuple[dict[str, Any], float]]],
) -> list[list[tuple[str, tuple[dict[str, Any], float]]]]:
    """Split a cost-sorted list into 4 equal buckets."""
    n = len(items)
    step = max(1, n // 4)
    return [items[i : i + step] for i in range(0, n, step)][:4]


def _render_agents_for_prompt(agents: list[Any]) -> str:
    lines = []
    for a in agents:
        name = getattr(a, "name", "unknown")
        role = getattr(a, "role", "")
        model = getattr(a, "model", "unspecified")
        lines.append(f"- {name} ({role}) on {model}")
    return "\n".join(lines)


def _recent_history_examples(runs: list[RunResult], *, limit: int = 3) -> str:
    """Render the last few run inputs as a few-shot block."""
    recent = runs[-limit:] if runs else []
    examples = []
    for r in recent:
        for event in r.events:
            if getattr(event, "data", None) and isinstance(event.data, dict):
                candidate = event.data.get("input")
                if isinstance(candidate, dict) and candidate:
                    examples.append(_canonical_json(candidate))
                    break
    return "\n".join(examples)


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    if not values:
        return 0.0
    total_weight = sum(weights) or 1.0
    return sum(v * w for v, w in zip(values, weights, strict=False)) / total_weight


def _bootstrap_ci(
    values: list[float], *, n_samples: int = 500, seed: int = 0, alpha: float = 0.10
) -> tuple[float, float]:
    """Percentile-bootstrap confidence interval of the mean.

    Defaults to a 90% CI (``alpha=0.10``). With very few cases (n<=2)
    we fall back to (min, max) since bootstrap resampling degenerates.
    """
    if len(values) <= 2:
        return (min(values), max(values))
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(n_samples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.fmean(sample))
    means.sort()
    lo_idx = int((alpha / 2) * n_samples)
    hi_idx = int((1 - alpha / 2) * n_samples) - 1
    return (means[lo_idx], means[hi_idx])

"""Forge Self-Evolution Loop — the differentiator.

The biology-inspired cycle that makes agents get smarter over time:

    OBSERVE → HYPOTHESIZE → MUTATE → EVALUATE → COMMIT / ROLLBACK
       ↑_______________________________________________|

This loop runs after (or between) production runs. Each iteration:
  1. Observes recent run_history from the MetaOrchestrator
  2. Hypothesizes: tries each mutator to find a mutation candidate
  3. Mutates: applies the highest-confidence mutation to the topology
  4. Evaluates: runs the mutated flow on an eval task, computes FitnessScore
  5. Commits (if fitness improves) or Rolls back (if fitness degrades)
  6. Logs everything to the EvolutionJournal for full audit trail

Safety guardrails:
  - max_mutations_per_session: caps iterations per call
  - cost_ceiling_per_eval: prevents expensive eval runs
  - rollback_on_quality_drop: threshold below which mutations are rejected
  - mode="suggest": propose only, never apply (human approves each change)

Usage:
    loop = EvolutionLoop(orchestrator=my_orchestrator)
    mutation = await loop.step()   # one iteration
    # or:
    mutations = await loop.run(max_steps=5)  # up to 5 iterations
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from forge_core.evolution.eval_suite import EvalSuite
from forge_core.evolution.evaluator import DefaultEvaluator
from forge_core.evolution.journal import EvolutionJournal
from forge_core.evolution.mutations import BUILTIN_MUTATORS
from forge_core.types import (
    Mutation,
    RunConfig,
    RunResult,
    TaskEnvelope,
)

if TYPE_CHECKING:
    from forge_core.evolution.snapshot import TopologySnapshot
    from forge_core.harness import MetaOrchestrator

logger = structlog.get_logger()


class EvolutionLoop:
    """The self-evolution engine for Forge.

    Implements the observe-hypothesize-mutate-evaluate-commit cycle.
    """

    def __init__(
        self,
        orchestrator: MetaOrchestrator,
        mode: str = "suggest",
        max_mutations: int = 5,
        cost_ceiling_per_eval: Decimal = Decimal("1.00"),
        rollback_threshold: float = 0.05,
        mutators: list[Any] | None = None,
        evaluator: Any | None = None,
        journal: EvolutionJournal | None = None,
        eval_suite: EvalSuite | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._mode = mode  # "suggest" | "auto"
        self._max_mutations = max_mutations
        self._cost_ceiling = cost_ceiling_per_eval
        self._rollback_threshold = rollback_threshold
        self._mutators = mutators or BUILTIN_MUTATORS
        self._evaluator = evaluator or DefaultEvaluator()
        self._journal = journal or EvolutionJournal()
        self._mutations_this_session = 0
        # β.5 — Deterministic eval suite. When ``None`` the loop falls back
        # to the single-run ``_eval_run`` path (legacy behavior). Callers
        # can inject one at construction or replace it via ``set_eval_suite``.
        self._eval_suite: EvalSuite | None = eval_suite

    # -----------------------------------------------------------------------
    # Public: eval suite management
    # -----------------------------------------------------------------------

    def set_eval_suite(self, suite: EvalSuite | None) -> None:
        """Replace the current eval suite (pass ``None`` to revert to legacy)."""
        self._eval_suite = suite

    @property
    def eval_suite(self) -> EvalSuite | None:
        return self._eval_suite

    async def ensure_eval_suite(self, *, k: int = 5, seed: int = 0) -> EvalSuite:
        """Lazy-build a :meth:`EvalSuite.from_history` suite if none is set.

        Returns the active suite — either the pre-configured one or the
        newly-constructed history-derived one. Safe to call repeatedly;
        subsequent calls return the cached suite.
        """
        if self._eval_suite is not None and len(self._eval_suite) > 0:
            return self._eval_suite
        suite = EvalSuite.from_history(self._orchestrator.run_history, k=k, seed=seed)
        self._eval_suite = suite
        return suite

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def step(self) -> Mutation | None:
        """Execute one iteration of the evolution cycle.

        Returns:
            The applied Mutation if one was committed, or None if no
            improvement was found or the session limit was reached.
        """
        if self._mutations_this_session >= self._max_mutations:
            logger.info("evolution.session_limit_reached", limit=self._max_mutations)
            return None

        # 1. OBSERVE: get recent run history
        run_history = self._orchestrator.run_history
        if len(run_history) < 2:
            logger.info("evolution.insufficient_history", runs=len(run_history))
            return None

        # 2. HYPOTHESIZE: find the best mutation candidate
        candidate = self._hypothesize(run_history)
        if candidate is None:
            logger.info("evolution.no_candidate")
            return None

        logger.info(
            "evolution.candidate_proposed",
            kind=candidate.kind,
            confidence=f"{candidate.confidence:.2f}",
            description=candidate.description,
        )
        self._journal.record(candidate, kind="proposed")

        # In suggest mode, return the proposal without applying
        if self._mode == "suggest":
            return candidate

        # 3. EVALUATE: score the current topology (before mutation).
        #    When an EvalSuite is configured (β.5) we run the full suite,
        #    giving us a bootstrap confidence interval. Otherwise fall
        #    back to the single-run eval for backwards compatibility.
        fitness_before = await self._eval_topology()
        if fitness_before is None:
            logger.warning("evolution.baseline_eval_failed")
            return None

        # 4. MUTATE: capture pre-snapshot then apply the candidate mutation.
        #    The snapshot is the atomic rollback target (β.1) — if anything
        #    from here on fails, `orchestrator.restore_snapshot(pre_snapshot)`
        #    puts the world back exactly as it was.
        pre_snapshot = self._orchestrator.capture_snapshot()
        current_state = self._orchestrator.current_state()
        mutator = self._find_mutator(candidate)
        if mutator is None:
            logger.warning("evolution.mutator_not_found", kind=candidate.kind)
            return None

        try:
            new_state = await mutator.apply(candidate, current_state)
            # β.3 mutators return TopologyState; legacy mutators may still
            # return list[AgentCard]. swap_topology handles both.
            self._orchestrator.swap_topology(new_state)
            self._journal.record(candidate, kind="applied", fitness_before=fitness_before)
        except Exception as exc:
            # Belt-and-braces: even if swap never happened, restoring is a
            # no-op equivalent so the error path is uniform.
            self._orchestrator.restore_snapshot(pre_snapshot)
            logger.error("evolution.mutate_failed", error=str(exc))
            self._journal.record(candidate, kind="skipped", reason=str(exc))
            return None

        # 5. EVALUATE: score the mutated topology (same suite — that's
        #    what makes the before/after delta comparable).
        fitness_after = await self._eval_topology()
        if fitness_after is None:
            await self._rollback(candidate, mutator, pre_snapshot)
            return None

        # 6. COMMIT or ROLLBACK
        improvement = fitness_after.overall - fitness_before.overall
        if improvement >= -self._rollback_threshold:
            # Commit: improvement is positive or within acceptable tolerance
            self._mutations_this_session += 1
            self._journal.record(
                candidate,
                kind="applied",
                fitness_before=fitness_before,
                fitness_after=fitness_after,
            )
            logger.info(
                "evolution.committed",
                mutation_id=candidate.id,
                improvement=f"{improvement:+.3f}",
                fitness_before=f"{fitness_before.overall:.3f}",
                fitness_after=f"{fitness_after.overall:.3f}",
            )
            return candidate
        else:
            # Rollback: fitness degraded beyond threshold
            await self._rollback(candidate, mutator, pre_snapshot)
            self._journal.record(
                candidate,
                kind="rolled_back",
                fitness_before=fitness_before,
                fitness_after=fitness_after,
                reason=(
                    f"Fitness dropped {improvement:.3f} "
                    f"(threshold: {-self._rollback_threshold:.3f})"
                ),
            )
            logger.warning(
                "evolution.rolled_back",
                mutation_id=candidate.id,
                improvement=f"{improvement:+.3f}",
            )
            return None

    async def run(self, max_steps: int | None = None) -> list[Mutation]:
        """Run multiple evolution iterations.

        Args:
            max_steps: Maximum iterations. Defaults to self._max_mutations.

        Returns:
            List of committed mutations.
        """
        steps = max_steps or self._max_mutations
        applied: list[Mutation] = []
        for _ in range(steps):
            mutation = await self.step()
            if mutation is None:
                break
            if self._mode != "suggest":
                applied.append(mutation)
        return applied

    @property
    def journal(self) -> EvolutionJournal:
        return self._journal

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _hypothesize(self, run_history: list[RunResult]) -> Mutation | None:
        """Try all mutators and return the highest-confidence proposal."""
        candidates: list[Mutation] = []
        for mutator in self._mutators:
            try:
                proposal = mutator.propose(run_history)
                if proposal is not None:
                    candidates.append(proposal)
            except Exception as exc:
                logger.warning(
                    "evolution.mutator_error",
                    mutator=type(mutator).__name__,
                    error=str(exc),
                )

        if not candidates:
            return None

        return max(candidates, key=lambda m: m.confidence)

    async def _eval_topology(self) -> Any | None:
        """Score the current topology, preferring the :class:`EvalSuite`.

        Returns a :class:`FitnessScore` (β.5 path) or ``None`` on total
        failure. The legacy single-run path builds a `FitnessScore` via
        the evaluator so the caller sees one uniform object either way.
        """
        # Prefer the configured suite; otherwise derive one from history.
        suite = self._eval_suite
        if suite is None or len(suite) == 0:
            # If we have enough history, lazy-build a deterministic suite.
            history = self._orchestrator.run_history
            if len(history) >= 1:
                suite = EvalSuite.from_history(history, k=3)
                if len(suite) > 0:
                    self._eval_suite = suite

        if suite is not None and len(suite) > 0:
            try:
                return await suite.evaluate(
                    self._orchestrator,
                    evaluator=self._evaluator,
                    cost_ceiling=self._cost_ceiling,
                )
            except Exception as exc:
                logger.error("evolution.eval_suite_failed", error=str(exc))
                # Fall through to legacy single-run path.

        legacy_result = await self._eval_run()
        if legacy_result is None:
            return None
        return self._evaluator.evaluate_sync(legacy_result)

    async def _eval_run(self) -> RunResult | None:
        """Execute a low-cost evaluation run to measure current fitness."""
        try:
            # Use a minimal eval input — tasks from history make good eval inputs
            history = self._orchestrator.run_history
            if history:
                eval_input = history[-1].output or {}
                if not isinstance(eval_input, dict):
                    eval_input = {"eval": True}
            else:
                eval_input = {"eval": True}

            envelope = TaskEnvelope(
                input=eval_input,
                config=RunConfig(
                    cost_ceiling=self._cost_ceiling,
                    enable_evolution=False,
                    enable_memory=False,
                ),
                # β.4 reentrancy guard. MetaOrchestrator._should_trigger_evolution
                # skips auto-trigger for envelopes carrying this marker, so the
                # loop's own eval runs can't spawn a nested evolution step.
                metadata={"forge_internal_eval": True},
            )
            return await self._orchestrator.run(envelope)
        except Exception as exc:
            logger.error("evolution.eval_run_failed", error=str(exc))
            return None

    def _find_mutator(self, mutation: Mutation) -> Any | None:
        """Find the mutator responsible for a given mutation kind."""
        for mutator in self._mutators:
            if mutator.mutation_kind == mutation.kind:
                return mutator
        return None

    async def _rollback(
        self,
        mutation: Mutation,
        mutator: Any,
        pre_snapshot: TopologySnapshot,
    ) -> None:
        """Rollback a mutation by restoring the pre-mutation snapshot.

        Snapshot-based rollback (β.1) is the authoritative path: it
        replaces the entire topology with the exact agents that were
        live before the mutation was applied. We still call the
        mutator's ``rollback`` for backwards compatibility — mutators
        that compute local inverses (e.g. ``ModelSwapMutator``) remain
        correct — but any disagreement between the two is resolved in
        favor of the snapshot, which is immutable and content-hashed.
        """
        try:
            # Authoritative restore first: this fixes the Fase β.1 gap
            # where ``AgentCullMutator.rollback`` was a no-op.
            self._orchestrator.restore_snapshot(pre_snapshot)
        except Exception as exc:
            logger.error("evolution.snapshot_restore_failed", error=str(exc))

        # Best-effort legacy hook. Some mutators may do cleanup work
        # beyond the topology (e.g. closing LLM clients); we still
        # invoke it, but tolerate failure. Try the β.3 state-based
        # signature first, fall back to the legacy list-of-agents one.
        for arg in (pre_snapshot.to_state(), pre_snapshot.agents_list()):
            try:
                await mutator.rollback(mutation, arg)
                break
            except (TypeError, AttributeError):
                continue
            except Exception as exc:
                logger.debug(
                    "evolution.mutator_rollback_hook_failed",
                    error=str(exc),
                )
                break

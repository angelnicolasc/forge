"""forge_review.runner — ReviewRunner facade."""

from __future__ import annotations

import time
from typing import Any

import structlog

from forge_core.types import RunEvent, RunEventKind

from forge_review.agents import (
    CostCeilingReviewer,
    ErrorRateReviewer,
    ReviewAgent,
    run_parallel,
)
from forge_review.policy import PolicyGate
from forge_review.types import (
    HookKind,
    PolicyDecision,
    ReviewConfig,
    ReviewContext,
    ReviewResult,
)

log = structlog.get_logger(__name__)


class ReviewRunner:
    """Facade that runs reviewers, evaluates the policy gate, and emits events.

    Usage::

        runner = ReviewRunner()
        result, decision = await runner.pre_merge(run_result, run_id="run-abc")
        if not decision.allowed:
            raise RuntimeError(decision.reason)
    """

    def __init__(
        self,
        reviewers: list[Any] | None = None,
        *,
        config: ReviewConfig | None = None,
        bus: Any = None,
    ) -> None:
        self._reviewers: list[Any] = (
            reviewers
            if reviewers is not None
            else [ErrorRateReviewer(), CostCeilingReviewer()]
        )
        self._config = config or ReviewConfig()
        self._gate = PolicyGate(self._config)
        self._bus = bus

    # ------------------------------------------------------------------
    # Typed hook helpers
    # ------------------------------------------------------------------

    async def on_plan(
        self,
        plan: dict[str, Any],
        *,
        run_id: str = "",
    ) -> tuple[ReviewResult, PolicyDecision]:
        """Review a plan before execution begins."""
        return await self._run(HookKind.ON_PLAN, run_id=run_id, plan=plan)

    async def pre_apply(
        self,
        mutation_kind: Any,
        *,
        run_id: str = "",
        spec: Any = None,
    ) -> tuple[ReviewResult, PolicyDecision]:
        """Gate an EvolutionLoop mutation before it is applied."""
        return await self._run(
            HookKind.PRE_APPLY,
            run_id=run_id,
            mutation_kind=mutation_kind,
            spec=spec,
        )

    async def pre_stop(
        self,
        run_result: Any,
        *,
        run_id: str = "",
    ) -> tuple[ReviewResult, PolicyDecision]:
        """Review a partial RunResult before the run is cancelled."""
        return await self._run(HookKind.PRE_STOP, run_id=run_id, run_result=run_result)

    async def pre_merge(
        self,
        run_result: Any,
        *,
        run_id: str = "",
    ) -> tuple[ReviewResult, PolicyDecision]:
        """Gate a completed RunResult before its output is accepted."""
        return await self._run(HookKind.PRE_MERGE, run_id=run_id, run_result=run_result)

    # ------------------------------------------------------------------
    # Generic entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        hook: HookKind,
        *,
        run_id: str = "",
        **data: Any,
    ) -> tuple[ReviewResult, PolicyDecision]:
        """Run all reviewers for any hook kind with arbitrary context data."""
        return await self._run(hook, run_id=run_id, **data)

    def gate(self, review_result: ReviewResult) -> PolicyDecision:
        """Evaluate an existing ReviewResult without re-running reviewers."""
        return self._gate.evaluate(review_result)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(
        self,
        hook: HookKind,
        *,
        run_id: str = "",
        **data: Any,
    ) -> tuple[ReviewResult, PolicyDecision]:
        start = time.monotonic()
        context = ReviewContext(hook=hook, run_id=run_id, data=data)
        findings = await run_parallel(self._reviewers, context)
        duration_ms = (time.monotonic() - start) * 1000

        review_result = ReviewResult(
            hook=hook,
            run_id=run_id,
            findings=findings,
            duration_ms=duration_ms,
            reviewers_run=[getattr(r, "name", repr(r)) for r in self._reviewers],
        )
        decision = self._gate.evaluate(review_result)

        log.debug(
            "review_complete",
            hook=hook,
            allowed=decision.allowed,
            findings=len(findings),
            duration_ms=round(duration_ms, 1),
        )

        if self._bus is not None:
            await self._bus.publish(
                RunEvent(
                    kind=RunEventKind.REVIEW_COMPLETE,
                    run_id=run_id or None,
                    data={
                        "hook": hook,
                        "allowed": decision.allowed,
                        "finding_count": len(findings),
                        "blocking_count": len(review_result.blocking_findings),
                    },
                )
            )

        return review_result, decision

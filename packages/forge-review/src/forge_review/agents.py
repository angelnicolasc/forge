"""forge_review.agents — ReviewAgent protocol, parallel runner, and built-in reviewers."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import structlog

from forge_review.types import Finding, FindingSeverity, ReviewContext

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ReviewAgent(Protocol):
    """Structural protocol for all review agents.

    Satisfying the protocol requires:
    - a ``name`` class/instance attribute
    - an async ``review(context)`` method returning a list of findings
    """

    @property
    def name(self) -> str: ...

    async def review(self, context: ReviewContext) -> list[Finding]: ...


# ---------------------------------------------------------------------------
# Parallel runner
# ---------------------------------------------------------------------------


async def run_parallel(
    agents: list[Any],
    context: ReviewContext,
) -> list[Finding]:
    """Run all agents concurrently; wrap exceptions as P2 findings so one bad
    reviewer never silences the rest."""
    if not agents:
        return []

    results = await asyncio.gather(
        *[a.review(context) for a in agents],
        return_exceptions=True,
    )
    findings: list[Finding] = []
    for agent, result in zip(agents, results, strict=False):
        if isinstance(result, BaseException):
            agent_name = getattr(agent, "name", repr(agent))
            log.warning("reviewer_exception", reviewer=agent_name, error=str(result))
            findings.append(
                Finding(
                    severity=FindingSeverity.P2,
                    title=f"Reviewer '{agent_name}' raised an exception",
                    message=str(result),
                    source=agent_name,
                    tags=["reviewer-error"],
                )
            )
        else:
            findings.extend(result)
    return findings


# ---------------------------------------------------------------------------
# Built-in heuristic reviewers
# ---------------------------------------------------------------------------


class ErrorRateReviewer:
    """P1 finding when run_result carries any errors."""

    name = "error_rate"

    async def review(self, context: ReviewContext) -> list[Finding]:
        run_result = context.data.get("run_result")
        if run_result is None:
            return []
        errors: list[str] = getattr(run_result, "errors", [])
        if not errors:
            return []
        sample = errors[0][:120] if errors else ""
        return [
            Finding(
                severity=FindingSeverity.P1,
                title="Run completed with errors",
                message=f"{len(errors)} error(s) — first: {sample}",
                source=self.name,
                tags=["run-errors"],
            )
        ]


class CostCeilingReviewer:
    """P1 finding when run_result.cost.total_cost meets or exceeds the ceiling."""

    name = "cost_ceiling"

    def __init__(self, ceiling: Decimal = Decimal("10.00")) -> None:
        self._ceiling = ceiling

    async def review(self, context: ReviewContext) -> list[Finding]:
        run_result = context.data.get("run_result")
        if run_result is None:
            return []
        cost = getattr(getattr(run_result, "cost", None), "total_cost", None)
        if cost is None:
            return []
        if cost >= self._ceiling:
            return [
                Finding(
                    severity=FindingSeverity.P1,
                    title="Cost ceiling reached",
                    message=f"Run cost {cost} >= ceiling {self._ceiling}",
                    source=self.name,
                    tags=["cost"],
                )
            ]
        return []


class SpecConstraintReviewer:
    """P0 finding when a mutation_kind violates spec constraints.

    Requires forge-spec to be installed (``pip install forge-review[spec]``).
    Silently no-ops when forge_spec is not available or spec/mutation_kind
    is absent from the context.
    """

    name = "spec_constraint"

    async def review(self, context: ReviewContext) -> list[Finding]:
        spec = context.data.get("spec")
        mutation_kind = context.data.get("mutation_kind")
        if spec is None or mutation_kind is None:
            return []
        try:
            from forge_spec.constraints import SpecConstraintGuard
        except ImportError:
            log.warning(
                "forge_spec_not_installed",
                message="Install forge-review[spec] to enable spec constraint checking.",
            )
            return []
        guard = SpecConstraintGuard(spec)
        violations = guard.violations_for(mutation_kind)
        if not violations:
            return []
        constraint_ids = ", ".join(v.id for v in violations)
        return [
            Finding(
                severity=FindingSeverity.P0,
                title="Mutation violates spec constraint",
                message=f"Blocked by constraint(s): {constraint_ids}",
                source=self.name,
                location=f"mutation_kind:{mutation_kind}",
                tags=["spec-constraint"],
            )
        ]


class StatusReviewer:
    """P1 finding when run_result.status is not COMPLETED."""

    name = "status"

    async def review(self, context: ReviewContext) -> list[Finding]:
        run_result = context.data.get("run_result")
        if run_result is None:
            return []
        status = str(getattr(run_result, "status", ""))
        if status == "completed":
            return []
        return [
            Finding(
                severity=FindingSeverity.P1,
                title=f"Run status is '{status}', expected 'completed'",
                message=f"Run did not complete successfully (status={status!r})",
                source=self.name,
                tags=["status"],
            )
        ]

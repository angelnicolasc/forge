"""Tests for forge_review.agents — ReviewAgent protocol, run_parallel, built-in reviewers."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from forge_core.types import CostSummary, RunResult, RunStatus
from forge_review.agents import (
    CostCeilingReviewer,
    ErrorRateReviewer,
    ReviewAgent,
    SpecConstraintReviewer,
    StatusReviewer,
    run_parallel,
)
from forge_review.types import FindingSeverity, HookKind, ReviewContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(**data) -> ReviewContext:
    return ReviewContext(hook=HookKind.PRE_MERGE, data=data)


def _run(
    status: RunStatus = RunStatus.COMPLETED,
    errors: list[str] | None = None,
    total_cost: Decimal = Decimal("0"),
) -> RunResult:
    cost = CostSummary(total_cost=total_cost)
    return RunResult(
        task_id="t1",
        status=status,
        errors=errors or [],
        cost=cost,
    )


# ---------------------------------------------------------------------------
# ReviewAgent Protocol
# ---------------------------------------------------------------------------


class TestReviewAgentProtocol:
    def test_error_rate_satisfies_protocol(self):
        assert isinstance(ErrorRateReviewer(), ReviewAgent)

    def test_cost_ceiling_satisfies_protocol(self):
        assert isinstance(CostCeilingReviewer(), ReviewAgent)

    def test_spec_constraint_satisfies_protocol(self):
        assert isinstance(SpecConstraintReviewer(), ReviewAgent)

    def test_status_satisfies_protocol(self):
        assert isinstance(StatusReviewer(), ReviewAgent)

    def test_custom_agent_satisfies_protocol(self):
        class MyAgent:
            @property
            def name(self) -> str:
                return "my_agent"

            async def review(self, context):
                return []

        assert isinstance(MyAgent(), ReviewAgent)


# ---------------------------------------------------------------------------
# run_parallel
# ---------------------------------------------------------------------------


class TestRunParallel:
    def test_empty_agents(self):
        ctx = _ctx()
        findings = asyncio.run(run_parallel([], ctx))
        assert findings == []

    def test_single_agent_no_findings(self):
        ctx = _ctx(run_result=_run())

        class NoOpAgent:
            name = "noop"

            async def review(self, context):
                return []

        findings = asyncio.run(run_parallel([NoOpAgent()], ctx))
        assert findings == []

    def test_multiple_agents_combined(self):
        run = _run(errors=["boom"], total_cost=Decimal("15.00"))
        ctx = _ctx(run_result=run)
        findings = asyncio.run(run_parallel([ErrorRateReviewer(), CostCeilingReviewer()], ctx))
        assert len(findings) == 2

    def test_exception_in_agent_becomes_p2_finding(self):
        class BrokenAgent:
            name = "broken"

            async def review(self, context):
                raise RuntimeError("internal error")

        ctx = _ctx()
        findings = asyncio.run(run_parallel([BrokenAgent()], ctx))
        assert len(findings) == 1
        assert findings[0].severity == FindingSeverity.P2
        assert "broken" in findings[0].title
        assert "reviewer-error" in findings[0].tags

    def test_exception_does_not_block_other_agents(self):
        class BrokenAgent:
            name = "broken"

            async def review(self, context):
                raise ValueError("bad")

        run = _run(errors=["oops"])
        ctx = _ctx(run_result=run)
        findings = asyncio.run(run_parallel([BrokenAgent(), ErrorRateReviewer()], ctx))
        assert len(findings) == 2
        severities = {f.severity for f in findings}
        assert FindingSeverity.P1 in severities  # from ErrorRateReviewer
        assert FindingSeverity.P2 in severities  # from BrokenAgent exception


# ---------------------------------------------------------------------------
# ErrorRateReviewer
# ---------------------------------------------------------------------------


class TestErrorRateReviewer:
    def test_no_errors_no_finding(self):
        ctx = _ctx(run_result=_run())
        findings = asyncio.run(ErrorRateReviewer().review(ctx))
        assert findings == []

    def test_errors_produce_p1(self):
        ctx = _ctx(run_result=_run(errors=["something broke"]))
        findings = asyncio.run(ErrorRateReviewer().review(ctx))
        assert len(findings) == 1
        assert findings[0].severity == FindingSeverity.P1
        assert findings[0].source == "error_rate"

    def test_multiple_errors_one_finding(self):
        ctx = _ctx(run_result=_run(errors=["e1", "e2", "e3"]))
        findings = asyncio.run(ErrorRateReviewer().review(ctx))
        assert len(findings) == 1
        assert "3" in findings[0].message

    def test_no_run_result_returns_empty(self):
        ctx = _ctx()
        findings = asyncio.run(ErrorRateReviewer().review(ctx))
        assert findings == []

    def test_name(self):
        assert ErrorRateReviewer.name == "error_rate"


# ---------------------------------------------------------------------------
# CostCeilingReviewer
# ---------------------------------------------------------------------------


class TestCostCeilingReviewer:
    def test_under_ceiling_no_finding(self):
        ctx = _ctx(run_result=_run(total_cost=Decimal("5.00")))
        findings = asyncio.run(CostCeilingReviewer(ceiling=Decimal("10.00")).review(ctx))
        assert findings == []

    def test_at_ceiling_produces_p1(self):
        ctx = _ctx(run_result=_run(total_cost=Decimal("10.00")))
        findings = asyncio.run(CostCeilingReviewer(ceiling=Decimal("10.00")).review(ctx))
        assert len(findings) == 1
        assert findings[0].severity == FindingSeverity.P1
        assert findings[0].source == "cost_ceiling"

    def test_over_ceiling_produces_p1(self):
        ctx = _ctx(run_result=_run(total_cost=Decimal("99.99")))
        findings = asyncio.run(CostCeilingReviewer(ceiling=Decimal("10.00")).review(ctx))
        assert len(findings) == 1

    def test_default_ceiling_is_ten(self):
        ctx = _ctx(run_result=_run(total_cost=Decimal("10.00")))
        findings = asyncio.run(CostCeilingReviewer().review(ctx))
        assert len(findings) == 1

    def test_no_run_result_returns_empty(self):
        ctx = _ctx()
        findings = asyncio.run(CostCeilingReviewer().review(ctx))
        assert findings == []

    def test_name(self):
        assert CostCeilingReviewer.name == "cost_ceiling"


# ---------------------------------------------------------------------------
# SpecConstraintReviewer
# ---------------------------------------------------------------------------


class TestSpecConstraintReviewer:
    def test_no_spec_no_finding(self):
        ctx = _ctx()
        findings = asyncio.run(SpecConstraintReviewer().review(ctx))
        assert findings == []

    def test_no_mutation_kind_no_finding(self):
        ctx = _ctx(spec=object())
        findings = asyncio.run(SpecConstraintReviewer().review(ctx))
        assert findings == []

    def test_with_spec_and_kind_blocked(self):
        from forge_core.types import MutationKind
        from forge_spec.types import SpecConstraint, SpecDef

        spec = SpecDef(
            name="s",
            constraints=[
                SpecConstraint(
                    id="c1",
                    description="no model swap",
                    mutation_kinds_blocked=[MutationKind.MODEL_SWAP],
                )
            ],
        )
        ctx = _ctx(spec=spec, mutation_kind=MutationKind.MODEL_SWAP)
        findings = asyncio.run(SpecConstraintReviewer().review(ctx))
        assert len(findings) == 1
        assert findings[0].severity == FindingSeverity.P0
        assert "c1" in findings[0].message
        assert "spec-constraint" in findings[0].tags

    def test_with_spec_and_kind_allowed(self):
        from forge_core.types import MutationKind
        from forge_spec.types import SpecConstraint, SpecDef

        spec = SpecDef(
            name="s",
            constraints=[
                SpecConstraint(
                    id="c1",
                    description="no model swap",
                    mutation_kinds_blocked=[MutationKind.MODEL_SWAP],
                )
            ],
        )
        ctx = _ctx(spec=spec, mutation_kind=MutationKind.PROMPT_REWRITE)
        findings = asyncio.run(SpecConstraintReviewer().review(ctx))
        assert findings == []

    def test_name(self):
        assert SpecConstraintReviewer.name == "spec_constraint"


# ---------------------------------------------------------------------------
# StatusReviewer
# ---------------------------------------------------------------------------


class TestStatusReviewer:
    def test_completed_no_finding(self):
        ctx = _ctx(run_result=_run(status=RunStatus.COMPLETED))
        findings = asyncio.run(StatusReviewer().review(ctx))
        assert findings == []

    def test_failed_produces_p1(self):
        ctx = _ctx(run_result=_run(status=RunStatus.FAILED))
        findings = asyncio.run(StatusReviewer().review(ctx))
        assert len(findings) == 1
        assert findings[0].severity == FindingSeverity.P1
        assert findings[0].source == "status"

    def test_cancelled_produces_p1(self):
        ctx = _ctx(run_result=_run(status=RunStatus.CANCELLED))
        findings = asyncio.run(StatusReviewer().review(ctx))
        assert len(findings) == 1

    def test_no_run_result_returns_empty(self):
        ctx = _ctx()
        findings = asyncio.run(StatusReviewer().review(ctx))
        assert findings == []

    def test_name(self):
        assert StatusReviewer.name == "status"

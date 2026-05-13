"""Tests for forge_review.runner — ReviewRunner facade."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from forge_core.types import CostSummary, RunEventKind, RunResult, RunStatus
from forge_review.runner import ReviewRunner
from forge_review.types import Finding, FindingSeverity, HookKind, ReviewConfig, ReviewContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    status: RunStatus = RunStatus.COMPLETED,
    errors: list[str] | None = None,
    total_cost: Decimal = Decimal("0"),
) -> RunResult:
    cost = CostSummary(total_cost=total_cost)
    return RunResult(task_id="t1", status=status, errors=errors or [], cost=cost)


class _FixedReviewer:
    def __init__(self, name: str, findings: list[Finding]) -> None:
        self.name = name
        self._findings = findings

    async def review(self, context: ReviewContext) -> list[Finding]:
        return list(self._findings)


class _FakeBus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Default reviewers
# ---------------------------------------------------------------------------


class TestReviewRunnerDefaults:
    def test_default_reviewers_are_error_and_cost(self):
        runner = ReviewRunner()
        names = {getattr(r, "name", "") for r in runner._reviewers}
        assert "error_rate" in names
        assert "cost_ceiling" in names

    def test_clean_run_allowed(self):
        runner = ReviewRunner()
        result, decision = asyncio.run(runner.pre_merge(_run()))
        assert decision.allowed is True
        assert result.hook == HookKind.PRE_MERGE

    def test_errored_run_blocked(self):
        runner = ReviewRunner()
        result, decision = asyncio.run(runner.pre_merge(_run(errors=["boom"])))
        assert decision.allowed is False
        assert result.has_blocking

    def test_over_cost_run_blocked(self):
        runner = ReviewRunner()
        _result, decision = asyncio.run(runner.pre_merge(_run(total_cost=Decimal("15.00"))))
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# Hook helpers
# ---------------------------------------------------------------------------


class TestHookHelpers:
    def test_on_plan_returns_result_and_decision(self):
        runner = ReviewRunner(reviewers=[_FixedReviewer("noop", [])])
        result, decision = asyncio.run(runner.on_plan({"step": "analyse"}))
        assert result.hook == HookKind.ON_PLAN
        assert decision.allowed is True

    def test_pre_apply_returns_result_and_decision(self):
        runner = ReviewRunner(reviewers=[_FixedReviewer("noop", [])])
        result, _decision = asyncio.run(runner.pre_apply("model_swap"))
        assert result.hook == HookKind.PRE_APPLY

    def test_pre_stop_uses_run_result(self):
        runner = ReviewRunner()
        result, _decision = asyncio.run(runner.pre_stop(_run()))
        assert result.hook == HookKind.PRE_STOP

    def test_pre_merge_uses_run_result(self):
        runner = ReviewRunner()
        result, _decision = asyncio.run(runner.pre_merge(_run()))
        assert result.hook == HookKind.PRE_MERGE

    def test_run_id_propagated(self):
        runner = ReviewRunner(reviewers=[_FixedReviewer("noop", [])])
        result, _ = asyncio.run(runner.pre_merge(_run(), run_id="my-run"))
        assert result.run_id == "my-run"


# ---------------------------------------------------------------------------
# Custom reviewers
# ---------------------------------------------------------------------------


class TestCustomReviewers:
    def test_custom_reviewers_replace_defaults(self):
        runner = ReviewRunner(reviewers=[_FixedReviewer("custom", [])])
        assert len(runner._reviewers) == 1
        assert runner._reviewers[0].name == "custom"

    def test_reviewers_run_in_result(self):
        runner = ReviewRunner(
            reviewers=[
                _FixedReviewer("r1", []),
                _FixedReviewer("r2", []),
            ]
        )
        result, _ = asyncio.run(runner.pre_merge(_run()))
        assert "r1" in result.reviewers_run
        assert "r2" in result.reviewers_run

    def test_blocking_finding_produces_denied_decision(self):
        runner = ReviewRunner(
            reviewers=[
                _FixedReviewer(
                    "p0_finder",
                    [Finding(severity=FindingSeverity.P0, title="Critical", message="stop")],
                )
            ]
        )
        _, decision = asyncio.run(runner.pre_merge(_run()))
        assert decision.allowed is False
        assert "Critical" in decision.reason


# ---------------------------------------------------------------------------
# gate()
# ---------------------------------------------------------------------------


class TestGateMethod:
    def test_gate_evaluates_existing_result(self):
        from forge_review.types import ReviewResult

        result = ReviewResult(
            hook=HookKind.PRE_MERGE,
            findings=[Finding(severity=FindingSeverity.P0, title="t", message="m")],
        )
        decision = ReviewRunner().gate(result)
        assert decision.allowed is False

    def test_gate_allows_clean_result(self):
        from forge_review.types import ReviewResult

        result = ReviewResult(hook=HookKind.PRE_MERGE)
        decision = ReviewRunner().gate(result)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# EventBus integration
# ---------------------------------------------------------------------------


class TestEventBusIntegration:
    def test_review_complete_event_emitted(self):
        bus = _FakeBus()
        runner = ReviewRunner(reviewers=[_FixedReviewer("noop", [])], bus=bus)
        asyncio.run(runner.pre_merge(_run()))
        assert len(bus.events) == 1
        assert bus.events[0].kind == RunEventKind.REVIEW_COMPLETE

    def test_event_contains_hook_and_decision(self):
        bus = _FakeBus()
        runner = ReviewRunner(reviewers=[_FixedReviewer("noop", [])], bus=bus)
        asyncio.run(runner.pre_merge(_run(), run_id="r1"))
        event = bus.events[0]
        assert event.data["hook"] == HookKind.PRE_MERGE
        assert event.data["allowed"] is True
        assert event.run_id == "r1"

    def test_no_event_without_bus(self):
        runner = ReviewRunner(reviewers=[_FixedReviewer("noop", [])])
        result, _ = asyncio.run(runner.pre_merge(_run()))
        assert result is not None  # just confirming no crash

    def test_blocking_decision_reflected_in_event(self):
        bus = _FakeBus()
        runner = ReviewRunner(
            reviewers=[
                _FixedReviewer(
                    "p0",
                    [Finding(severity=FindingSeverity.P0, title="t", message="m")],
                )
            ],
            bus=bus,
        )
        asyncio.run(runner.pre_merge(_run()))
        assert bus.events[0].data["allowed"] is False
        assert bus.events[0].data["blocking_count"] == 1


# ---------------------------------------------------------------------------
# Custom config
# ---------------------------------------------------------------------------


class TestCustomConfig:
    def test_custom_block_on_p0_only(self):
        config = ReviewConfig(block_on=[FindingSeverity.P0])
        runner = ReviewRunner(
            reviewers=[_FixedReviewer("err", [])],
            config=config,
        )
        # P1 error but config only blocks P0 — should allow
        result = ReviewResult = asyncio.run(runner.pre_merge(_run(errors=["e"])))
        # ErrorRateReviewer produces P1 — but this runner has custom reviewer
        # Use default reviewers with custom config
        runner2 = ReviewRunner(config=config)
        _, decision = asyncio.run(runner2.pre_merge(_run(errors=["boom"])))
        assert decision.allowed is True  # P1 from error_rate, but config only blocks P0

    def test_generic_run_method(self):
        runner = ReviewRunner(reviewers=[_FixedReviewer("noop", [])])
        result, decision = asyncio.run(runner.run(HookKind.ON_PLAN, run_id="r99", plan={}))
        assert result.hook == HookKind.ON_PLAN
        assert result.run_id == "r99"
        assert decision.allowed is True

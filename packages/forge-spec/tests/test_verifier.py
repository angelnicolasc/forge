"""Tests for forge_spec.verifier."""

from __future__ import annotations

import asyncio

import pytest

from forge_core.types import RunResult, RunStatus
from forge_spec.types import (
    AcceptanceCriterion,
    CheckType,
    CriterionStatus,
    SpecDef,
)
from forge_spec.verifier import SpecVerifier, _compute_compliance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(*criteria: AcceptanceCriterion) -> SpecDef:
    return SpecDef(name="test", acceptance_criteria=list(criteria))


def _criterion(
    cid: str,
    check_type: CheckType,
    expected,
    weight: float = 1.0,
) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=cid,
        description=f"criterion {cid}",
        check_type=check_type,
        expected=expected,
        weight=weight,
    )


def _run(output=None, status: RunStatus = RunStatus.COMPLETED) -> RunResult:
    return RunResult(task_id="t1", status=status, output=output)


# ---------------------------------------------------------------------------
# ASSERTION
# ---------------------------------------------------------------------------


class TestAssertionCheck:
    def test_passes_when_true(self):
        spec = _spec(_criterion("ac-1", CheckType.ASSERTION, "run_result.status == 'completed'"))
        run = _run(status=RunStatus.COMPLETED)
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.PASSED

    def test_fails_when_false(self):
        spec = _spec(_criterion("ac-1", CheckType.ASSERTION, "run_result.status == 'completed'"))
        run = _run(status=RunStatus.FAILED)
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.FAILED

    def test_error_on_invalid_expression(self):
        spec = _spec(_criterion("ac-1", CheckType.ASSERTION, "this_is_undefined_var"))
        run = _run()
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.ERROR

    def test_restricted_builtins_open_blocked(self):
        spec = _spec(_criterion("ac-1", CheckType.ASSERTION, "open('/etc/passwd').read()"))
        run = _run()
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.ERROR

    def test_safe_builtin_len(self):
        spec = _spec(_criterion("ac-1", CheckType.ASSERTION, "len(run_result.errors) == 0"))
        run = _run()
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.PASSED


# ---------------------------------------------------------------------------
# REGEX
# ---------------------------------------------------------------------------


class TestRegexCheck:
    def test_passes_when_pattern_found(self):
        spec = _spec(_criterion("ac-1", CheckType.REGEX, r"\d{3}"))
        run = _run(output="result: 123")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.PASSED

    def test_fails_when_pattern_missing(self):
        spec = _spec(_criterion("ac-1", CheckType.REGEX, r"\d{3}"))
        run = _run(output="no numbers here")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.FAILED

    def test_error_on_invalid_regex(self):
        spec = _spec(_criterion("ac-1", CheckType.REGEX, "[invalid"))
        run = _run(output="anything")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.ERROR


# ---------------------------------------------------------------------------
# CONTAINS
# ---------------------------------------------------------------------------


class TestContainsCheck:
    def test_passes_when_substring_found(self):
        spec = _spec(_criterion("ac-1", CheckType.CONTAINS, "recommendation"))
        run = _run(output="Here is a recommendation for you.")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.PASSED

    def test_fails_when_substring_missing(self):
        spec = _spec(_criterion("ac-1", CheckType.CONTAINS, "recommendation"))
        run = _run(output="no useful output")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.FAILED

    def test_none_output_coerced_to_string(self):
        spec = _spec(_criterion("ac-1", CheckType.CONTAINS, "None"))
        run = _run(output=None)
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.PASSED


# ---------------------------------------------------------------------------
# LLM_JUDGE / SCRIPT — deferred
# ---------------------------------------------------------------------------


class TestDeferredCheckTypes:
    def test_llm_judge_returns_skipped(self):
        spec = _spec(_criterion("ac-1", CheckType.LLM_JUDGE, "some prompt"))
        run = _run()
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.SKIPPED

    def test_script_returns_skipped(self):
        spec = _spec(_criterion("ac-1", CheckType.SCRIPT, "./check.sh"))
        run = _run()
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.results[0].status == CriterionStatus.SKIPPED


# ---------------------------------------------------------------------------
# Compliance rate and passed flag
# ---------------------------------------------------------------------------


class TestComplianceRate:
    def test_all_passed(self):
        spec = _spec(
            _criterion("ac-1", CheckType.CONTAINS, "hello"),
            _criterion("ac-2", CheckType.CONTAINS, "world"),
        )
        run = _run(output="hello world")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.compliance_rate == 1.0
        assert vr.passed is True

    def test_half_passed(self):
        spec = _spec(
            _criterion("ac-1", CheckType.CONTAINS, "hello"),
            _criterion("ac-2", CheckType.CONTAINS, "MISSING"),
        )
        run = _run(output="hello")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert abs(vr.compliance_rate - 0.5) < 1e-9
        assert vr.passed is False

    def test_weighted_compliance(self):
        spec = _spec(
            _criterion("ac-1", CheckType.CONTAINS, "hello", weight=3.0),
            _criterion("ac-2", CheckType.CONTAINS, "MISSING", weight=1.0),
        )
        run = _run(output="hello")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert abs(vr.compliance_rate - 0.75) < 1e-9

    def test_skipped_deferred_not_counted(self):
        spec = _spec(
            _criterion("ac-1", CheckType.CONTAINS, "hello"),
            _criterion("ac-2", CheckType.LLM_JUDGE, "some prompt"),
        )
        run = _run(output="hello")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.compliance_rate == 1.0

    def test_empty_spec_zero_compliance(self):
        spec = _spec()
        run = _run()
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.compliance_rate == 0.0

    def test_passed_when_all_skipped_and_one_passed(self):
        spec = _spec(
            _criterion("ac-1", CheckType.CONTAINS, "hello"),
            _criterion("ac-2", CheckType.LLM_JUDGE, "skip me"),
        )
        run = _run(output="hello")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.passed is True


# ---------------------------------------------------------------------------
# EventBus integration
# ---------------------------------------------------------------------------


class TestEventBusIntegration:
    def test_spec_verified_event_emitted(self):
        events = []

        class FakeBus:
            async def publish(self, event):
                events.append(event)

        spec = _spec(_criterion("ac-1", CheckType.CONTAINS, "x"))
        run = _run(output="x")
        asyncio.run(SpecVerifier(bus=FakeBus()).verify(spec, run))
        assert len(events) == 1
        from forge_core.types import RunEventKind
        assert events[0].kind == RunEventKind.SPEC_VERIFIED

    def test_no_event_without_bus(self):
        spec = _spec(_criterion("ac-1", CheckType.CONTAINS, "x"))
        run = _run(output="x")
        vr = asyncio.run(SpecVerifier().verify(spec, run))
        assert vr.passed is True


# ---------------------------------------------------------------------------
# _compute_compliance (unit)
# ---------------------------------------------------------------------------


class TestComputeCompliance:
    def test_empty_returns_zero(self):
        assert _compute_compliance([], []) == 0.0

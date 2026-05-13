"""Tests for forge_spec.constraints.SpecConstraintGuard and forge_spec.runner.SpecRunner."""

from __future__ import annotations

import asyncio

import pytest

from forge_core.types import MutationKind, RunResult, RunStatus
from forge_spec.constraints import ConstraintViolationError, SpecConstraintGuard
from forge_spec.runner import SpecRunner
from forge_spec.types import (
    AcceptanceCriterion,
    CheckType,
    SpecConstraint,
    SpecDef,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec(**kwargs) -> SpecDef:
    defaults = dict(name="test-spec")
    return SpecDef(**{**defaults, **kwargs})


def _run(output="hello") -> RunResult:
    return RunResult(task_id="t1", status=RunStatus.COMPLETED, output=output)


# ---------------------------------------------------------------------------
# SpecConstraintGuard
# ---------------------------------------------------------------------------


class TestSpecConstraintGuard:
    def test_no_constraints_allows_all(self):
        spec = _make_spec()
        guard = SpecConstraintGuard(spec)
        for kind in MutationKind:
            assert guard.is_allowed(kind)

    def test_empty_blocked_list_blocks_all(self):
        spec = _make_spec(
            constraints=[SpecConstraint(id="c1", description="no mutations")]
        )
        guard = SpecConstraintGuard(spec)
        for kind in MutationKind:
            assert not guard.is_allowed(kind)

    def test_specific_kind_blocked(self):
        spec = _make_spec(
            constraints=[
                SpecConstraint(
                    id="c1",
                    description="no model swap",
                    mutation_kinds_blocked=[MutationKind.MODEL_SWAP],
                )
            ]
        )
        guard = SpecConstraintGuard(spec)
        assert not guard.is_allowed(MutationKind.MODEL_SWAP)
        assert guard.is_allowed(MutationKind.PROMPT_REWRITE)

    def test_check_raises_on_violation(self):
        spec = _make_spec(
            constraints=[
                SpecConstraint(
                    id="c1",
                    description="no model swap",
                    mutation_kinds_blocked=[MutationKind.MODEL_SWAP],
                )
            ]
        )
        guard = SpecConstraintGuard(spec)
        with pytest.raises(ConstraintViolationError) as exc_info:
            guard.check(MutationKind.MODEL_SWAP)
        assert "c1" in str(exc_info.value)
        assert len(exc_info.value.violations) == 1

    def test_check_passes_when_allowed(self):
        spec = _make_spec()
        guard = SpecConstraintGuard(spec)
        guard.check(MutationKind.MODEL_SWAP)  # should not raise

    def test_blocked_kinds_returns_set(self):
        spec = _make_spec(
            constraints=[
                SpecConstraint(
                    id="c1",
                    description="block two",
                    mutation_kinds_blocked=[MutationKind.MODEL_SWAP, MutationKind.AGENT_ADD],
                )
            ]
        )
        guard = SpecConstraintGuard(spec)
        blocked = guard.blocked_kinds()
        assert MutationKind.MODEL_SWAP in blocked
        assert MutationKind.AGENT_ADD in blocked
        assert MutationKind.PROMPT_REWRITE not in blocked

    def test_blocked_kinds_with_empty_list_blocks_all_mutation_kinds(self):
        spec = _make_spec(
            constraints=[SpecConstraint(id="c1", description="no mutations at all")]
        )
        guard = SpecConstraintGuard(spec)
        assert guard.blocked_kinds() == set(MutationKind)

    def test_allowed_kinds_complement_of_blocked(self):
        spec = _make_spec(
            constraints=[
                SpecConstraint(
                    id="c1",
                    description="no swap",
                    mutation_kinds_blocked=[MutationKind.MODEL_SWAP],
                )
            ]
        )
        guard = SpecConstraintGuard(spec)
        allowed = guard.allowed_kinds()
        blocked = guard.blocked_kinds()
        assert MutationKind.MODEL_SWAP not in allowed
        assert MutationKind.MODEL_SWAP in blocked
        assert len(allowed) + len(blocked) == len(MutationKind)

    def test_violations_for_returns_list(self):
        spec = _make_spec(
            constraints=[
                SpecConstraint(
                    id="c1",
                    description="no swap",
                    mutation_kinds_blocked=[MutationKind.MODEL_SWAP],
                )
            ]
        )
        guard = SpecConstraintGuard(spec)
        violations = guard.violations_for(MutationKind.MODEL_SWAP)
        assert len(violations) == 1
        assert violations[0].id == "c1"

    def test_violations_for_returns_empty_when_allowed(self):
        spec = _make_spec()
        guard = SpecConstraintGuard(spec)
        assert guard.violations_for(MutationKind.MODEL_SWAP) == []

    def test_multiple_constraints_can_all_violate(self):
        spec = _make_spec(
            constraints=[
                SpecConstraint(id="c1", description="a", mutation_kinds_blocked=[MutationKind.MODEL_SWAP]),
                SpecConstraint(id="c2", description="b", mutation_kinds_blocked=[MutationKind.MODEL_SWAP]),
            ]
        )
        guard = SpecConstraintGuard(spec)
        with pytest.raises(ConstraintViolationError) as exc_info:
            guard.check(MutationKind.MODEL_SWAP)
        assert len(exc_info.value.violations) == 2


# ---------------------------------------------------------------------------
# SpecRunner
# ---------------------------------------------------------------------------


class TestSpecRunner:
    def _make_spec_with_criteria(self) -> SpecDef:
        return _make_spec(
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="ac-1",
                    description="output contains hello",
                    check_type=CheckType.CONTAINS,
                    expected="hello",
                )
            ]
        )

    def test_run_pipeline_returns_tuple(self):
        spec = self._make_spec_with_criteria()
        run = _run(output="hello world")
        vr, att = asyncio.run(SpecRunner().run_pipeline(spec, run))
        assert vr.passed is True
        assert att.spec_name == "test-spec"

    def test_run_pipeline_attestation_matches_verify(self):
        spec = self._make_spec_with_criteria()
        run = _run(output="hello world")
        vr, att = asyncio.run(SpecRunner().run_pipeline(spec, run))
        assert att.passed == vr.passed
        assert att.compliance_rate == vr.compliance_rate
        assert att.spec_hash == vr.spec_hash

    def test_verify_only(self):
        spec = self._make_spec_with_criteria()
        run = _run(output="hello")
        vr = asyncio.run(SpecRunner().verify(spec, run))
        assert vr.compliance_rate == 1.0

    def test_attest_only(self):
        spec = self._make_spec_with_criteria()
        run = _run(output="hello")
        runner = SpecRunner()
        vr = asyncio.run(runner.verify(spec, run))
        att = runner.attest(spec, vr)
        assert att.total_criteria == 1

    def test_guard_returns_constraint_guard(self):
        spec = _make_spec(
            constraints=[
                SpecConstraint(
                    id="c1",
                    description="no swap",
                    mutation_kinds_blocked=[MutationKind.MODEL_SWAP],
                )
            ]
        )
        guard = SpecRunner().guard(spec)
        assert not guard.is_allowed(MutationKind.MODEL_SWAP)

    def test_refine_no_refiner_returns_same_spec(self):
        spec = self._make_spec_with_criteria()
        run = _run()
        runner = SpecRunner()
        vr = asyncio.run(runner.verify(spec, run))
        refined = asyncio.run(runner.refine(spec, vr))
        assert refined is spec

    def test_refine_with_refiner(self):
        spec = self._make_spec_with_criteria()
        new_spec = _make_spec(name="refined-spec")

        class FakeRefiner:
            async def refine(self, s, vr):
                return new_spec

        run = _run()
        runner = SpecRunner(refiner=FakeRefiner())
        vr = asyncio.run(runner.verify(spec, run))
        refined = asyncio.run(runner.refine(spec, vr))
        assert refined.name == "refined-spec"

    def test_event_bus_wired_to_verifier(self):
        events = []

        class FakeBus:
            async def publish(self, event):
                events.append(event)

        spec = self._make_spec_with_criteria()
        run = _run(output="hello")
        asyncio.run(SpecRunner(bus=FakeBus()).run_pipeline(spec, run))
        assert len(events) == 1

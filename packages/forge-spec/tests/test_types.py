"""Tests for forge_spec.types."""

from __future__ import annotations

from forge_core.types import MutationKind
from forge_spec.types import (
    AcceptanceCriterion,
    CheckType,
    CriterionResult,
    CriterionStatus,
    Risk,
    SpecAttestation,
    SpecConstraint,
    SpecDef,
    VerifyResult,
)

# ---------------------------------------------------------------------------
# AcceptanceCriterion
# ---------------------------------------------------------------------------


class TestAcceptanceCriterion:
    def test_defaults(self):
        c = AcceptanceCriterion(id="ac-1", description="test")
        assert c.check_type == CheckType.ASSERTION
        assert c.weight == 1.0
        assert c.tags == []
        assert c.expected is None

    def test_custom(self):
        c = AcceptanceCriterion(
            id="ac-2",
            description="contains check",
            check_type=CheckType.CONTAINS,
            expected="hello",
            weight=2.5,
            tags=["smoke"],
        )
        assert c.check_type == CheckType.CONTAINS
        assert c.weight == 2.5
        assert "smoke" in c.tags

    def test_check_type_values(self):
        for ct in CheckType:
            c = AcceptanceCriterion(id="x", description="d", check_type=ct)
            assert c.check_type == ct


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class TestRisk:
    def test_score(self):
        r = Risk(id="r1", description="d", likelihood=0.4, impact=0.5)
        assert abs(r.score - 0.2) < 1e-9

    def test_defaults(self):
        r = Risk(id="r1", description="d")
        assert r.likelihood == 0.5
        assert r.impact == 0.5
        assert r.mitigation == ""

    def test_score_bounds(self):
        r = Risk(id="r1", description="d", likelihood=1.0, impact=1.0)
        assert r.score == 1.0
        r2 = Risk(id="r2", description="d", likelihood=0.0, impact=0.0)
        assert r2.score == 0.0


# ---------------------------------------------------------------------------
# SpecConstraint
# ---------------------------------------------------------------------------


class TestSpecConstraint:
    def test_empty_blocks_all(self):
        c = SpecConstraint(id="c1", description="no mutations")
        assert c.mutation_kinds_blocked == []

    def test_specific_kinds(self):
        c = SpecConstraint(
            id="c2",
            description="no swap",
            mutation_kinds_blocked=[MutationKind.MODEL_SWAP],
        )
        assert MutationKind.MODEL_SWAP in c.mutation_kinds_blocked


# ---------------------------------------------------------------------------
# SpecDef
# ---------------------------------------------------------------------------


class TestSpecDef:
    def _make_spec(self, **kwargs) -> SpecDef:
        defaults = dict(name="test-spec", version="0.1.0", description="")
        return SpecDef(**{**defaults, **kwargs})

    def test_defaults(self):
        s = self._make_spec()
        assert s.acceptance_criteria == []
        assert s.risks == []
        assert s.constraints == []
        assert s.objectives == []
        assert s.tags == {}

    def test_spec_hash_is_16_chars(self):
        s = self._make_spec()
        assert len(s.spec_hash) == 16

    def test_spec_hash_excludes_created_at(self):
        s1 = self._make_spec()
        import time

        time.sleep(0.01)
        s2 = self._make_spec()
        assert s1.spec_hash == s2.spec_hash

    def test_spec_hash_changes_with_name(self):
        s1 = self._make_spec(name="a")
        s2 = self._make_spec(name="b")
        assert s1.spec_hash != s2.spec_hash

    def test_criterion_by_id_found(self):
        c = AcceptanceCriterion(id="ac-1", description="d")
        s = self._make_spec(acceptance_criteria=[c])
        assert s.criterion_by_id("ac-1") is c

    def test_criterion_by_id_not_found(self):
        s = self._make_spec()
        assert s.criterion_by_id("missing") is None

    def test_model_roundtrip(self):
        s = self._make_spec(
            objectives=["obj1"],
            acceptance_criteria=[AcceptanceCriterion(id="ac-1", description="d")],
        )
        dumped = s.model_dump(mode="json", exclude_none=True)
        s2 = SpecDef.model_validate(dumped)
        assert s2.name == s.name
        assert s2.spec_hash == s.spec_hash


# ---------------------------------------------------------------------------
# VerifyResult
# ---------------------------------------------------------------------------


class TestVerifyResult:
    def _make_result(self, statuses: list[CriterionStatus]) -> VerifyResult:
        results = [
            CriterionResult(criterion_id=f"ac-{i}", status=s) for i, s in enumerate(statuses)
        ]
        return VerifyResult(
            spec_name="s",
            spec_version="0.1.0",
            spec_hash="abc",
            run_id="r1",
            results=results,
        )

    def test_summary_counts(self):
        vr = self._make_result(
            [CriterionStatus.PASSED, CriterionStatus.PASSED, CriterionStatus.FAILED]
        )
        assert vr.summary[CriterionStatus.PASSED] == 2
        assert vr.summary[CriterionStatus.FAILED] == 1

    def test_summary_all_zero_when_empty(self):
        vr = self._make_result([])
        for v in vr.summary.values():
            assert v == 0


# ---------------------------------------------------------------------------
# SpecAttestation
# ---------------------------------------------------------------------------


class TestSpecAttestation:
    def test_predicate_type(self):
        a = SpecAttestation(
            spec_name="s",
            spec_version="0.1.0",
            spec_hash="abc",
            run_id="r1",
            passed=True,
            compliance_rate=1.0,
            passed_criteria=1,
            failed_criteria=0,
            skipped_criteria=0,
            total_criteria=1,
        )
        assert a.predicate_type == "https://slsa.dev/provenance/v1"
        assert a.build_type == "https://forge.dev/spec/v1"
        assert len(a.id) == 16

    def test_roundtrip_json(self):
        import json

        a = SpecAttestation(
            spec_name="s",
            spec_version="0.1.0",
            spec_hash="abc",
            run_id="r1",
            passed=False,
            compliance_rate=0.5,
            passed_criteria=1,
            failed_criteria=1,
            skipped_criteria=0,
            total_criteria=2,
        )
        data = json.loads(a.model_dump_json())
        a2 = SpecAttestation.model_validate(data)
        assert a2.spec_name == a.spec_name
        assert a2.passed == a.passed

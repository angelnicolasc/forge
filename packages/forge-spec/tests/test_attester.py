"""Tests for forge_spec.attester."""

from __future__ import annotations

import json

from forge_spec.attester import SpecAttester
from forge_spec.types import (
    AcceptanceCriterion,
    CheckType,
    CriterionResult,
    CriterionStatus,
    SpecDef,
    VerifyResult,
)


def _make_spec(name: str = "test-spec") -> SpecDef:
    return SpecDef(
        name=name,
        acceptance_criteria=[
            AcceptanceCriterion(id="ac-1", description="d", check_type=CheckType.CONTAINS, expected="x"),
        ],
    )


def _make_verify_result(
    spec: SpecDef,
    *,
    passed: bool = True,
    compliance_rate: float = 1.0,
    statuses: list[CriterionStatus] | None = None,
) -> VerifyResult:
    if statuses is None:
        statuses = [CriterionStatus.PASSED]
    results = [
        CriterionResult(criterion_id=f"ac-{i}", status=s)
        for i, s in enumerate(statuses, 1)
    ]
    return VerifyResult(
        spec_name=spec.name,
        spec_version=spec.version,
        spec_hash=spec.spec_hash,
        run_id="run-abc",
        results=results,
        compliance_rate=compliance_rate,
        passed=passed,
    )


class TestSpecAttester:
    def test_attest_basic_fields(self):
        spec = _make_spec()
        vr = _make_verify_result(spec)
        att = SpecAttester().attest(spec, vr)

        assert att.spec_name == "test-spec"
        assert att.spec_version == spec.version
        assert att.spec_hash == spec.spec_hash
        assert att.run_id == "run-abc"
        assert att.passed is True
        assert att.compliance_rate == 1.0

    def test_attest_slsa_fields(self):
        spec = _make_spec()
        vr = _make_verify_result(spec)
        att = SpecAttester().attest(spec, vr)

        assert att.predicate_type == "https://slsa.dev/provenance/v1"
        assert att.build_type == "https://forge.dev/spec/v1"

    def test_attest_criterion_counts(self):
        spec = _make_spec()
        vr = _make_verify_result(
            spec,
            statuses=[CriterionStatus.PASSED, CriterionStatus.FAILED, CriterionStatus.SKIPPED],
            passed=False,
            compliance_rate=0.5,
        )
        att = SpecAttester().attest(spec, vr)

        assert att.passed_criteria == 1
        assert att.failed_criteria == 1
        assert att.skipped_criteria == 1
        assert att.total_criteria == 3

    def test_attest_materials(self):
        spec = _make_spec()
        vr = _make_verify_result(spec)
        att = SpecAttester().attest(spec, vr)

        assert len(att.materials) == 1
        mat = att.materials[0]
        assert "test-spec" in mat["uri"]
        assert mat["digest"]["sha256"] == spec.spec_hash

    def test_attest_id_is_unique(self):
        spec = _make_spec()
        vr = _make_verify_result(spec)
        attester = SpecAttester()
        a1 = attester.attest(spec, vr)
        a2 = attester.attest(spec, vr)
        assert a1.id != a2.id

    def test_to_json_is_valid_json(self):
        spec = _make_spec()
        vr = _make_verify_result(spec)
        att = SpecAttester().attest(spec, vr)
        json_str = SpecAttester().to_json(att)
        parsed = json.loads(json_str)
        assert parsed["spec_name"] == "test-spec"

    def test_to_yaml_contains_spec_name(self):
        spec = _make_spec()
        vr = _make_verify_result(spec)
        att = SpecAttester().attest(spec, vr)
        yaml_str = SpecAttester().to_yaml(att)
        assert "test-spec" in yaml_str

    def test_from_json_roundtrip(self):
        spec = _make_spec()
        vr = _make_verify_result(spec)
        attester = SpecAttester()
        att = attester.attest(spec, vr)
        json_str = attester.to_json(att)
        att2 = attester.from_json(json_str)
        assert att2.spec_name == att.spec_name
        assert att2.spec_hash == att.spec_hash
        assert att2.passed == att.passed

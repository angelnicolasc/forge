"""Tests for forge_spec.loader."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from forge_spec.loader import (
    dump_spec_yaml,
    load_acceptance_yaml,
    load_risk_register_yaml,
    load_spec_yaml,
    merge_spec_files,
)
from forge_spec.types import AcceptanceCriterion, CheckType, Risk, SpecDef


MINIMAL_SPEC = """\
name: test-spec
version: "0.1.0"
"""

FULL_SPEC = """\
name: full-spec
version: "0.2.0"
description: A full spec
objectives:
  - Achieve 99.9% uptime
acceptance_criteria:
  - id: ac-001
    description: Status must be completed
    check_type: assertion
    expected: "run_result.status == 'completed'"
    weight: 2.0
  - id: ac-002
    description: Output contains hello
    check_type: contains
    expected: hello
risks:
  - id: risk-001
    description: Model drift
    likelihood: 0.3
    impact: 0.7
    mitigation: Monitor outputs
"""

ACCEPTANCE_YAML = """\
acceptance_criteria:
  - id: ac-ext-1
    description: External criterion
    check_type: regex
    expected: "\\\\d+"
"""

RISK_YAML = """\
risks:
  - id: r-ext-1
    description: External risk
    likelihood: 0.2
    impact: 0.4
"""


# ---------------------------------------------------------------------------
# load_spec_yaml
# ---------------------------------------------------------------------------


class TestLoadSpecYaml:
    def test_from_str_minimal(self):
        spec = load_spec_yaml(MINIMAL_SPEC)
        assert spec.name == "test-spec"
        assert spec.version == "0.1.0"
        assert spec.acceptance_criteria == []

    def test_from_str_full(self):
        spec = load_spec_yaml(FULL_SPEC)
        assert spec.name == "full-spec"
        assert len(spec.acceptance_criteria) == 2
        assert spec.acceptance_criteria[0].weight == 2.0
        assert len(spec.risks) == 1

    def test_from_path(self, tmp_path):
        p = tmp_path / "spec.yaml"
        p.write_text(MINIMAL_SPEC, encoding="utf-8")
        spec = load_spec_yaml(p)
        assert spec.name == "test-spec"

    def test_from_bytes_io(self):
        buf = io.BytesIO(MINIMAL_SPEC.encode())
        spec = load_spec_yaml(buf)
        assert spec.name == "test-spec"

    def test_from_string_io(self):
        buf = io.StringIO(MINIMAL_SPEC)
        spec = load_spec_yaml(buf)
        assert spec.name == "test-spec"

    def test_invalid_yaml_raises(self):
        with pytest.raises(ValueError, match="YAML parse error"):
            load_spec_yaml(":\nbroken: yaml: [")

    def test_non_mapping_raises(self):
        with pytest.raises(ValueError, match="mapping"):
            load_spec_yaml("- item1\n- item2\n")

    def test_validation_error_raises(self):
        with pytest.raises(ValueError, match="Spec validation error"):
            load_spec_yaml("version: '0.1.0'\n")  # missing 'name'

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError, match="Unsupported source type"):
            load_spec_yaml(12345)


# ---------------------------------------------------------------------------
# load_acceptance_yaml
# ---------------------------------------------------------------------------


class TestLoadAcceptanceYaml:
    def test_from_str(self):
        criteria = load_acceptance_yaml(ACCEPTANCE_YAML)
        assert len(criteria) == 1
        assert criteria[0].id == "ac-ext-1"
        assert criteria[0].check_type == CheckType.REGEX

    def test_alias_criteria_key(self):
        yaml_text = """\
criteria:
  - id: ac-a
    description: from alias
"""
        criteria = load_acceptance_yaml(yaml_text)
        assert len(criteria) == 1
        assert criteria[0].id == "ac-a"

    def test_empty_list(self):
        criteria = load_acceptance_yaml("acceptance_criteria: []\n")
        assert criteria == []

    def test_non_list_raises(self):
        with pytest.raises(ValueError, match="must contain a list"):
            load_acceptance_yaml("acceptance_criteria: not_a_list\n")


# ---------------------------------------------------------------------------
# load_risk_register_yaml
# ---------------------------------------------------------------------------


class TestLoadRiskRegisterYaml:
    def test_from_str(self):
        risks = load_risk_register_yaml(RISK_YAML)
        assert len(risks) == 1
        assert risks[0].id == "r-ext-1"
        assert abs(risks[0].score - 0.08) < 1e-9

    def test_alias_risk_register_key(self):
        yaml_text = """\
risk_register:
  - id: r-alias
    description: from alias
"""
        risks = load_risk_register_yaml(yaml_text)
        assert len(risks) == 1
        assert risks[0].id == "r-alias"

    def test_non_list_raises(self):
        with pytest.raises(ValueError, match="must contain a list"):
            load_risk_register_yaml("risks: not_a_list\n")


# ---------------------------------------------------------------------------
# merge_spec_files
# ---------------------------------------------------------------------------


class TestMergeSpecFiles:
    def test_merge_acceptance_replaces_inline(self):
        spec = merge_spec_files(FULL_SPEC, acceptance_source=ACCEPTANCE_YAML)
        assert len(spec.acceptance_criteria) == 1
        assert spec.acceptance_criteria[0].id == "ac-ext-1"
        assert len(spec.risks) == 1  # risks from inline spec unchanged

    def test_merge_risks_replaces_inline(self):
        spec = merge_spec_files(FULL_SPEC, risk_source=RISK_YAML)
        assert len(spec.risks) == 1
        assert spec.risks[0].id == "r-ext-1"
        assert len(spec.acceptance_criteria) == 2  # criteria unchanged

    def test_merge_both(self):
        spec = merge_spec_files(
            FULL_SPEC,
            acceptance_source=ACCEPTANCE_YAML,
            risk_source=RISK_YAML,
        )
        assert spec.acceptance_criteria[0].id == "ac-ext-1"
        assert spec.risks[0].id == "r-ext-1"

    def test_no_merge_sources(self):
        spec = merge_spec_files(FULL_SPEC)
        assert len(spec.acceptance_criteria) == 2


# ---------------------------------------------------------------------------
# dump_spec_yaml
# ---------------------------------------------------------------------------


class TestDumpSpecYaml:
    def test_roundtrip(self):
        spec = load_spec_yaml(FULL_SPEC)
        dumped = dump_spec_yaml(spec)
        reloaded = load_spec_yaml(dumped)
        assert reloaded.name == spec.name
        assert len(reloaded.acceptance_criteria) == len(spec.acceptance_criteria)

    def test_output_is_str(self):
        spec = load_spec_yaml(MINIMAL_SPEC)
        assert isinstance(dump_spec_yaml(spec), str)

    def test_name_in_output(self):
        spec = load_spec_yaml(MINIMAL_SPEC)
        dumped = dump_spec_yaml(spec)
        assert "test-spec" in dumped

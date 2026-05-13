"""Tests for forge_rules.loader — YAML round-trip."""

from __future__ import annotations

import io
import textwrap

import pytest

from forge_rules.loader import dump_yaml, load_yaml
from forge_rules.types import RuleAction, RulePack


VALID_YAML = textwrap.dedent("""
    name: safety-rules
    version: "1.0.0"
    rules:
      - id: no-eval
        action: deny
        description: "Prohibit eval()"
        content: "Never use eval() or exec()."
        scope:
          - "**/*.py"
        intent_tags: []
        priority: 0
        enabled: true
""").strip()


class TestLoadYaml:
    def test_load_from_string(self) -> None:
        pack = load_yaml(VALID_YAML)
        assert pack.name == "safety-rules"
        assert len(pack.rules) == 1
        assert pack.rules[0].id == "no-eval"
        assert pack.rules[0].action == RuleAction.DENY

    def test_load_from_file_like(self) -> None:
        buf = io.StringIO(VALID_YAML)
        pack = load_yaml(buf)
        assert pack.name == "safety-rules"

    def test_load_from_bytes_file_like(self) -> None:
        buf = io.BytesIO(VALID_YAML.encode("utf-8"))
        pack = load_yaml(buf)
        assert pack.name == "safety-rules"

    def test_invalid_yaml_raises_value_error(self) -> None:
        bad_yaml = "{"  # incomplete YAML
        with pytest.raises(ValueError, match="YAML parse error"):
            load_yaml(bad_yaml)

    def test_non_mapping_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="mapping"):
            load_yaml("- item1\n- item2")

    def test_validation_error_raises_value_error(self) -> None:
        # Missing required 'name' field
        bad = "version: '1.0.0'\nrules: []"
        with pytest.raises(ValueError, match="validation error"):
            load_yaml(bad)

    def test_unsupported_type_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            load_yaml(42)  # type: ignore[arg-type]


class TestDumpYaml:
    def test_roundtrip(self) -> None:
        pack = load_yaml(VALID_YAML)
        dumped = dump_yaml(pack)
        reloaded = load_yaml(dumped)
        assert reloaded.name == pack.name
        assert len(reloaded.rules) == len(pack.rules)
        assert reloaded.rules[0].id == pack.rules[0].id

    def test_output_is_string(self) -> None:
        pack = load_yaml(VALID_YAML)
        dumped = dump_yaml(pack)
        assert isinstance(dumped, str)
        assert "name:" in dumped

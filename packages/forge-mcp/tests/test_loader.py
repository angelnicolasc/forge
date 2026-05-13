"""Tests for forge_mcp.loader — YAML config round-trip."""
from __future__ import annotations

import io
import os
import textwrap

import pytest

from forge_mcp.loader import dump_yaml, load_yaml
from forge_mcp.types import ToolTransport


VALID_YAML = textwrap.dedent("""
    cache_ttl: 120
    servers:
      - name: filesystem
        transport: stdio
        command: ["npx", "-y", "server-filesystem", "/tmp"]
        tools:
          read_file:
            allowed: true
            max_calls_per_run: 20
          write_file:
            allowed: false
      - name: github
        transport: stdio
        command: ["npx", "server-github"]
        tools:
          search_code:
            allowed: true
""").strip()


class TestLoadYaml:
    def test_load_from_string(self) -> None:
        servers, options = load_yaml(VALID_YAML)
        assert len(servers) == 2
        assert servers[0].name == "filesystem"
        assert servers[1].name == "github"
        assert options["cache_ttl"] == 120

    def test_load_from_file_like(self) -> None:
        buf = io.StringIO(VALID_YAML)
        servers, _ = load_yaml(buf)
        assert servers[0].name == "filesystem"

    def test_load_from_bytes_file_like(self) -> None:
        buf = io.BytesIO(VALID_YAML.encode("utf-8"))
        servers, _ = load_yaml(buf)
        assert len(servers) == 2

    def test_tool_policies_parsed(self) -> None:
        servers, _ = load_yaml(VALID_YAML)
        fs = servers[0]
        assert fs.tools["read_file"].allowed is True
        assert fs.tools["read_file"].max_calls_per_run == 20
        assert fs.tools["write_file"].allowed is False

    def test_invalid_yaml_raises(self) -> None:
        with pytest.raises(ValueError, match="YAML parse error"):
            load_yaml("{unclosed")

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ValueError, match="mapping"):
            load_yaml("- item1\n- item2")

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError):
            load_yaml(42)  # type: ignore[arg-type]

    def test_env_var_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_TOKEN", "secret123")
        yaml_str = textwrap.dedent("""
            servers:
              - name: github
                transport: stdio
                command: ["npx", "server"]
                env:
                  GITHUB_TOKEN: "${MY_TOKEN}"
        """).strip()
        servers, _ = load_yaml(yaml_str)
        assert servers[0].env["GITHUB_TOKEN"] == "secret123"

    def test_env_var_not_set_leaves_placeholder(self) -> None:
        yaml_str = textwrap.dedent("""
            servers:
              - name: s
                command: ["npx", "server"]
                env:
                  KEY: "${UNSET_VAR_XYZ}"
        """).strip()
        servers, _ = load_yaml(yaml_str)
        assert servers[0].env["KEY"] == "${UNSET_VAR_XYZ}"


class TestDumpYaml:
    def test_roundtrip(self) -> None:
        servers, options = load_yaml(VALID_YAML)
        dumped = dump_yaml(servers, options)
        servers2, options2 = load_yaml(dumped)
        assert len(servers2) == len(servers)
        assert servers2[0].name == servers[0].name
        assert options2.get("cache_ttl") == options.get("cache_ttl")

    def test_output_is_string(self) -> None:
        servers, _ = load_yaml(VALID_YAML)
        result = dump_yaml(servers)
        assert isinstance(result, str)
        assert "name:" in result

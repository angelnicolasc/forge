"""Tests for forge_mcp.security — deny-by-default and secret redaction."""
from __future__ import annotations

import pytest

from forge_mcp.security import get_policy, is_allowed, redact_secrets, redact_value
from forge_mcp.types import ServerConfig, ToolPolicy


def _server(tools: dict[str, ToolPolicy] | None = None) -> ServerConfig:
    return ServerConfig(name="test", tools=tools or {})


class TestIsAllowed:
    def test_no_policy_denied(self) -> None:
        server = _server({})
        assert is_allowed(server, "read_file") is False

    def test_allowed_false_denied(self) -> None:
        server = _server({"read_file": ToolPolicy(allowed=False)})
        assert is_allowed(server, "read_file") is False

    def test_allowed_true_permitted(self) -> None:
        server = _server({"read_file": ToolPolicy(allowed=True)})
        assert is_allowed(server, "read_file") is True

    def test_caller_allowlist_match(self) -> None:
        policy = ToolPolicy(allowed=True, allowed_callers=["agent-1"])
        server = _server({"tool": policy})
        assert is_allowed(server, "tool", caller_id="agent-1") is True

    def test_caller_allowlist_no_match(self) -> None:
        policy = ToolPolicy(allowed=True, allowed_callers=["agent-1"])
        server = _server({"tool": policy})
        assert is_allowed(server, "tool", caller_id="agent-2") is False

    def test_empty_allowlist_any_caller(self) -> None:
        policy = ToolPolicy(allowed=True, allowed_callers=[])
        server = _server({"tool": policy})
        assert is_allowed(server, "tool", caller_id="anyone") is True
        assert is_allowed(server, "tool", caller_id="") is True


class TestGetPolicy:
    def test_returns_policy(self) -> None:
        p = ToolPolicy(allowed=True)
        server = _server({"t": p})
        assert get_policy(server, "t") is p

    def test_returns_none_for_missing(self) -> None:
        server = _server({})
        assert get_policy(server, "missing") is None


class TestRedactSecrets:
    def test_api_key_redacted(self) -> None:
        text = "api_key=sk-abcdef123456789012345"
        result = redact_secrets(text)
        assert "sk-abcdef123456789012345" not in result
        assert "[REDACTED]" in result

    def test_openai_key_redacted(self) -> None:
        text = "key is sk-abc1234567890abcdef1234567890"
        result = redact_secrets(text)
        assert "sk-abc" not in result

    def test_github_pat_redacted(self) -> None:
        text = "token: ghp_abcdefghijklmnopqrstu"
        result = redact_secrets(text)
        assert "ghp_" not in result

    def test_clean_text_unchanged(self) -> None:
        text = "The file contains 42 lines of Python code."
        assert redact_secrets(text) == text

    def test_jwt_redacted(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
        result = redact_secrets(jwt)
        assert "eyJ" not in result


class TestRedactValue:
    def test_string_redacted(self) -> None:
        p = ToolPolicy(allowed=True, redact_secrets=True)
        result = redact_value("api_key=sk-abc1234567890abcdef1234", p)
        assert "sk-abc" not in result

    def test_redact_disabled(self) -> None:
        p = ToolPolicy(allowed=True, redact_secrets=False)
        text = "api_key=sk-abc1234567890abcdef1234"
        assert redact_value(text, p) == text

    def test_dict_recursed(self) -> None:
        p = ToolPolicy(allowed=True, redact_secrets=True)
        data = {"key": "ghp_abcdefghijklmnopqrstu", "count": 42}
        result = redact_value(data, p)
        assert isinstance(result, dict)
        assert "ghp_" not in result["key"]
        assert result["count"] == 42

    def test_list_recursed(self) -> None:
        p = ToolPolicy(allowed=True, redact_secrets=True)
        data = ["ghp_abcdefghijklmnopqrstu", "normal"]
        result = redact_value(data, p)
        assert isinstance(result, list)
        assert "ghp_" not in result[0]
        assert result[1] == "normal"

    def test_non_string_passthrough(self) -> None:
        p = ToolPolicy(allowed=True, redact_secrets=True)
        assert redact_value(42, p) == 42
        assert redact_value(None, p) is None

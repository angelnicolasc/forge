"""Tests for forge_mcp.types."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge_mcp.types import (
    BudgetSnapshot,
    ServerConfig,
    ToolCall,
    ToolPolicy,
    ToolResult,
    ToolTransport,
)


class TestToolPolicy:
    def test_deny_by_default(self) -> None:
        p = ToolPolicy()
        assert p.allowed is False

    def test_explicit_allow(self) -> None:
        p = ToolPolicy(allowed=True, max_calls_per_run=5)
        assert p.allowed is True
        assert p.max_calls_per_run == 5

    def test_defaults(self) -> None:
        p = ToolPolicy()
        assert p.max_calls_per_run == 10
        assert p.max_tokens_per_call == 8192
        assert p.redact_secrets is True
        assert p.allowed_callers == []
        assert p.cache_ttl_seconds is None


class TestServerConfig:
    def test_defaults(self) -> None:
        cfg = ServerConfig(name="fs")
        assert cfg.transport == ToolTransport.STDIO
        assert cfg.enabled is True
        assert cfg.tools == {}
        assert cfg.circuit_breaker_threshold == 5

    def test_with_tools(self) -> None:
        cfg = ServerConfig(
            name="fs",
            command=["npx", "server-filesystem"],
            tools={"read_file": ToolPolicy(allowed=True)},
        )
        assert "read_file" in cfg.tools
        assert cfg.tools["read_file"].allowed is True

    def test_sse_transport(self) -> None:
        cfg = ServerConfig(name="remote", transport=ToolTransport.SSE, url="http://localhost:8080")
        assert cfg.transport == ToolTransport.SSE
        assert cfg.url == "http://localhost:8080"

    def test_all_transports_valid(self) -> None:
        for t in ToolTransport:
            cfg = ServerConfig(name="s", transport=t)
            assert cfg.transport == t


class TestToolCall:
    def test_defaults(self) -> None:
        call = ToolCall(tool_name="read_file")
        assert call.arguments == {}
        assert call.caller_id == ""
        assert call.run_id == ""

    def test_full_call(self) -> None:
        call = ToolCall(
            tool_name="write_file",
            arguments={"path": "/tmp/x.txt", "content": "hello"},
            caller_id="agent-1",
            run_id="run-xyz",
        )
        assert call.arguments["path"] == "/tmp/x.txt"
        assert call.caller_id == "agent-1"


class TestToolResult:
    def test_success(self) -> None:
        r = ToolResult(tool_name="read_file", content="file data")
        assert r.ok is True
        assert r.error is None
        assert r.cached is False

    def test_error(self) -> None:
        r = ToolResult(tool_name="read_file", error="timeout")
        assert r.ok is False

    def test_cached(self) -> None:
        r = ToolResult(tool_name="t", content="x", cached=True)
        assert r.cached is True


class TestBudgetSnapshot:
    def test_empty(self) -> None:
        s = BudgetSnapshot()
        assert s.total_calls == 0
        assert s.total_tokens == 0
        assert s.calls_by_tool == {}

    def test_with_data(self) -> None:
        s = BudgetSnapshot(
            calls_by_tool={"read_file": 3, "write_file": 1},
            tokens_by_tool={"read_file": 100},
            total_calls=4,
            total_tokens=100,
        )
        assert s.calls_by_tool["read_file"] == 3
        assert s.total_calls == 4

"""Tests for forge_mcp.server — MetaMCPServer integration."""
from __future__ import annotations

import asyncio

import pytest

from forge_mcp.client import FakeMCPClient
from forge_mcp.server import MetaMCPServer, ToolDeniedError
from forge_mcp.types import ServerConfig, ToolCall, ToolPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _server(
    name: str = "fs",
    tools: dict[str, ToolPolicy] | None = None,
    cb_threshold: int = 5,
) -> ServerConfig:
    return ServerConfig(
        name=name,
        tools=tools or {},
        circuit_breaker_threshold=cb_threshold,
        circuit_breaker_timeout=30.0,
    )


def _allowed(max_calls: int = 10, redact: bool = True) -> ToolPolicy:
    return ToolPolicy(allowed=True, max_calls_per_run=max_calls, redact_secrets=redact)


def _call(
    tool: str,
    args: dict | None = None,
    caller: str = "",
    run_id: str = "",
) -> ToolCall:
    return ToolCall(tool_name=tool, arguments=args or {}, caller_id=caller, run_id=run_id)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMetaMCPServerHappyPath:
    def test_successful_tool_call(self) -> None:
        meta = MetaMCPServer()
        cfg = _server(tools={"read_file": _allowed()})
        client = FakeMCPClient(responses={"read_file": "file contents"})
        meta.add_server(cfg, client)

        result = asyncio.run(meta.call_tool(_call("read_file")))
        assert result.ok
        assert result.content == "file contents"
        assert result.server_name == "fs"
        assert not result.cached

    def test_list_tools_includes_registered(self) -> None:
        meta = MetaMCPServer()
        cfg = _server(tools={"read_file": _allowed(), "write_file": _allowed()})
        meta.add_server(cfg, FakeMCPClient())
        assert set(meta.list_tools()) == {"read_file", "write_file"}

    def test_list_servers(self) -> None:
        meta = MetaMCPServer()
        meta.add_server(_server("fs"), FakeMCPClient())
        meta.add_server(_server("github"), FakeMCPClient())
        assert set(meta.list_servers()) == {"fs", "github"}

    def test_remove_server(self) -> None:
        meta = MetaMCPServer()
        meta.add_server(_server("fs", tools={"read_file": _allowed()}), FakeMCPClient())
        assert meta.remove_server("fs") is True
        assert meta.remove_server("ghost") is False
        assert meta.list_tools() == []


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_denied_tool_raises(self) -> None:
        meta = MetaMCPServer()
        cfg = _server(tools={"read_file": ToolPolicy(allowed=False)})
        meta.add_server(cfg, FakeMCPClient())
        with pytest.raises(ToolDeniedError):
            asyncio.run(meta.call_tool(_call("read_file")))

    def test_undeclared_tool_raises(self) -> None:
        meta = MetaMCPServer()
        meta.add_server(_server(tools={}), FakeMCPClient())
        with pytest.raises(ToolDeniedError):
            asyncio.run(meta.call_tool(_call("ghost")))

    def test_unknown_tool_routing_error_returns_result_error(self) -> None:
        meta = MetaMCPServer()  # no servers registered
        result = asyncio.run(meta.call_tool(_call("unknown")))
        assert not result.ok
        assert "No server registered" in result.error

    def test_caller_allowlist_denied(self) -> None:
        policy = ToolPolicy(allowed=True, allowed_callers=["agent-1"])
        meta = MetaMCPServer()
        cfg = _server(tools={"tool": policy})
        meta.add_server(cfg, FakeMCPClient())
        with pytest.raises(ToolDeniedError):
            asyncio.run(meta.call_tool(_call("tool", caller="agent-2")))

    def test_caller_allowlist_allowed(self) -> None:
        policy = ToolPolicy(allowed=True, allowed_callers=["agent-1"])
        meta = MetaMCPServer()
        cfg = _server(tools={"tool": policy})
        meta.add_server(cfg, FakeMCPClient(responses={"tool": "ok"}))
        result = asyncio.run(meta.call_tool(_call("tool", caller="agent-1")))
        assert result.ok

    def test_secret_redaction_applied(self) -> None:
        policy = ToolPolicy(allowed=True, redact_secrets=True)
        meta = MetaMCPServer(enable_cache=False)
        cfg = _server(tools={"get_token": policy})
        client = FakeMCPClient(responses={"get_token": "token: sk-abc1234567890abcdef12345"})
        meta.add_server(cfg, client)
        result = asyncio.run(meta.call_tool(_call("get_token")))
        assert result.ok
        assert "sk-abc" not in str(result.content)

    def test_redaction_disabled(self) -> None:
        policy = ToolPolicy(allowed=True, redact_secrets=False)
        meta = MetaMCPServer(enable_cache=False)
        cfg = _server(tools={"get_token": policy})
        raw = "token: sk-abc1234567890abcdef12345"
        client = FakeMCPClient(responses={"get_token": raw})
        meta.add_server(cfg, client)
        result = asyncio.run(meta.call_tool(_call("get_token")))
        assert result.content == raw


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestBudget:
    def test_budget_exceeded_returns_error_result(self) -> None:
        policy = ToolPolicy(allowed=True, max_calls_per_run=2)
        meta = MetaMCPServer(enable_cache=False)
        cfg = _server(tools={"tool": policy})
        meta.add_server(cfg, FakeMCPClient(responses={"tool": "ok"}))

        asyncio.run(meta.call_tool(_call("tool")))
        asyncio.run(meta.call_tool(_call("tool")))
        result = asyncio.run(meta.call_tool(_call("tool")))
        assert not result.ok
        assert "limit" in result.error.lower()

    def test_budget_snapshot_counts_calls(self) -> None:
        policy = _allowed(max_calls=10)
        meta = MetaMCPServer(enable_cache=False)
        cfg = _server(tools={"tool": policy})
        meta.add_server(cfg, FakeMCPClient(responses={"tool": "v"}))
        asyncio.run(meta.call_tool(_call("tool")))
        asyncio.run(meta.call_tool(_call("tool")))
        snap = meta.budget_snapshot()
        assert snap.calls_by_tool.get("tool") == 2

    def test_reset_budget(self) -> None:
        policy = ToolPolicy(allowed=True, max_calls_per_run=1)
        meta = MetaMCPServer(enable_cache=False)
        cfg = _server(tools={"tool": policy})
        meta.add_server(cfg, FakeMCPClient(responses={"tool": "v"}))
        asyncio.run(meta.call_tool(_call("tool")))
        meta.reset_budget()
        result = asyncio.run(meta.call_tool(_call("tool")))  # should succeed again
        assert result.ok


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestCache:
    def test_second_call_is_cached(self) -> None:
        policy = _allowed()
        meta = MetaMCPServer(cache_ttl=60.0)
        cfg = _server(tools={"tool": policy})
        client = FakeMCPClient(responses={"tool": "v"})
        meta.add_server(cfg, client)

        asyncio.run(meta.call_tool(_call("tool", {"x": 1})))
        result2 = asyncio.run(meta.call_tool(_call("tool", {"x": 1})))
        assert result2.cached is True
        assert len(client.calls_made) == 1  # upstream called only once

    def test_different_args_not_cached(self) -> None:
        policy = _allowed()
        meta = MetaMCPServer(cache_ttl=60.0)
        cfg = _server(tools={"tool": policy})
        client = FakeMCPClient(responses={"tool": "v"})
        meta.add_server(cfg, client)

        asyncio.run(meta.call_tool(_call("tool", {"x": 1})))
        asyncio.run(meta.call_tool(_call("tool", {"x": 2})))
        assert len(client.calls_made) == 2

    def test_cache_disabled(self) -> None:
        policy = _allowed()
        meta = MetaMCPServer(enable_cache=False)
        cfg = _server(tools={"tool": policy})
        client = FakeMCPClient(responses={"tool": "v"})
        meta.add_server(cfg, client)

        asyncio.run(meta.call_tool(_call("tool")))
        asyncio.run(meta.call_tool(_call("tool")))
        assert len(client.calls_made) == 2

    def test_cache_stats(self) -> None:
        policy = _allowed()
        meta = MetaMCPServer(cache_ttl=60.0)
        cfg = _server(tools={"tool": policy})
        meta.add_server(cfg, FakeMCPClient(responses={"tool": "v"}))
        asyncio.run(meta.call_tool(_call("tool")))
        asyncio.run(meta.call_tool(_call("tool")))
        stats = meta.cache_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_open_after_threshold_failures(self) -> None:
        policy = _allowed()
        meta = MetaMCPServer(enable_cache=False)
        cfg = _server(tools={"tool": policy}, cb_threshold=2)
        client = FakeMCPClient(error_on={"tool"})
        meta.add_server(cfg, client)

        asyncio.run(meta.call_tool(_call("tool")))
        asyncio.run(meta.call_tool(_call("tool")))

        states = meta.circuit_breaker_states()
        assert states.get("fs") == "open"

    def test_open_circuit_returns_error_result(self) -> None:
        policy = _allowed()
        meta = MetaMCPServer(enable_cache=False)
        cfg = _server(tools={"tool": policy}, cb_threshold=1)
        client = FakeMCPClient(error_on={"tool"})
        meta.add_server(cfg, client)

        asyncio.run(meta.call_tool(_call("tool")))  # trips breaker
        result = asyncio.run(meta.call_tool(_call("tool")))  # OPEN → error result
        assert not result.ok
        assert "OPEN" in result.error or "circuit" in result.error.lower()


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------


class TestEventBus:
    def test_tool_call_event_emitted(self) -> None:
        from forge_core.types import RunEvent, RunEventKind

        published: list[RunEvent] = []

        class _Bus:
            async def publish(self, event: RunEvent) -> None:
                published.append(event)

        policy = _allowed()
        meta = MetaMCPServer(enable_cache=False)
        meta.set_event_bus(_Bus())
        cfg = _server(tools={"tool": policy})
        meta.add_server(cfg, FakeMCPClient(responses={"tool": "v"}))
        asyncio.run(meta.call_tool(_call("tool", run_id="run-123")))

        assert len(published) == 1
        assert published[0].kind == RunEventKind.TOOL_CALL
        assert published[0].tool_name == "tool"
        assert published[0].run_id == "run-123"

    def test_no_event_on_cache_hit(self) -> None:
        from forge_core.types import RunEvent

        published: list[RunEvent] = []

        class _Bus:
            async def publish(self, event: RunEvent) -> None:
                published.append(event)

        policy = _allowed()
        meta = MetaMCPServer(cache_ttl=60.0)
        meta.set_event_bus(_Bus())
        cfg = _server(tools={"tool": policy})
        meta.add_server(cfg, FakeMCPClient(responses={"tool": "v"}))

        asyncio.run(meta.call_tool(_call("tool")))   # miss → event
        asyncio.run(meta.call_tool(_call("tool")))   # hit  → no event

        assert len(published) == 1

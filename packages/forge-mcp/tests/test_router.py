"""Tests for forge_mcp.router — ToolRouter."""

from __future__ import annotations

import pytest

from forge_mcp.router import RoutingError, ToolRouter
from forge_mcp.types import ServerConfig, ToolPolicy


def _server(name: str, tools: list[str] | None = None) -> ServerConfig:
    t = {t: ToolPolicy(allowed=True) for t in (tools or [])}
    return ServerConfig(name=name, tools=t)


class TestToolRouter:
    def test_resolve_registered_tool(self) -> None:
        router = ToolRouter()
        router.register_server(_server("fs", ["read_file"]))
        cfg = router.resolve("read_file")
        assert cfg.name == "fs"

    def test_resolve_missing_raises(self) -> None:
        router = ToolRouter()
        with pytest.raises(RoutingError):
            router.resolve("nonexistent")

    def test_all_tools_after_register(self) -> None:
        router = ToolRouter()
        router.register_server(_server("fs", ["read_file", "write_file"]))
        assert set(router.all_tools()) == {"read_file", "write_file"}

    def test_unregister_removes_tools(self) -> None:
        router = ToolRouter()
        router.register_server(_server("fs", ["read_file"]))
        result = router.unregister_server("fs")
        assert result is True
        with pytest.raises(RoutingError):
            router.resolve("read_file")

    def test_unregister_nonexistent_returns_false(self) -> None:
        router = ToolRouter()
        assert router.unregister_server("ghost") is False

    def test_last_registration_wins_for_same_tool(self) -> None:
        router = ToolRouter()
        router.register_server(_server("server-a", ["shared_tool"]))
        router.register_server(_server("server-b", ["shared_tool"]))
        cfg = router.resolve("shared_tool")
        assert cfg.name == "server-b"

    def test_multi_server_routing(self) -> None:
        router = ToolRouter()
        router.register_server(_server("fs", ["read_file"]))
        router.register_server(_server("github", ["search_code"]))
        assert router.resolve("read_file").name == "fs"
        assert router.resolve("search_code").name == "github"

    def test_tool_count(self) -> None:
        router = ToolRouter()
        router.register_server(_server("fs", ["read_file", "write_file"]))
        router.register_server(_server("github", ["search_code"]))
        assert router.tool_count() == 3

    def test_all_servers(self) -> None:
        router = ToolRouter()
        router.register_server(_server("fs"))
        router.register_server(_server("github"))
        assert set(router.all_servers()) == {"fs", "github"}

    def test_server_for_tool(self) -> None:
        router = ToolRouter()
        router.register_server(_server("fs", ["read_file"]))
        assert router.server_for_tool("read_file") == "fs"
        assert router.server_for_tool("unknown") is None

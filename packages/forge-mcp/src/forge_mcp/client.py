"""forge_mcp.client — MCPClientProtocol and test helpers.

The protocol defines the minimum surface forge-mcp needs from any upstream
MCP server transport. Real implementations (StdioMCPClient backed by the
`mcp` Python SDK, SSEMCPClient, etc.) satisfy it structurally. Tests use
FakeMCPClient so no subprocess or network is required.

Real transport implementation is deferred (DT-1): StdioMCPClient requires
spawning a subprocess and speaking the MCP stdio wire protocol, which
mandates the optional `mcp>=1.0` extra and a running MCP server binary.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MCPClientProtocol(Protocol):
    """Minimum interface for an upstream MCP server connection.

    Every transport adapter must implement these three methods.
    """

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return metadata for all tools exposed by this server."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke *name* with *arguments* and return the raw result."""
        ...

    async def close(self) -> None:
        """Tear down the connection gracefully."""
        ...


class FakeMCPClient:
    """In-process fake for unit tests — no subprocess, no network.

    Usage::

        client = FakeMCPClient(
            tools=[{"name": "read_file", "description": "Read a file"}],
            responses={"read_file": "file contents here"},
        )

    Responses can be callables: ``responses={"echo": lambda args: args["text"]}``.
    """

    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        responses: dict[str, Any] | None = None,
        error_on: set[str] | None = None,
    ) -> None:
        self._tools = tools or []
        self._responses: dict[str, Any] = responses or {}
        self._error_on: set[str] = error_on or set()
        self.calls_made: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls_made.append((name, arguments))
        if name in self._error_on:
            raise RuntimeError(f"FakeMCPClient: configured to fail on '{name}'")
        if name not in self._responses:
            raise KeyError(f"FakeMCPClient: no response configured for tool '{name}'")
        resp = self._responses[name]
        if callable(resp):
            return resp(arguments)
        return resp

    async def close(self) -> None:
        pass


class StdioMCPClient:
    """Skeleton for the real stdio transport (requires mcp>=1.0 extra).

    Full implementation is deferred (DT-1). This class satisfies the
    protocol interface so callers can type-check against it, but calling
    any method before ``connect()`` raises NotImplementedError.
    """

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        self._command = command
        self._env = env or {}
        self._connected = False

    async def connect(self) -> None:
        try:
            import mcp  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "StdioMCPClient requires the 'mcp' extra: pip install forge-mcp[sdk]"
            ) from exc
        # Real connection logic deferred — see DT-1 in DEVLOG.md
        raise NotImplementedError("StdioMCPClient.connect() is not yet implemented (DT-1)")

    async def list_tools(self) -> list[dict[str, Any]]:
        raise NotImplementedError("Connect first via StdioMCPClient.connect()")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError("Connect first via StdioMCPClient.connect()")

    async def close(self) -> None:
        pass

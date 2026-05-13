"""forge_mcp.server — MetaMCPServer: the central governance and routing engine.

Layer order for every tool call:
  1. Route      — find the upstream server for this tool
  2. Security   — deny-by-default policy + caller allowlist
  3. Budget     — per-run call-count ceiling
  4. Cache      — return cached result if available (skip 5–7)
  5. Breaker    — circuit breaker guard (OPEN → fail fast)
  6. Upstream   — delegate to MCPClientProtocol implementation
  7. Redaction  — strip secrets from raw response
  8. Post       — record budget, populate cache, emit TOOL_CALL event
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from forge_core.circuit_breaker import CircuitBreaker, CircuitOpenError
from forge_core.types import RunEvent, RunEventKind
from forge_mcp.budget import BudgetExceededError, ToolBudget
from forge_mcp.cache import ToolCallCache
from forge_mcp.client import MCPClientProtocol
from forge_mcp.router import RoutingError, ToolRouter
from forge_mcp.security import get_policy, is_allowed, redact_value
from forge_mcp.types import BudgetSnapshot, ServerConfig, ToolCall, ToolResult

logger = structlog.get_logger(__name__)


class ToolDeniedError(PermissionError):
    """Raised when a tool call is blocked by the deny-by-default policy."""


class MetaMCPServer:
    """forge-mcp meta-server: routes, secures, budgets, and caches MCP tool calls.

    This class is the governance and routing layer. It does NOT implement
    the MCP wire protocol — that is the responsibility of MCPClientProtocol
    implementations (FakeMCPClient for tests, StdioMCPClient for production).

    Thread/async safety: each layer uses its own internal lock; the server
    itself is stateless between calls (all state lives in ToolBudget, ToolCallCache,
    and CircuitBreaker, each of which is independently thread-safe).
    """

    def __init__(
        self,
        *,
        cache_ttl: float = 300.0,
        enable_cache: bool = True,
    ) -> None:
        self._router = ToolRouter()
        self._cache: ToolCallCache | None = (
            ToolCallCache(default_ttl=cache_ttl) if enable_cache else None
        )
        self._budget = ToolBudget()
        self._clients: dict[str, MCPClientProtocol] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._bus: Any = None

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def add_server(self, server: ServerConfig, client: MCPClientProtocol) -> None:
        """Register an upstream MCP server with its transport client."""
        self._router.register_server(server)
        self._clients[server.name] = client
        self._breakers[server.name] = CircuitBreaker(
            name=f"mcp:{server.name}",
            failure_threshold=server.circuit_breaker_threshold,
            recovery_timeout_seconds=server.circuit_breaker_timeout,
        )
        logger.info(
            "mcp.server.added",
            name=server.name,
            tools=list(server.tools.keys()),
            transport=server.transport.value,
        )

    def remove_server(self, server_name: str) -> bool:
        """Deregister a server and all its tool routes. Returns True if found."""
        if server_name not in self._clients:
            return False
        self._router.unregister_server(server_name)
        del self._clients[server_name]
        del self._breakers[server_name]
        logger.info("mcp.server.removed", name=server_name)
        return True

    def set_event_bus(self, bus: Any) -> None:
        """Inject an EventBus for TOOL_CALL event emission."""
        self._bus = bus

    # ------------------------------------------------------------------
    # Tool invocation
    # ------------------------------------------------------------------

    async def call_tool(self, call: ToolCall) -> ToolResult:
        """Invoke a tool through all governance layers.

        Never raises for recoverable errors (routing miss, denied, budget,
        circuit open, upstream failure) — those are encoded in ToolResult.error.
        Raises ToolDeniedError only for explicit policy violations so callers
        can distinguish "server said no" from "something went wrong".
        """
        t0 = time.monotonic()

        # 1. Route
        try:
            server = self._router.resolve(call.tool_name)
        except RoutingError as exc:
            if self._clients:
                # Servers are registered but this tool is not declared in any of them.
                # Treat as a security denial, not a routing error.
                raise ToolDeniedError(
                    f"Tool '{call.tool_name}' is not declared in any registered server. "
                    "Add it with allowed=true to grant access."
                ) from exc
            return ToolResult(tool_name=call.tool_name, error=str(exc))

        # 2. Security (deny-by-default)
        if not is_allowed(server, call.tool_name, call.caller_id):
            raise ToolDeniedError(
                f"Tool '{call.tool_name}' on server '{server.name}' is not allowed. "
                "Set allowed=true in the server config to grant access."
            )

        policy = get_policy(server, call.tool_name)
        assert policy is not None  # guaranteed by is_allowed

        # 3. Budget
        try:
            self._budget.check(call.tool_name, policy)
        except BudgetExceededError as exc:
            return ToolResult(tool_name=call.tool_name, server_name=server.name, error=str(exc))

        # 4. Cache lookup
        if self._cache is not None:
            cached_result, hit = self._cache.get(call.tool_name, call.arguments)
            if hit:
                logger.debug("mcp.cache.hit", tool=call.tool_name)
                return ToolResult(
                    tool_name=call.tool_name,
                    server_name=server.name,
                    content=cached_result,
                    cached=True,
                    duration_ms=(time.monotonic() - t0) * 1000,
                )

        # 5–6. Circuit breaker + upstream call
        client = self._clients[server.name]
        breaker = self._breakers[server.name]

        try:
            raw = await breaker.call(client.call_tool, call.tool_name, call.arguments)
        except CircuitOpenError as exc:
            logger.warning("mcp.circuit.open", tool=call.tool_name, server=server.name)
            return ToolResult(
                tool_name=call.tool_name, server_name=server.name, error=str(exc)
            )
        except Exception as exc:
            logger.error(
                "mcp.upstream.error",
                tool=call.tool_name,
                server=server.name,
                error=repr(exc),
            )
            return ToolResult(
                tool_name=call.tool_name, server_name=server.name, error=str(exc)
            )

        # 7. Secret redaction
        content = redact_value(raw, policy)

        # 8. Post-call bookkeeping
        self._budget.record(call.tool_name)

        ttl = policy.cache_ttl_seconds
        if self._cache is not None:
            self._cache.put(call.tool_name, call.arguments, content, ttl=ttl)

        duration_ms = (time.monotonic() - t0) * 1000

        if self._bus is not None:
            await self._bus.publish(
                RunEvent(
                    kind=RunEventKind.TOOL_CALL,
                    run_id=call.run_id,
                    tool_name=call.tool_name,
                    data={
                        "server": server.name,
                        "cached": False,
                        "duration_ms": round(duration_ms, 1),
                    },
                )
            )

        logger.info(
            "mcp.tool.called",
            tool=call.tool_name,
            server=server.name,
            duration_ms=round(duration_ms, 1),
        )

        return ToolResult(
            tool_name=call.tool_name,
            server_name=server.name,
            content=content,
            duration_ms=duration_ms,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_tools(self) -> list[str]:
        """Return the names of all registered tools across all servers."""
        return self._router.all_tools()

    def list_servers(self) -> list[str]:
        return self._router.all_servers()

    def budget_snapshot(self) -> BudgetSnapshot:
        return self._budget.snapshot()

    def reset_budget(self) -> None:
        """Reset per-run counters (call at the start of each new run)."""
        self._budget.reset()

    def cache_stats(self) -> dict[str, int]:
        return self._cache.stats() if self._cache else {}

    def invalidate_cache(self, tool_name: str | None = None) -> int:
        if self._cache is None:
            return 0
        return self._cache.invalidate(tool_name)

    def circuit_breaker_states(self) -> dict[str, str]:
        """Return {server_name: circuit_state} for all registered servers."""
        return {name: b.state.value for name, b in self._breakers.items()}

"""forge_mcp.router — Tool-to-server routing table.

The router maintains a bidirectional index:
  server_name → ServerConfig
  tool_name   → server_name

When multiple servers expose a tool with the same name, the last registered
server wins (deterministic, easy to reason about in YAML order). A warning
is logged at registration time so operators notice the override.
"""

from __future__ import annotations

import structlog

from forge_mcp.types import ServerConfig

logger = structlog.get_logger(__name__)


class RoutingError(KeyError):
    """Raised when no server is registered for the requested tool."""


class ToolRouter:
    """Maps tool names to upstream ServerConfig objects."""

    def __init__(self) -> None:
        self._servers: dict[str, ServerConfig] = {}
        self._tool_map: dict[str, str] = {}  # tool_name → server_name

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_server(self, server: ServerConfig) -> None:
        """Add *server* and map all its declared tools to it."""
        if server.name in self._servers:
            logger.warning("mcp.router.server.overwrite", name=server.name)
        self._servers[server.name] = server
        for tool_name in server.tools:
            if tool_name in self._tool_map and self._tool_map[tool_name] != server.name:
                logger.warning(
                    "mcp.router.tool.override",
                    tool=tool_name,
                    old_server=self._tool_map[tool_name],
                    new_server=server.name,
                )
            self._tool_map[tool_name] = server.name

    def unregister_server(self, server_name: str) -> bool:
        """Remove *server_name* and all its tool mappings. Returns True if found."""
        if server_name not in self._servers:
            return False
        del self._servers[server_name]
        self._tool_map = {t: s for t, s in self._tool_map.items() if s != server_name}
        return True

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def resolve(self, tool_name: str) -> ServerConfig:
        """Return the ServerConfig responsible for *tool_name*.

        Raises RoutingError if no server is registered for the tool.
        """
        server_name = self._tool_map.get(tool_name)
        if server_name is None:
            raise RoutingError(f"No server registered for tool '{tool_name}'")
        server = self._servers.get(server_name)
        if server is None:
            raise RoutingError(
                f"Routing table inconsistency: tool '{tool_name}' maps to "
                f"'{server_name}' but that server is not registered"
            )
        return server

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def all_tools(self) -> list[str]:
        return sorted(self._tool_map.keys())

    def all_servers(self) -> list[str]:
        return sorted(self._servers.keys())

    def tool_count(self) -> int:
        return len(self._tool_map)

    def server_for_tool(self, tool_name: str) -> str | None:
        return self._tool_map.get(tool_name)

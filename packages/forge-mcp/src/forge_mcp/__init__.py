"""forge_mcp — MCP Meta-Orchestrator for Forge.

Routes tool calls across N upstream MCP servers with deny-by-default security,
per-run budget accounting, exact-match TTL caching, and circuit breakers.
"""

from forge_mcp.budget import BudgetExceededError, ToolBudget
from forge_mcp.cache import ToolCallCache
from forge_mcp.client import FakeMCPClient, MCPClientProtocol, StdioMCPClient
from forge_mcp.loader import dump_yaml, load_yaml
from forge_mcp.router import RoutingError, ToolRouter
from forge_mcp.security import is_allowed, redact_secrets, redact_value
from forge_mcp.server import MetaMCPServer, ToolDeniedError
from forge_mcp.types import (
    BudgetSnapshot,
    ServerConfig,
    ToolCall,
    ToolPolicy,
    ToolResult,
    ToolTransport,
)

__version__ = "0.1.0"

__all__ = [
    "MetaMCPServer",
    "ToolDeniedError",
    "ServerConfig",
    "ToolPolicy",
    "ToolCall",
    "ToolResult",
    "ToolTransport",
    "BudgetSnapshot",
    "ToolBudget",
    "BudgetExceededError",
    "ToolCallCache",
    "ToolRouter",
    "RoutingError",
    "MCPClientProtocol",
    "FakeMCPClient",
    "StdioMCPClient",
    "is_allowed",
    "redact_secrets",
    "redact_value",
    "load_yaml",
    "dump_yaml",
]

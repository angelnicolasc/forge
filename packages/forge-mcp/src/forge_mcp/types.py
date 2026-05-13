"""forge_mcp.types — Core type system for the MCP Meta-Orchestrator."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ToolTransport(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"


class ToolPolicy(BaseModel):
    """Security and budget policy for a single tool.

    The default is deny-by-default: a tool with no explicit policy or
    ``allowed=False`` is never executed.
    """

    allowed: bool = False
    max_calls_per_run: int = 10
    max_tokens_per_call: int = 8192
    redact_secrets: bool = True
    allowed_callers: list[str] = Field(
        default_factory=list,
        description="Caller IDs permitted to invoke this tool. Empty = any caller.",
    )
    cache_ttl_seconds: float | None = None


class ServerConfig(BaseModel):
    """Configuration for a single upstream MCP server."""

    name: str
    transport: ToolTransport = ToolTransport.STDIO
    command: list[str] = Field(
        default_factory=list,
        description="Process command for stdio transport.",
    )
    url: str | None = Field(
        default=None,
        description="Base URL for sse/http transport.",
    )
    env: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, ToolPolicy] = Field(
        default_factory=dict,
        description="Per-tool policies. Tools absent from this map are denied.",
    )
    enabled: bool = True
    timeout_seconds: float = 30.0
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout: float = 30.0


class ToolCall(BaseModel):
    """A single tool invocation request directed at the meta-server."""

    tool_name: str
    server_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    caller_id: str = ""
    run_id: str = ""


class ToolResult(BaseModel):
    """Result returned by the meta-server after processing a ToolCall."""

    tool_name: str
    server_name: str = ""
    content: Any = None
    error: str | None = None
    cached: bool = False
    tokens_used: int = 0
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


class BudgetSnapshot(BaseModel):
    """Per-run tool usage summary emitted by ToolBudget.snapshot()."""

    calls_by_tool: dict[str, int] = Field(default_factory=dict)
    tokens_by_tool: dict[str, int] = Field(default_factory=dict)
    total_calls: int = 0
    total_tokens: int = 0

# forge-mcp

MCP Meta-Orchestrator for Forge — routes, secures, budgets, and caches tool calls across N upstream MCP servers.

## Features

- **Deny-by-default security**: tools must be explicitly `allowed: true` per server config
- **Caller allowlists**: restrict tool access to specific agent/caller IDs
- **Secret redaction**: responses are scanned and sanitized before reaching the agent context
- **Per-run budgets**: configurable `max_calls_per_run` per tool; `BudgetExceededError` on breach
- **Exact-match TTL cache**: Anthropic prompt-caching semantics — identical input → cache hit
- **Circuit breakers**: per-server, reusing `forge-core.CircuitBreaker` (5 failures → OPEN)
- **YAML config**: human-friendly server and policy authoring
- **CLI**: `forge mcp list-tools`, `forge mcp call`, `forge mcp status`

## Quick start

```python
from forge_mcp import MetaMCPServer, ServerConfig, ToolCall, ToolPolicy, FakeMCPClient

server = MetaMCPServer()
cfg = ServerConfig(
    name="filesystem",
    transport="stdio",
    command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    tools={"read_file": ToolPolicy(allowed=True, max_calls_per_run=20)},
)
server.add_server(cfg, FakeMCPClient(responses={"read_file": "file contents"}))

result = await server.call_tool(ToolCall(tool_name="read_file", arguments={"path": "/tmp/x.txt"}))
print(result.content)
```

## YAML config

```yaml
cache_ttl: 300
servers:
  - name: filesystem
    transport: stdio
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    tools:
      read_file:
        allowed: true
        max_calls_per_run: 20
      write_file:
        allowed: true
        max_calls_per_run: 5
```

## Part of Forge

This package is part of [Forge](https://github.com/angelnicolasc/forge) — the universal AI agent harness.

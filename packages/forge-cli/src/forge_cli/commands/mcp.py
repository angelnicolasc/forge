"""forge mcp — Manage and invoke MCP meta-server tool calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from forge_observe.exporters.console import FORGE_GRAY, FORGE_ORANGE, FORGE_TEAL

mcp_app = typer.Typer(help="Manage MCP tool routing, budgets, and cache.")
console = Console()


def _build_server(config_file: Path) -> Any:
    """Load YAML config and return a configured MetaMCPServer (no real clients)."""
    from forge_mcp.loader import load_yaml
    from forge_mcp.server import MetaMCPServer

    servers, options = load_yaml(config_file)
    cache_ttl = float(options.get("cache_ttl", 300.0))
    meta = MetaMCPServer(cache_ttl=cache_ttl)
    return meta, servers


@mcp_app.command("list-tools")
def mcp_list_tools(
    config: Path = typer.Argument(..., help="forge-mcp YAML config file.", metavar="CONFIG"),
) -> None:
    """[bold]List[/] all tools and their policies from a forge-mcp config."""
    if not config.exists():
        console.print(f"[red]✗ File not found:[/] {config}")
        raise typer.Exit(1)

    from forge_mcp.loader import load_yaml

    try:
        servers, _ = load_yaml(config)
    except (ValueError, TypeError) as exc:
        console.print(f"[red]✗ Config error:[/]\n{exc}")
        raise typer.Exit(1)

    table = Table(title="Registered MCP Tools", border_style=FORGE_ORANGE)
    table.add_column("Server", style="bold white")
    table.add_column("Tool", style=FORGE_TEAL)
    table.add_column("Allowed", justify="center")
    table.add_column("Max Calls", justify="right", style=FORGE_GRAY)
    table.add_column("Redact Secrets", justify="center", style=FORGE_GRAY)

    for srv in servers:
        for tool_name, policy in srv.tools.items():
            allowed_str = f"[{FORGE_TEAL}]✓[/]" if policy.allowed else "[red]✗[/]"
            redact_str = "✓" if policy.redact_secrets else "—"
            table.add_row(
                srv.name,
                tool_name,
                allowed_str,
                str(policy.max_calls_per_run),
                redact_str,
            )
        if not srv.tools:
            table.add_row(srv.name, f"[dim](no tools — all denied by default)[/]", "—", "—", "—")

    console.print(table)

    total_tools = sum(len(s.tools) for s in servers)
    allowed_tools = sum(
        sum(1 for p in s.tools.values() if p.allowed) for s in servers
    )
    console.print(
        f"\n  [{FORGE_GRAY}]{len(servers)} server(s), "
        f"{total_tools} tool(s) declared, "
        f"{allowed_tools} allowed.[/]"
    )


@mcp_app.command("call")
def mcp_call(
    tool: str = typer.Argument(..., help="Tool name to invoke.", metavar="TOOL"),
    config: Path = typer.Option(
        Path("mcp.yaml"), "--config", "-c", help="forge-mcp config file."
    ),
    args: str = typer.Option(
        "{}", "--args", "-a", help="Tool arguments as a JSON string."
    ),
    run_id: str = typer.Option("", "--run-id", help="Run ID for event tracking."),
    caller_id: str = typer.Option("", "--caller-id", help="Caller identity for allowlist checks."),
) -> None:
    """[bold]Call[/] a tool through the MCP meta-server (requires configured clients)."""
    if not config.exists():
        console.print(f"[red]✗ Config not found:[/] {config}")
        raise typer.Exit(1)

    try:
        arguments: dict[str, Any] = json.loads(args)
    except json.JSONDecodeError as exc:
        console.print(f"[red]✗ Invalid JSON args:[/] {exc}")
        raise typer.Exit(1)

    result = asyncio.run(_do_call(config, tool, arguments, run_id, caller_id))

    if result.error:
        console.print(f"  [red]✗ Error:[/] {result.error}")
        raise typer.Exit(1)

    icon = f"[dim](cached)[/]" if result.cached else ""
    console.print(
        f"  [{FORGE_TEAL}]✓[/] {tool} via [{FORGE_GRAY}]{result.server_name}[/] "
        f"[dim]({result.duration_ms:.0f}ms)[/] {icon}"
    )
    console.print_json(json.dumps(result.content, default=str))


async def _do_call(
    config: Path,
    tool_name: str,
    arguments: dict[str, Any],
    run_id: str,
    caller_id: str,
) -> Any:
    from forge_mcp.client import StdioMCPClient
    from forge_mcp.loader import load_yaml
    from forge_mcp.server import MetaMCPServer, ToolDeniedError
    from forge_mcp.types import ToolCall

    servers, options = load_yaml(config)
    cache_ttl = float(options.get("cache_ttl", 300.0))
    meta = MetaMCPServer(cache_ttl=cache_ttl)

    for srv in servers:
        if srv.enabled:
            client = StdioMCPClient(command=srv.command, env=srv.env)
            meta.add_server(srv, client)

    call = ToolCall(
        tool_name=tool_name,
        arguments=arguments,
        run_id=run_id,
        caller_id=caller_id,
    )
    try:
        return await meta.call_tool(call)
    except ToolDeniedError as exc:
        from forge_mcp.types import ToolResult
        return ToolResult(tool_name=tool_name, error=str(exc))


@mcp_app.command("status")
def mcp_status(
    config: Path = typer.Argument(..., help="forge-mcp YAML config file.", metavar="CONFIG"),
) -> None:
    """[bold]Show[/] server status, transport, and tool counts from a config file."""
    if not config.exists():
        console.print(f"[red]✗ File not found:[/] {config}")
        raise typer.Exit(1)

    from forge_mcp.loader import load_yaml

    try:
        servers, options = load_yaml(config)
    except (ValueError, TypeError) as exc:
        console.print(f"[red]✗ Config error:[/]\n{exc}")
        raise typer.Exit(1)

    table = Table(title="MCP Server Status", border_style=FORGE_ORANGE)
    table.add_column("Server", style="bold white")
    table.add_column("Transport", style=FORGE_TEAL)
    table.add_column("Enabled", justify="center")
    table.add_column("Tools Declared", justify="right", style=FORGE_GRAY)
    table.add_column("Tools Allowed", justify="right", style=FORGE_TEAL)
    table.add_column("Command / URL", style="dim white")

    for srv in servers:
        enabled_str = f"[{FORGE_TEAL}]✓[/]" if srv.enabled else "[red]✗[/]"
        allowed_count = sum(1 for p in srv.tools.values() if p.allowed)
        endpoint = " ".join(srv.command) if srv.command else (srv.url or "—")
        table.add_row(
            srv.name,
            srv.transport.value,
            enabled_str,
            str(len(srv.tools)),
            str(allowed_count),
            endpoint[:60],
        )

    console.print(table)

    if options:
        console.print(f"\n  [{FORGE_GRAY}]Options: {options}[/]")

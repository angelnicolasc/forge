"""forge run — Execute a previously wrapped flow from a config file or module path."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path  # noqa: TC003  (needed at runtime: Typer evaluates signature annotations)
from typing import Any

import typer
from rich.console import Console

from forge_observe.exporters.console import print_forge_banner, print_run_result

run_app = typer.Typer(invoke_without_command=True)
console = Console()


@run_app.callback(invoke_without_command=True)
def run(
    source: str = typer.Argument(..., help="Agent flow file or module path.", metavar="SOURCE"),
    input_json: str | None = typer.Option(None, "--input", "-i", help="JSON input string."),
    input_file: Path | None = typer.Option(None, "--input-file", help="JSON input file."),
    budget: float | None = typer.Option(None, "--budget", help="Cost ceiling in USD."),
    evolution: bool = typer.Option(False, "--evolution", "-e", help="Enable evolution."),
    memory: bool = typer.Option(True, "--memory/--no-memory", help="Enable memory."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full output."),
    stream: bool = typer.Option(False, "--stream", "-s", help="Stream events to stdout."),
) -> None:
    """Execute a wrapped agent flow with full Forge instrumentation."""
    print_forge_banner()

    run_input: dict[str, Any] = {}
    if input_file and input_file.exists():
        run_input = json.loads(input_file.read_text())
    elif input_json:
        try:
            run_input = json.loads(input_json)
        except json.JSONDecodeError as exc:
            console.print(f"[red]✗ Invalid JSON: {exc}[/]")
            raise typer.Exit(1) from exc

    exit_code = asyncio.run(_do_run(source, run_input, budget, evolution, memory, verbose, stream))
    raise typer.Exit(exit_code)


async def _do_run(
    source: str,
    run_input: dict[str, Any],
    budget_usd: float | None,
    evolution: bool,
    memory_enabled: bool,
    verbose: bool,
    stream: bool,
) -> int:
    from decimal import Decimal

    from forge_core.config import ForgeConfig
    from forge_core.harness import MetaOrchestrator
    from forge_core.types import RunConfig, TaskEnvelope

    config = ForgeConfig()
    orchestrator = MetaOrchestrator(config=config)

    try:
        await orchestrator.load(source)
    except Exception as exc:
        console.print(f"[red]✗ Load failed:[/] {exc}")
        return 1

    run_config = RunConfig(
        enable_evolution=evolution,
        evolution_mode="auto" if evolution else "off",
        enable_memory=memory_enabled,
        cost_ceiling=Decimal(str(budget_usd)) if budget_usd else Decimal("100.00"),
    )
    envelope = TaskEnvelope(input=run_input, config=run_config)

    if stream:
        async for event in orchestrator.stream(envelope):
            console.print(f"  [dim]{event.kind}[/] {event.data}")
        return 0

    result = await orchestrator.run(envelope)
    print_run_result(result, verbose=verbose)
    return 0 if result.status == "completed" else 1

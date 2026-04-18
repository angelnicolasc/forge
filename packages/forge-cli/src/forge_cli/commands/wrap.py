"""forge wrap — The viral front door of Forge.

Wraps any multi-agent flow in a single command and instruments it
with full observability, cost tracking, and optional evolution.

Usage:
    forge wrap my_langgraph_flow.py
    forge wrap my_crew.py --evolution
    forge wrap my_flow.py --input '{"query": "summarize this"}' --verbose

What happens:
    1. Auto-detects the framework (LangGraph / CrewAI / AutoGen / generic)
    2. Loads and validates the flow
    3. Runs it with full OpenTelemetry instrumentation
    4. Displays a beautiful Rich dashboard with cost, latency, and topology
    5. Optionally stores results in Living Collaborative Memory
    6. If --evolution is set, feeds results into the self-evolution loop
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from forge_observe.exporters.console import (
    FORGE_ORANGE,
    FORGE_TEAL,
    print_forge_banner,
    print_run_result,
)

wrap_app = typer.Typer(invoke_without_command=True)
console = Console()


@wrap_app.callback(invoke_without_command=True)
def wrap(
    source: str = typer.Argument(
        ...,
        help="Path to your agent flow file (e.g., my_flow.py) or module (e.g., myapp.flow).",
        metavar="SOURCE",
    ),
    input_json: str | None = typer.Option(
        None,
        "--input",
        "-i",
        help='JSON input for the run (e.g., \'{"query": "..."}\').',
    ),
    input_file: Path | None = typer.Option(
        None,
        "--input-file",
        help="Path to a JSON file with the run input.",
    ),
    evolution: bool = typer.Option(
        False,
        "--evolution",
        "-e",
        help="Enable the self-evolution loop after the run.",
    ),
    memory: bool = typer.Option(
        True,
        "--memory/--no-memory",
        help="Store run results in Living Collaborative Memory.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show full output in addition to the dashboard.",
    ),
    budget: float | None = typer.Option(
        None,
        "--budget",
        help="Cost ceiling in USD (e.g., 1.50). Stops run if exceeded.",
    ),
    dashboard: bool = typer.Option(
        False,
        "--dashboard",
        "-d",
        help="Start the observe API and open the web dashboard.",
    ),
) -> None:
    """[bold #FF6B35]Wrap[/] any multi-agent flow with full Forge instrumentation.

    \b
    [dim]One command. Full observability. Self-evolution ready.[/dim]

    [bold]Examples:[/bold]
      [green]forge wrap my_flow.py[/green]
      [green]forge wrap my_crew.py --input '{"topic": "AGI safety"}' --evolution[/green]
      [green]forge wrap my_flow.py --budget 0.50 --dashboard[/green]
    """
    print_forge_banner()

    # Resolve input
    run_input: dict[str, Any] = {}
    if input_file:
        try:
            run_input = json.loads(input_file.read_text())
        except Exception as exc:
            console.print(f"[red]✗ Failed to read input file: {exc}[/]")
            raise typer.Exit(1) from exc
    elif input_json:
        try:
            run_input = json.loads(input_json)
        except json.JSONDecodeError as exc:
            console.print(f"[red]✗ Invalid JSON input: {exc}[/]")
            raise typer.Exit(1) from exc

    # Run the async flow
    exit_code = asyncio.run(
        _run_wrap(
            source=source,
            run_input=run_input,
            evolution=evolution,
            memory_enabled=memory,
            verbose=verbose,
            budget_usd=budget,
            start_dashboard=dashboard,
        )
    )
    raise typer.Exit(exit_code)


async def _run_wrap(
    source: str,
    run_input: dict[str, Any],
    evolution: bool,
    memory_enabled: bool,
    verbose: bool,
    budget_usd: float | None,
    start_dashboard: bool,
) -> int:
    """Core async implementation of forge wrap."""
    from decimal import Decimal

    from forge_core.config import ForgeConfig
    from forge_core.harness import MetaOrchestrator
    from forge_core.types import RunConfig, TaskEnvelope

    config = ForgeConfig()

    # Start dashboard backend if requested
    if start_dashboard:
        from forge_observe.dashboard_backend import DashboardBackend

        backend = DashboardBackend(host=config.observe.api_host, port=config.observe.api_port)
        backend.start_background()
        console.print(
            f"  [{FORGE_TEAL}]Dashboard API:[/] {backend.url}/docs",
            f"\n  [{FORGE_TEAL}]Dashboard UI:[/] http://localhost:3000",
        )

    # Show loading spinner
    with Live(
        Panel(
            Text.assemble(
                ("  ⚡ ", FORGE_ORANGE),
                ("Detecting framework for ", "dim white"),
                (source, "bold white"),
                (" ...", "dim white"),
            ),
            border_style=FORGE_ORANGE,
        ),
        console=console,
        refresh_per_second=10,
        transient=True,
    ):
        orchestrator = MetaOrchestrator(config=config)
        t0 = time.perf_counter()
        try:
            adapter = await orchestrator.load(source)
        except Exception as exc:
            console.print(f"\n  [red]✗ Failed to load flow:[/] {exc}")
            _print_framework_hint(source)
            return 1

    detect_ms = (time.perf_counter() - t0) * 1000
    console.print(
        f"  [dim]✓ Detected:[/] [bold white]{adapter.name}[/]  [dim]({detect_ms:.0f}ms)[/]"
    )

    # Build TaskEnvelope
    run_config = RunConfig(
        enable_evolution=evolution,
        evolution_mode="auto" if evolution else "off",
        enable_memory=memory_enabled,
        cost_ceiling=Decimal(str(budget_usd)) if budget_usd else Decimal("100.00"),
    )
    envelope = TaskEnvelope(input=run_input, config=run_config)

    # Execute with live progress
    console.print(f"\n  [{FORGE_TEAL}]Running[/] [dim]task_id={envelope.task_id[:12]}...[/]\n")

    spinner_text = Text.assemble(("  ⚙ ", FORGE_ORANGE), ("Agents working...", "white"))
    run_result = None

    with Live(spinner_text, console=console, refresh_per_second=10, transient=True):
        try:
            run_result = await orchestrator.run(envelope)
        except Exception as exc:
            console.print(f"\n  [red]✗ Run failed:[/] {exc}")
            return 1

    # Print the rich dashboard
    print_run_result(run_result, verbose=verbose)

    # Ingest into memory if enabled
    if memory_enabled and run_result.output:
        await _ingest_to_memory(run_result, config)

    # Run evolution if requested
    if evolution and run_result:
        await _run_evolution(orchestrator, config)

    # Push to dashboard API if running
    if start_dashboard:
        await _push_to_api(run_result, config)

    return 0 if run_result.status == "completed" else 1


async def _ingest_to_memory(run_result: object, config: object) -> None:
    """Silently ingest run output into Living Memory."""
    try:
        from forge_memory.hybrid import HybridMemory

        memory = HybridMemory()
        content = str(getattr(run_result, "output", ""))
        if content and content != "None":
            run_id = getattr(run_result, "run_id", None)
            await memory.ingest_text(content, source_run_id=run_id, tags={"source": "forge_run"})
            console.print(f"  [{FORGE_TEAL}]✓ Stored in Living Memory[/]")
    except Exception as exc:
        console.print(f"  [dim]Memory ingest skipped: {exc}[/]")


async def _run_evolution(orchestrator: object, config: object) -> None:
    """Trigger evolution loop if enough runs available."""
    try:
        from forge_core.evolution.loop import EvolutionLoop

        history = getattr(orchestrator, "run_history", [])
        if len(history) < 2:
            console.print(
                f"  [{FORGE_TEAL}]ℹ  Evolution needs ≥ 2 runs. "
                f"Current: {len(history)}. Keep running![/]"
            )
            return

        loop = EvolutionLoop(orchestrator=orchestrator)  # type: ignore[arg-type]
        mutation = await loop.step()
        if mutation:
            console.print(f"  [{FORGE_TEAL}]⚗  Evolution proposed:[/] {mutation.description}")
    except Exception as exc:
        console.print(f"  [dim]Evolution skipped: {exc}[/]")


async def _push_to_api(run_result: object, config: object) -> None:
    """Push run result to the observe API."""
    try:
        import httpx

        from forge_core.types import RunResult

        if not isinstance(run_result, RunResult):
            return
        observe_cfg = getattr(config, "observe", None)
        if not observe_cfg:
            return
        url = f"http://{observe_cfg.api_host}:{observe_cfg.api_port}/api/v1/runs"
        async with httpx.AsyncClient() as client:
            await client.post(
                url,
                content=run_result.model_dump_json(),
                headers={"Content-Type": "application/json"},
                timeout=2.0,
            )
    except Exception:
        pass


def _print_framework_hint(source: str) -> None:
    """Print a helpful hint about which extras to install."""
    hints = {
        "langgraph": "pip install 'forge-adapters[langgraph]'",
        "crewai": "pip install 'forge-adapters[crewai]'",
        "autogen": "pip install 'forge-adapters[autogen]'",
    }
    for keyword, install_cmd in hints.items():
        if keyword in source.lower():
            console.print(f"\n  [dim]Hint: Try installing [bold]{install_cmd}[/][/]")
            return
    console.print(
        "\n  [dim]No specific adapter detected. Try: pip install 'forge-adapters[all]'[/]"
    )

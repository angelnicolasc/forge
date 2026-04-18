"""forge evolve — Trigger the self-evolution loop manually.

Subcommands:

- ``forge evolve run <source>``  — execute the evolution loop against a flow.
- ``forge evolve status``         — show recent journal entries + breaker state.
- ``forge evolve resume <source>`` — re-arm the evolution circuit breaker.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from forge_observe.exporters.console import (
    FORGE_GRAY,
    FORGE_ORANGE,
    FORGE_TEAL,
    print_evolution_proposal,
)

evolve_app = typer.Typer(help="Trigger, inspect, or resume the self-evolution loop.")
console = Console()


@evolve_app.command("run")
def evolve_run(
    source: str = typer.Argument(..., help="Agent flow file or module.", metavar="SOURCE"),
    runs: int = typer.Option(5, "--runs", "-r", help="Number of eval runs for fitness scoring."),
    mode: str = typer.Option(
        "suggest",
        "--mode",
        "-m",
        help="Evolution mode: 'suggest' (show proposals) or 'auto' (apply automatically).",
    ),
    max_mutations: int = typer.Option(3, "--max-mutations", help="Max mutations per session."),
) -> None:
    """[bold]Run[/] the self-evolution loop against a flow.

    \b
    [dim]The loop observes recent runs, proposes mutations, evaluates them,
    and commits improvements. Your agents literally get smarter over time.[/dim]

    [bold]Examples:[/bold]
      [green]forge evolve run my_flow.py --mode suggest[/green]
      [green]forge evolve run my_crew.py --mode auto --runs 10[/green]
    """
    console.print(
        f"\n  [{FORGE_TEAL}]⚗  Starting Evolution Loop[/]  (mode={mode}, eval_runs={runs})\n"
    )
    exit_code = asyncio.run(_do_evolve(source, runs, mode, max_mutations))
    raise typer.Exit(exit_code)


async def _do_evolve(source: str, runs: int, mode: str, max_mutations: int) -> int:
    from forge_core.config import ForgeConfig
    from forge_core.evolution.loop import EvolutionLoop
    from forge_core.harness import MetaOrchestrator

    config = ForgeConfig()
    orchestrator = MetaOrchestrator(config=config)

    try:
        await orchestrator.load(source)
    except Exception as exc:
        console.print(f"[red]✗ Load failed:[/] {exc}")
        return 1

    loop = EvolutionLoop(orchestrator=orchestrator, mode=mode, max_mutations=max_mutations)

    mutations_applied = 0
    for i in range(max_mutations):
        console.print(f"  [dim]Iteration {i + 1}/{max_mutations}...[/]")
        mutation = await loop.step()
        if mutation is None:
            console.print(f"  [{FORGE_TEAL}]✓ No further improvements found. System is optimal.[/]")
            break
        print_evolution_proposal(mutation)
        if mode == "auto":
            console.print(f"  [{FORGE_TEAL}]↳ Applied automatically.[/]")
            mutations_applied += 1
        else:
            apply = typer.confirm("  Apply this mutation?", default=False)
            if apply:
                mutations_applied += 1
                console.print(f"  [{FORGE_TEAL}]✓ Applied.[/]")
            else:
                console.print("  [dim]Skipped.[/]")

    console.print(
        f"\n  [{FORGE_TEAL}]⚗  Evolution complete.[/] {mutations_applied} mutation(s) applied.\n"
    )
    return 0


# ---------------------------------------------------------------------------
# ζ.15 — read-side subcommands
# ---------------------------------------------------------------------------


_DEFAULT_JOURNAL_PATH = Path.home() / ".forge" / "evolution_journal.jsonl"


def _read_journal(path: Path, limit: int) -> list[dict[str, Any]]:
    """Read the last ``limit`` JSONL entries from the journal on disk."""
    if not path.exists():
        return []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for ln in lines[-limit:]:
        try:
            entries.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return list(reversed(entries))


def _format_delta(entry: dict[str, Any]) -> str:
    improvement = entry.get("improvement")
    if improvement is None:
        return "—"
    return f"{improvement:+.3f}"


def _outcome_style(kind: str) -> str:
    mapping = {
        "applied": FORGE_TEAL,
        "rolled_back": "yellow",
        "skipped": FORGE_GRAY,
        "proposed": FORGE_GRAY,
        "evaluated": FORGE_GRAY,
        "breaker_opened": "red",
    }
    return mapping.get(kind, "white")


@evolve_app.command("status")
def evolve_status(
    journal_path: Path = typer.Option(
        _DEFAULT_JOURNAL_PATH,
        "--journal",
        help="Path to the evolution journal JSONL file.",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max entries to display."),
) -> None:
    """[bold]Show[/] recent evolution journal entries and breaker state."""
    entries = _read_journal(journal_path, limit)

    if not entries:
        console.print(
            f"  [{FORGE_GRAY}]No evolution history found at[/] {journal_path}\n"
            f"  [{FORGE_GRAY}]Run[/] [green]forge evolve run <flow>[/] "
            f"[{FORGE_GRAY}]to start.[/]"
        )
        raise typer.Exit(0)

    # Detect a breaker_opened sentinel entry (written by harness when auto-suspending).
    breaker_open = any(e.get("kind") == "breaker_opened" for e in entries)
    if breaker_open:
        last = next(e for e in entries if e.get("kind") == "breaker_opened")
        reason = last.get("reason") or "consecutive_rollbacks"
        console.print(
            Panel(
                f"[bold red]⚠  Evolution circuit breaker is OPEN[/]\n"
                f"  reason: {reason}\n"
                f"  opened_at: {last.get('timestamp', '—')}\n\n"
                f"  Resume with: [green]forge evolve resume <source>[/]",
                border_style="red",
                title="Breaker Opened",
            )
        )

    table = Table(title="Evolution Journal", border_style=FORGE_ORANGE)
    table.add_column("Timestamp", style=FORGE_GRAY, no_wrap=True)
    table.add_column("Mutation Kind", style="white")
    table.add_column("Outcome", no_wrap=True)
    table.add_column("Fitness Δ", justify="right", style=FORGE_TEAL, no_wrap=True)
    table.add_column("Description", style=FORGE_GRAY, overflow="fold")

    for e in entries:
        outcome = str(e.get("kind", "—"))
        table.add_row(
            str(e.get("timestamp", "—"))[:19],
            str(e.get("mutation_kind", "—")),
            f"[{_outcome_style(outcome)}]{outcome}[/]",
            _format_delta(e),
            (str(e.get("description", "")) or "")[:60],
        )

    console.print(table)
    raise typer.Exit(0)


@evolve_app.command("resume")
def evolve_resume(
    source: str = typer.Argument(
        ..., help="Agent flow file or module to reload.", metavar="SOURCE"
    ),
) -> None:
    """[bold]Re-arm[/] the evolution circuit breaker after it opened."""
    exit_code = asyncio.run(_do_resume(source))
    raise typer.Exit(exit_code)


async def _do_resume(source: str) -> int:
    from forge_core.config import ForgeConfig
    from forge_core.harness import MetaOrchestrator

    config = ForgeConfig()
    orchestrator = MetaOrchestrator(config=config)
    try:
        await orchestrator.load(source)
    except Exception as exc:
        console.print(f"[red]✗ Load failed:[/] {exc}")
        return 1

    breaker = orchestrator.evolution_breaker
    prev_state = breaker.state.value
    orchestrator.resume_evolution()
    console.print(
        f"  [{FORGE_TEAL}]✓ Evolution breaker reset[/] "
        f"[{FORGE_GRAY}](was: {prev_state} → now: {breaker.state.value})[/]"
    )
    return 0

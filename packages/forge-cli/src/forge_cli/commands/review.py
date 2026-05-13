"""forge review — Review Agents and Policy Gate commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

review_app = typer.Typer(help="Run review agents and policy gates.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

_SEV_COLOR = {
    "p0": "red bold",
    "p1": "red",
    "p2": "yellow",
    "p3": "dim",
}


def _import_review() -> Any:
    try:
        import forge_review

        return forge_review
    except ImportError:
        err_console.print(
            "[red]forge-review is not installed.[/red] Install with: "
            "[bold]pip install forge-review[/bold]"
        )
        raise typer.Exit(code=1) from None


def _print_review_result(result: Any) -> None:
    _import_review()
    summary = result.summary
    console.print(
        Panel(
            f"Hook: [bold]{result.hook}[/bold]  |  "
            f"Findings: {len(result.findings)}  |  "
            f"[{'red' if result.has_blocking else 'green'}]"
            f"{'BLOCKED' if result.has_blocking else 'ALLOWED'}[/]",
            expand=False,
        )
    )

    if not result.findings:
        console.print("[green]No findings — run passes review.[/green]")
        return

    table = Table(show_lines=True)
    table.add_column("Sev", no_wrap=True, width=4)
    table.add_column("Source", no_wrap=True)
    table.add_column("Title")
    table.add_column("Message")
    table.add_column("Conf", justify="right", width=5)
    for f in result.findings:
        color = _SEV_COLOR.get(f.severity, "white")
        table.add_row(
            f"[{color}]{f.severity.upper()}[/{color}]",
            f.source or "—",
            f.title,
            f.message[:80],
            f"{f.confidence:.0%}",
        )
    console.print(table)
    p = summary
    console.print(f"\n[dim]P0:{p['p0']}  P1:{p['p1']}  P2:{p['p2']}  P3:{p['p3']}[/dim]")


@review_app.command("run")
def review_run(
    hook: str = typer.Option(
        "pre_merge",
        "--hook",
        "-k",
        help="Hook kind: on_plan | pre_apply | pre_stop | pre_merge",
    ),
    run_result_file: Path | None = typer.Option(
        None,
        "--run-result",
        help="Path to RunResult JSON (else reads stdin)",
    ),
    run_id: str = typer.Option("", "--run-id", help="Run ID to record in the review result"),
    output_json: bool = typer.Option(False, "--json", help="Output ReviewResult as JSON"),
) -> None:
    """Run all review agents against a RunResult and apply the policy gate."""
    import asyncio

    forge_review = _import_review()
    from forge_core.types import RunResult

    # Parse hook kind
    try:
        forge_review.HookKind(hook)
    except ValueError:
        err_console.print(
            f"[red]Unknown hook:[/red] {hook!r}. Choose: on_plan, pre_apply, pre_stop, pre_merge"
        )
        raise typer.Exit(code=1) from None

    # Load RunResult
    if run_result_file:
        if not run_result_file.exists():
            err_console.print(f"[red]File not found:[/red] {run_result_file}")
            raise typer.Exit(code=1)
        raw = json.loads(run_result_file.read_text(encoding="utf-8"))
    else:
        if sys.stdin.isatty():
            err_console.print("[yellow]Reading RunResult JSON from stdin...[/yellow]")
        raw = json.load(sys.stdin)

    try:
        run_result_obj = RunResult.model_validate(raw)
    except Exception as exc:
        err_console.print(f"[red]Invalid RunResult JSON:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    runner = forge_review.ReviewRunner()
    review_result, decision = asyncio.run(runner.pre_merge(run_result_obj, run_id=run_id))

    if output_json:
        console.print(review_result.model_dump_json(indent=2))
    else:
        _print_review_result(review_result)

    raise typer.Exit(code=0 if decision.allowed else 1)


@review_app.command("show")
def review_show(
    result_file: Path = typer.Argument(..., help="Path to ReviewResult JSON"),
) -> None:
    """Display a saved ReviewResult in a readable format."""
    forge_review = _import_review()

    if not result_file.exists():
        err_console.print(f"[red]File not found:[/red] {result_file}")
        raise typer.Exit(code=1)

    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
        result = forge_review.ReviewResult.model_validate(data)
    except Exception as exc:
        err_console.print(f"[red]Error loading result:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _print_review_result(result)

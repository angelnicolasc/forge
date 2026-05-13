"""forge spec — Spec-Driven Development commands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

spec_app = typer.Typer(help="Manage and verify Forge spec artifacts.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)


def _import_spec():
    try:
        import forge_spec
        return forge_spec
    except ImportError:
        err_console.print(
            "[red]forge-spec is not installed.[/red] Install with: "
            "[bold]pip install forge-spec[/bold]"
        )
        raise typer.Exit(code=1)


@spec_app.command("init")
def spec_init(
    name: str = typer.Argument(..., help="Spec name"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file (default: <name>.yaml)"),
) -> None:
    """Scaffold a new spec.yaml with sensible defaults."""
    _import_spec()
    from forge_spec.types import AcceptanceCriterion, CheckType, SpecDef
    from forge_spec.loader import dump_spec_yaml

    spec = SpecDef(
        name=name,
        version="0.1.0",
        description=f"Spec for {name}",
        objectives=[f"Define acceptance criteria for {name}"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="ac-001",
                description="Run completes successfully",
                check_type=CheckType.ASSERTION,
                expected="run_result.status == 'completed'",
                weight=1.0,
            )
        ],
    )

    out_path = output or Path(f"{name}.yaml")
    out_path.write_text(dump_spec_yaml(spec), encoding="utf-8")
    console.print(f"[green]✓[/green] Spec written to [bold]{out_path}[/bold]")


@spec_app.command("show")
def spec_show(
    spec_file: Path = typer.Argument(..., help="Path to spec.yaml"),
) -> None:
    """Display a spec.yaml in a readable format."""
    forge_spec = _import_spec()

    if not spec_file.exists():
        err_console.print(f"[red]File not found:[/red] {spec_file}")
        raise typer.Exit(code=1)

    try:
        spec = forge_spec.load_spec_yaml(spec_file)
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Error loading spec:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(Panel(f"[bold]{spec.name}[/bold] v{spec.version}", expand=False))
    if spec.description:
        console.print(f"[dim]{spec.description}[/dim]\n")

    if spec.objectives:
        console.print("[bold]Objectives:[/bold]")
        for obj in spec.objectives:
            console.print(f"  • {obj}")
        console.print()

    if spec.acceptance_criteria:
        table = Table(title="Acceptance Criteria", show_lines=True)
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Description")
        table.add_column("Check", style="magenta")
        table.add_column("Weight", justify="right")
        for c in spec.acceptance_criteria:
            table.add_row(c.id, c.description, c.check_type, str(c.weight))
        console.print(table)

    if spec.risks:
        rtable = Table(title="Risks", show_lines=True)
        rtable.add_column("ID", style="cyan", no_wrap=True)
        rtable.add_column("Description")
        rtable.add_column("Score", justify="right")
        for r in spec.risks:
            rtable.add_row(r.id, r.description, f"{r.score:.2f}")
        console.print(rtable)

    console.print(f"\n[dim]spec_hash: {spec.spec_hash}[/dim]")


@spec_app.command("verify")
def spec_verify(
    spec_file: Path = typer.Argument(..., help="Path to spec.yaml"),
    run_id: str = typer.Option("manual", "--run-id", help="Run ID to record in result"),
    output_text: str = typer.Option("", "--output", help="Simulate run output for CONTAINS/REGEX checks"),
    status: str = typer.Option("completed", "--status", help="Simulate run status (completed/failed)"),
) -> None:
    """Verify a spec against a simulated RunResult (for testing spec correctness)."""
    import asyncio
    forge_spec = _import_spec()
    from forge_core.types import RunResult, RunStatus

    if not spec_file.exists():
        err_console.print(f"[red]File not found:[/red] {spec_file}")
        raise typer.Exit(code=1)

    try:
        spec = forge_spec.load_spec_yaml(spec_file)
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Error loading spec:[/red] {exc}")
        raise typer.Exit(code=1)

    try:
        run_status = RunStatus(status)
    except ValueError:
        err_console.print(f"[red]Invalid status:[/red] {status!r}. Use: completed, failed, cancelled")
        raise typer.Exit(code=1)

    run_result = RunResult(task_id="cli", run_id=run_id, status=run_status, output=output_text or None)
    vr = asyncio.run(forge_spec.SpecVerifier().verify(spec, run_result))

    console.print(f"\n[bold]Spec:[/bold] {spec.name} v{spec.version}")
    console.print(f"[bold]Compliance:[/bold] {vr.compliance_rate:.0%}  [bold]Passed:[/bold] {vr.passed}")

    table = Table(show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Message")
    for r in vr.results:
        status_color = {
            "passed": "green",
            "failed": "red",
            "skipped": "yellow",
            "error": "red bold",
            "pending": "dim",
        }.get(r.status, "white")
        table.add_row(r.criterion_id, f"[{status_color}]{r.status}[/{status_color}]", r.message)
    console.print(table)

    raise typer.Exit(code=0 if vr.passed else 1)


@spec_app.command("attest")
def spec_attest(
    spec_file: Path = typer.Argument(..., help="Path to spec.yaml"),
    run_id: str = typer.Option(..., "--run-id", help="Run ID to attest"),
    compliance_rate: float = typer.Option(..., "--compliance-rate", help="Compliance rate [0.0–1.0]"),
    passed: bool = typer.Option(False, "--passed/--failed", help="Whether the run passed"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write attestation JSON to file"),
) -> None:
    """Generate a SLSA Level 2-inspired attestation for a completed spec run."""
    forge_spec = _import_spec()
    from forge_spec.types import VerifyResult

    if not spec_file.exists():
        err_console.print(f"[red]File not found:[/red] {spec_file}")
        raise typer.Exit(code=1)

    try:
        spec = forge_spec.load_spec_yaml(spec_file)
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Error loading spec:[/red] {exc}")
        raise typer.Exit(code=1)

    vr = VerifyResult(
        spec_name=spec.name,
        spec_version=spec.version,
        spec_hash=spec.spec_hash,
        run_id=run_id,
        compliance_rate=compliance_rate,
        passed=passed,
    )
    attester = forge_spec.SpecAttester()
    att = attester.attest(spec, vr)
    json_str = attester.to_json(att)

    if output:
        output.write_text(json_str, encoding="utf-8")
        console.print(f"[green]✓[/green] Attestation written to [bold]{output}[/bold]")
    else:
        console.print(json_str)

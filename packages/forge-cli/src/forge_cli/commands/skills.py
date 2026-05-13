"""forge skills — CLI commands for the Skills Runtime.

All forge_skills imports are inside command bodies for graceful degradation:
if forge-skills is not installed the CLI still starts; the user sees a clear
ImportError only when they actually run a skills sub-command.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

skills_app = typer.Typer(
    name="skills",
    help="Load, inspect, and invoke Forge skills.",
    no_args_is_help=True,
)

console = Console()
err = Console(stderr=True, style="bold red")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@skills_app.command("list")
def list_skills(
    skills_dir: Path = typer.Option(
        Path("skills"),
        "--dir",
        "-d",
        help="Directory to scan for SKILL.md files.",
        show_default=True,
    ),
    tag: str = typer.Option("", "--tag", "-t", help="Filter by tag."),
) -> None:
    """List all skills found in the skills directory."""
    try:
        from forge_skills.loader import load_dir
    except ImportError:
        err.print("forge-skills is not installed. Run: pip install forge-skills")
        raise typer.Exit(1) from None

    if not skills_dir.exists():
        err.print(f"Directory not found: {skills_dir}")
        raise typer.Exit(1)

    loaded = load_dir(skills_dir)
    if tag:
        loaded = [s for s in loaded if tag in s.tags]

    if not loaded:
        console.print("[dim]No skills found.[/dim]")
        return

    table = Table(title=f"Skills in {skills_dir}", show_lines=False)
    table.add_column("Name", style="bold cyan")
    table.add_column("Description")
    table.add_column("Tags", style="dim")
    table.add_column("Timeout", justify="right")

    for skill in sorted(loaded, key=lambda s: s.name):
        table.add_row(
            skill.name,
            skill.description or "[dim]—[/dim]",
            ", ".join(skill.tags) or "—",
            f"{skill.timeout_seconds}s",
        )

    console.print(table)


@skills_app.command("explain")
def explain_skill(
    name: str = typer.Argument(..., help="Skill name to explain."),
    skills_dir: Path = typer.Option(Path("skills"), "--dir", "-d"),
) -> None:
    """Show the full definition of a skill."""
    try:
        from forge_skills.loader import load_dir
    except ImportError:
        err.print("forge-skills is not installed. Run: pip install forge-skills")
        raise typer.Exit(1) from None

    loaded = {s.name: s for s in load_dir(skills_dir)}
    skill = loaded.get(name)
    if skill is None:
        err.print(f"Skill '{name}' not found in {skills_dir}")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]{skill.name}[/bold cyan]")
    console.print(f"  [dim]Description:[/dim] {skill.description}")
    console.print(f"  [dim]Source:[/dim]      {skill.source}")
    console.print(f"  [dim]Timeout:[/dim]     {skill.timeout_seconds}s")
    console.print(f"  [dim]Max calls:[/dim]   {skill.max_calls}")
    if skill.tags:
        console.print(f"  [dim]Tags:[/dim]       {', '.join(skill.tags)}")
    if skill.examples:
        console.print("  [dim]Examples:[/dim]")
        for ex in skill.examples:
            console.print(f"    · {ex}")
    if skill.parameters:
        console.print("  [dim]Parameters:[/dim]")
        for p in skill.parameters:
            req = "required" if p.required else f"optional (default: {p.default})"
            console.print(f"    · {p.name} [{p.type.value}] — {p.description} ({req})")
    if skill.body:
        console.print("\n[dim]Body:[/dim]")
        console.print(skill.body)


@skills_app.command("run")
def run_skill(
    name: str = typer.Argument(..., help="Skill name to invoke."),
    args_json: str = typer.Option(
        "{}",
        "--args",
        help='Arguments as a JSON object, e.g. \'{"user": "Alice"}\'.',
    ),
    skills_dir: Path = typer.Option(Path("skills"), "--dir", "-d"),
    run_id: str = typer.Option("", "--run-id", help="Optional run ID for tracing."),
) -> None:
    """Invoke a skill by name with the given arguments."""
    try:
        from forge_skills.loader import load_dir
        from forge_skills.runtime import SkillRuntime
    except ImportError:
        err.print("forge-skills is not installed. Run: pip install forge-skills")
        raise typer.Exit(1) from None

    try:
        arguments = json.loads(args_json)
    except json.JSONDecodeError as exc:
        err.print(f"Invalid --args JSON: {exc}")
        raise typer.Exit(1) from exc

    import asyncio

    rt = SkillRuntime()
    for skill in load_dir(skills_dir):
        rt.register(skill)

    result = asyncio.run(rt.invoke(name, arguments=arguments, run_id=run_id))

    if not result.ok:
        err.print(f"[bold]Error:[/bold] {result.error}")
        raise typer.Exit(1)

    console.print(
        f"[bold green]✓[/bold green] {result.skill_name} completed in {result.duration_ms:.1f}ms"
    )
    console.print(result.output)

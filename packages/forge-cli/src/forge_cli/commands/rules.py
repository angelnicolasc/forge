"""forge rules — Validate, lint, explain, and diff rule packs."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from forge_observe.exporters.console import FORGE_GRAY, FORGE_ORANGE, FORGE_TEAL

rules_app = typer.Typer(help="Manage and inspect Forge rule packs.")
console = Console()


@rules_app.command("validate")
def rules_validate(
    pack_file: Path = typer.Argument(..., help="YAML rule pack file to validate.", metavar="FILE"),
) -> None:
    """[bold]Validate[/] a rule pack YAML file for schema and logical errors."""
    if not pack_file.exists():
        console.print(f"[red]✗ File not found:[/] {pack_file}")
        raise typer.Exit(1)

    from forge_rules.engine import RulesEngine
    from forge_rules.loader import load_yaml

    try:
        pack = load_yaml(pack_file)
    except ValueError as exc:
        console.print(f"[red]✗ Parse error:[/]\n{exc}")
        raise typer.Exit(1) from exc

    engine = RulesEngine()
    errors = engine.validate(pack)

    if errors:
        for err in errors:
            console.print(f"  [yellow]⚠[/] {err}")
        raise typer.Exit(1)

    console.print(
        f"  [{FORGE_TEAL}]✓ Pack [bold]{pack.name}[/] v{pack.version}[/] "
        f"— {len(pack.rules)} rule(s), no errors."
    )


@rules_app.command("lint")
def rules_lint(
    pack_file: Path = typer.Argument(..., help="YAML rule pack to lint.", metavar="FILE"),
    fail_on_intersect: bool = typer.Option(
        False, "--strict", "-s", help="Exit 1 on any scope intersection."
    ),
) -> None:
    """[bold]Lint[/] a rule pack for overlapping scope patterns (scope intersection detection)."""
    if not pack_file.exists():
        console.print(f"[red]✗ File not found:[/] {pack_file}")
        raise typer.Exit(1)

    from forge_rules.engine import RulesEngine
    from forge_rules.loader import load_yaml

    try:
        pack = load_yaml(pack_file)
    except ValueError as exc:
        console.print(f"[red]✗ Parse error:[/]\n{exc}")
        raise typer.Exit(1) from exc

    engine = RulesEngine()
    engine.load_pack(pack)
    intersections = engine.lint()

    if not intersections:
        console.print(f"  [{FORGE_TEAL}]✓ No scope intersections found in {pack.name!r}.[/]")
        return

    table = Table(title="Scope Intersections", border_style=FORGE_ORANGE)
    table.add_column("Rule A", style=FORGE_GRAY)
    table.add_column("Rule B", style=FORGE_GRAY)
    table.add_column("Pattern A", style="dim white")
    table.add_column("Pattern B", style="dim white")
    table.add_column("Example Path", style=FORGE_TEAL)

    for ix in intersections:
        table.add_row(ix.rule_a, ix.rule_b, ix.pattern_a, ix.pattern_b, ix.example_path)

    console.print(table)
    console.print(
        f"\n  [yellow]⚠ {len(intersections)} intersection(s) detected.[/] "
        "Resolve ambiguity with priority or scope refinement."
    )

    if fail_on_intersect:
        raise typer.Exit(1)


@rules_app.command("explain")
def rules_explain(
    pack_file: Path = typer.Argument(..., help="YAML rule pack to explain.", metavar="FILE"),
    path: str = typer.Option("", "--path", "-p", help="Simulate file path for scope matching."),
    intent: str = typer.Option("", "--intent", "-i", help="Simulate intent label."),
) -> None:
    """[bold]Explain[/] which rules would apply for a given file path and intent."""
    if not pack_file.exists():
        console.print(f"[red]✗ File not found:[/] {pack_file}")
        raise typer.Exit(1)

    from forge_rules.engine import RulesEngine
    from forge_rules.loader import load_yaml

    try:
        pack = load_yaml(pack_file)
    except ValueError as exc:
        console.print(f"[red]✗ Parse error:[/]\n{exc}")
        raise typer.Exit(1) from exc

    engine = RulesEngine()
    engine.load_pack(pack)
    selection = engine.select(path=path, intent=intent)

    if not selection.rules and not selection.denied_ids:
        console.print(f"  [{FORGE_GRAY}]No rules match path={path!r} intent={intent!r}.[/]")
        return

    table = Table(
        title=f"Rules for path={path!r} intent={intent!r}",
        border_style=FORGE_ORANGE,
    )
    table.add_column("Action", style="bold")
    table.add_column("ID", style=FORGE_GRAY)
    table.add_column("Description", style="white")

    action_colors = {"deny": "red", "require": "yellow", "suggest": FORGE_TEAL}
    for rule in selection.rules:
        color = action_colors.get(rule.action, "white")
        table.add_row(f"[{color}]{rule.action.upper()}[/]", rule.id, rule.description)

    console.print(table)

    if selection.denied_ids:
        console.print(f"  [red]Denied:[/] {', '.join(selection.denied_ids)}")
    if selection.conflicts_resolved:
        console.print(f"  [{FORGE_GRAY}]{selection.conflicts_resolved} conflict(s) resolved.[/]")

    console.print()
    ctx = selection.context_text()
    if ctx:
        console.print("[dim]Context block that would be injected:[/]")
        console.print(f"[dim]{ctx}[/]")


@rules_app.command("diff")
def rules_diff(
    pack_a: Path = typer.Argument(..., help="Base YAML rule pack.", metavar="PACK_A"),
    pack_b: Path = typer.Argument(..., help="New YAML rule pack.", metavar="PACK_B"),
) -> None:
    """[bold]Diff[/] two rule packs — show added, removed, and changed rules."""
    for p in (pack_a, pack_b):
        if not p.exists():
            console.print(f"[red]✗ File not found:[/] {p}")
            raise typer.Exit(1)

    from forge_rules.loader import load_yaml

    try:
        a = load_yaml(pack_a)
        b = load_yaml(pack_b)
    except ValueError as exc:
        console.print(f"[red]✗ Parse error:[/]\n{exc}")
        raise typer.Exit(1) from exc

    rules_a = {r.id: r for r in a.rules}
    rules_b = {r.id: r for r in b.rules}

    added = set(rules_b) - set(rules_a)
    removed = set(rules_a) - set(rules_b)
    changed = {
        rid
        for rid in set(rules_a) & set(rules_b)
        if rules_a[rid].model_dump() != rules_b[rid].model_dump()
    }

    if not added and not removed and not changed:
        console.print(f"  [{FORGE_TEAL}]✓ Packs are identical.[/]")
        return

    table = Table(title=f"Diff {pack_a.name} → {pack_b.name}", border_style=FORGE_ORANGE)
    table.add_column("Change", style="bold")
    table.add_column("Rule ID", style=FORGE_GRAY)
    table.add_column("Details", style="white")

    for rid in sorted(added):
        r = rules_b[rid]
        table.add_row(f"[{FORGE_TEAL}]+ added[/]", rid, r.description)
    for rid in sorted(removed):
        r = rules_a[rid]
        table.add_row("[red]- removed[/]", rid, r.description)
    for rid in sorted(changed):
        table.add_row("[yellow]~ changed[/]", rid, "")

    console.print(table)

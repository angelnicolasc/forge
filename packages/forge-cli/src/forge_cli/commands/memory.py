"""forge memory — Query, ingest, and inspect the Living Collaborative Memory."""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003  (needed at runtime: Typer evaluates signature annotations)
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from forge_observe.exporters.console import FORGE_GRAY, FORGE_ORANGE, FORGE_TEAL

memory_app = typer.Typer(help="Manage the Living Collaborative Memory.")
console = Console()


@memory_app.command("query")
def memory_query(
    text: str = typer.Argument(..., help="Query text for semantic search.", metavar="QUERY"),
    top_k: int = typer.Option(10, "--top-k", "-k", help="Max results to return."),
    entity: list[str] | None = typer.Option(None, "--entity", "-e", help="Filter by entity."),
    show_content: bool = typer.Option(False, "--content", "-c", help="Show full content."),
) -> None:
    """[bold]Search[/] the Living Memory with natural language or entity filters."""
    results = asyncio.run(_do_query(text, top_k, entity or [], show_content))
    raise typer.Exit(0 if results else 1)


async def _do_query(text: str, top_k: int, entities: list[str], show_content: bool) -> list[Any]:
    from forge_core.types import MemoryQuery
    from forge_memory.hybrid import HybridMemory

    memory = HybridMemory()
    q = MemoryQuery(text=text, entity_filter=entities or None, top_k=top_k)
    results = await memory.query(q)

    if not results:
        console.print(f"  [{FORGE_TEAL}]No results found for:[/] {text}")
        return []

    table = Table(title=f"Memory Query: '{text}'", border_style=FORGE_ORANGE)
    table.add_column("Score", justify="right", style=FORGE_TEAL, no_wrap=True)
    table.add_column("ID", style=FORGE_GRAY, no_wrap=True)
    table.add_column("Source Run", style=FORGE_GRAY, no_wrap=True)
    table.add_column("Entities", style="dim white")
    table.add_column("Content Preview" if not show_content else "Content", style="white")

    for entry, score in results:
        content_display = (
            entry.content
            if show_content
            else entry.content[:80] + ("..." if len(entry.content) > 80 else "")
        )
        table.add_row(
            f"{score:.3f}",
            entry.id[:12],
            (entry.source_run_id or "—")[:12],
            ", ".join(entry.entities[:3]),
            content_display,
        )

    console.print(table)
    return results


@memory_app.command("ingest")
def memory_ingest(
    path: Path = typer.Argument(..., help="Text file to ingest.", metavar="FILE"),
    run_id: str | None = typer.Option(None, "--run-id", help="Associate with a run ID."),
    tag: list[str] | None = typer.Option(None, "--tag", "-t", help="Tags as key=value."),
) -> None:
    """[bold]Ingest[/] a text file into the Living Memory."""
    if not path.exists():
        console.print(f"[red]✗ File not found:[/] {path}")
        raise typer.Exit(1)

    tags: dict[str, str] = {}
    for t in tag or []:
        if "=" in t:
            k, v = t.split("=", 1)
            tags[k.strip()] = v.strip()

    ids = asyncio.run(_do_ingest(path.read_text(), run_id, tags))
    console.print(f"  [{FORGE_TEAL}]✓ Ingested {len(ids)} chunk(s) from {path.name}[/]")


async def _do_ingest(content: str, run_id: str | None, tags: dict[str, str]) -> list[str]:
    from forge_memory.hybrid import HybridMemory

    memory = HybridMemory()
    return await memory.ingest_text(content, source_run_id=run_id, tags=tags)


@memory_app.command("status")
def memory_status() -> None:
    """Show Living Memory statistics and health."""
    stats = asyncio.run(_do_status())
    table = Table(title="Living Memory Status", border_style=FORGE_ORANGE)
    table.add_column("Component", style="bold white")
    table.add_column("Metric", style=FORGE_GRAY)
    table.add_column("Value", style=FORGE_TEAL, justify="right")

    for section, data in stats["stats"].items():
        if isinstance(data, dict):
            for k, v in data.items():
                table.add_row(section, k, str(v))
        else:
            table.add_row(section, "value", str(data))

    console.print(table)

    health = stats["health"]
    for backend, ok in health.items():
        icon = "✓" if ok else "✗"
        color = FORGE_TEAL if ok else "red"
        console.print(f"  [{color}]{icon}[/] {backend}")


async def _do_status() -> dict[str, Any]:
    from forge_memory.hybrid import HybridMemory

    memory = HybridMemory()
    return {
        "health": await memory.health_check(),
        "stats": memory.stats(),
    }

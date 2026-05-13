"""Forge CLI entry point.

Usage:
    forge wrap my_flow.py
    forge run --input '{"query": "What is RAG?"}'
    forge evolve --runs 5
    forge observe
    forge memory query "RAG optimization"
    forge memory ingest document.txt
    forge init my_project
"""

from __future__ import annotations

import contextlib
import sys

import typer
from rich.console import Console

from forge_cli._version import __version__

# Windows consoles default to cp1252, which cannot encode the unicode glyphs
# (✓, ⚡, ·, em-dashes) Forge emits throughout its Rich output. Reconfigure
# stdout/stderr to UTF-8 at CLI entry so `forge doctor`, `forge wrap`, etc.
# render without crashing in PowerShell/cmd.exe. No-op on POSIX terminals
# that already default to UTF-8.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(_stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    name="forge",
    help="Forge — Universal Agent Harness. Drop your agents. Watch them evolve, remember and win.",
    add_completion=True,
    rich_markup_mode="rich",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold #FF6B35]Forge[/] [white]v{__version__}[/]")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """[bold #FF6B35]Forge[/] — The living, self-evolving harness for production multi-agent systems.

    \b
    [dim]2026 is not the year of models. It's the year of harnesses.[/dim]
    """  # noqa: E501


# ---------------------------------------------------------------------------
# Import sub-commands
# ---------------------------------------------------------------------------
# Each command lives in its own module for clean organization.

from forge_cli.commands.doctor import doctor_app  # noqa: E402
from forge_cli.commands.evolve import evolve_app  # noqa: E402
from forge_cli.commands.mcp import mcp_app  # noqa: E402
from forge_cli.commands.memory import memory_app  # noqa: E402
from forge_cli.commands.observe import observe_app  # noqa: E402
from forge_cli.commands.rules import rules_app  # noqa: E402
from forge_cli.commands.run import run_app  # noqa: E402
from forge_cli.commands.skills import skills_app  # noqa: E402
from forge_cli.commands.spec import spec_app  # noqa: E402
from forge_cli.commands.wrap import wrap_app  # noqa: E402

app.add_typer(wrap_app, name="wrap", help="Wrap a multi-agent flow and instrument it.")
app.add_typer(run_app, name="run", help="Execute a wrapped flow.")
app.add_typer(evolve_app, name="evolve", help="Trigger the self-evolution loop.")
app.add_typer(observe_app, name="observe", help="Open the live dashboard.")
app.add_typer(memory_app, name="memory", help="Query, ingest, and inspect the Living Memory.")
app.add_typer(rules_app, name="rules", help="Validate, lint, explain, and diff rule packs.")
app.add_typer(mcp_app, name="mcp", help="Manage MCP tool routing, budgets, and cache.")
app.add_typer(skills_app, name="skills", help="Load, inspect, and invoke Forge skills.")
app.add_typer(spec_app, name="spec", help="Init, verify, and attest Forge specs.")
app.add_typer(doctor_app, name="doctor", help="Diagnose the local Forge installation.")

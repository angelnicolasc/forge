"""Rich-powered console exporter for Forge.

Displays a live terminal dashboard showing:
- Per-agent cost breakdown
- Token usage (input/output/cache)
- Latency per agent
- Agent topology (ASCII tree)
- Evolution suggestions (when enabled)

This is the "viral" interface — the 47-second demo lives here.
"""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from forge_core.types import AgentCard, CostSummary, Mutation, RunResult, RunStatus

_console = Console()

# ---------------------------------------------------------------------------
# ANSI colour palette for Forge branding
# ---------------------------------------------------------------------------

FORGE_ORANGE = "bold #FF6B35"
FORGE_TEAL = "#00D4AA"
FORGE_PURPLE = "#8B5CF6"
FORGE_GRAY = "dim white"
SUCCESS_GREEN = "bold green"
ERROR_RED = "bold red"
WARN_YELLOW = "bold yellow"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def print_forge_banner() -> None:
    """Print the Forge welcome banner."""
    banner = Text()
    banner.append("  ⚡ ", style=FORGE_ORANGE)
    banner.append("FORGE", style="bold white")
    banner.append(" — Universal Agent Harness", style=FORGE_TEAL)
    banner.append("\n  Drop your agents. Watch them evolve, remember and win.\n", style=FORGE_GRAY)
    _console.print(Panel(banner, border_style=FORGE_ORANGE, padding=(0, 2)))


def print_run_result(result: RunResult, verbose: bool = False) -> None:
    """Print a complete run result in a beautiful terminal layout."""
    _console.rule(f"[{FORGE_TEAL}]Run Complete[/]", style=FORGE_ORANGE)

    # Status badge
    if result.status == RunStatus.COMPLETED:
        status_text = Text("✓ COMPLETED", style=SUCCESS_GREEN)
    elif result.status == RunStatus.FAILED:
        status_text = Text("✗ FAILED", style=ERROR_RED)
    else:
        status_text = Text(f"⚡ {result.status.upper()}", style=WARN_YELLOW)

    # Header info
    info_table = Table.grid(padding=(0, 2))
    info_table.add_column(style="dim white", no_wrap=True)
    info_table.add_column(style="white")
    info_table.add_row("Status", status_text)
    info_table.add_row("Task ID", Text(result.task_id[:16] + "...", style=FORGE_GRAY))
    info_table.add_row("Trace ID", Text(result.trace_id[:16] + "...", style=FORGE_GRAY))
    if result.duration_ms:
        info_table.add_row("Duration", _fmt_duration(result.duration_ms))
    _console.print(Panel(info_table, title="[bold white]Run Info[/]", border_style=FORGE_PURPLE))

    # Cost summary
    if result.cost.total_cost > 0:
        _print_cost_panel(result.cost)

    # Topology
    if result.topology:
        _print_topology_tree(result.topology)

    # Evolution mutations — RunResult may not carry this field in 0.1.x;
    # treat absence as "none applied" rather than crashing the pretty-
    # printer (which would also crash `forge wrap` in production).
    mutations_applied = getattr(result, "mutations_applied", None) or []
    if mutations_applied:
        _console.print(
            Panel(
                f"[{FORGE_TEAL}]⚗  {len(mutations_applied)} mutation(s) applied this run[/]",
                border_style=FORGE_TEAL,
                title="[bold]Evolution[/]",
            )
        )

    # Errors
    if result.errors:
        for err in result.errors:
            _console.print(f"  [{ERROR_RED}]✗[/] {err}")

    # Output (verbose)
    if verbose and result.output is not None:
        _console.print(Panel(str(result.output), title="[bold]Output[/]", border_style=FORGE_GRAY))


def print_evolution_proposal(mutation: Mutation) -> None:
    """Print an evolution proposal for human review."""
    table = Table(box=box.ROUNDED, border_style=FORGE_TEAL)
    table.add_column("Field", style="dim white")
    table.add_column("Value", style="white")
    table.add_row("Kind", Text(mutation.kind.value, style=FORGE_ORANGE))
    table.add_row("Confidence", _confidence_bar(mutation.confidence))
    table.add_row("Description", mutation.description)
    if mutation.rationale:
        table.add_row("Rationale", Text(mutation.rationale, style=FORGE_GRAY))
    _console.print(
        Panel(table, title=f"[{FORGE_TEAL}]⚗  Evolution Proposal[/]", border_style=FORGE_TEAL)
    )


def live_progress(description: str) -> Progress:
    """Return a pre-configured Rich Progress bar for live operations."""
    return Progress(
        SpinnerColumn(spinner_name="dots", style=FORGE_ORANGE),
        TextColumn("[bold white]{task.description}"),
        BarColumn(bar_width=30, style=FORGE_TEAL, complete_style=FORGE_ORANGE),
        TaskProgressColumn(),
        console=_console,
        transient=False,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _print_cost_panel(cost: CostSummary) -> None:
    table = Table(box=box.SIMPLE, show_header=True, header_style=f"bold {FORGE_ORANGE}")
    table.add_column("Scope", style="white")
    table.add_column("Cost (USD)", justify="right", style=FORGE_ORANGE)
    table.add_column("Tokens In", justify="right", style=FORGE_GRAY)
    table.add_column("Tokens Out", justify="right", style=FORGE_GRAY)

    # Per-agent breakdown
    for agent_id, agent_cost in cost.cost_by_agent.items():
        table.add_row(
            f"[dim]agent:[/] {agent_id[:12]}",
            f"${agent_cost:.5f}",
            "—",
            "—",
        )

    # Total row
    table.add_section()
    table.add_row(
        "[bold white]TOTAL[/]",
        f"[bold {FORGE_ORANGE}]${cost.total_cost:.5f}[/]",
        str(cost.total_input_tokens),
        str(cost.total_output_tokens),
    )

    _console.print(Panel(table, title="[bold]Cost Breakdown[/]", border_style=FORGE_ORANGE))


def _print_topology_tree(agents: list[AgentCard]) -> None:
    tree = Tree(f"[{FORGE_ORANGE}]⚙ Agent Topology[/]", guide_style=FORGE_GRAY)
    # Index by id for edge traversal
    id_map = {a.id: a for a in agents}
    roots = [a for a in agents if not a.upstream]
    visited: set[str] = set()

    def add_node(agent: AgentCard, parent: Tree) -> None:
        if agent.id in visited:
            return
        visited.add(agent.id)
        label = Text()
        label.append(f"[{agent.role or 'agent'}] ", style=FORGE_GRAY)
        label.append(agent.name, style="bold white")
        if agent.model:
            label.append(f" ({agent.model})", style=FORGE_TEAL)
        if agent.tools:
            label.append(f" [{len(agent.tools)} tools]", style=FORGE_PURPLE)
        branch = parent.add(label)
        for ds_id in agent.downstream:
            if ds_id in id_map:
                add_node(id_map[ds_id], branch)

    for root in roots:
        add_node(root, tree)

    # If no explicit edges, just list them flat
    if not roots:
        for agent in agents:
            tree.add(f"[bold white]{agent.name}[/] [{FORGE_TEAL}]{agent.role}[/]")

    _console.print(Panel(tree, border_style=FORGE_PURPLE))


def _fmt_duration(ms: float) -> Text:
    if ms < 1000:
        return Text(f"{ms:.0f}ms", style=FORGE_TEAL)
    return Text(f"{ms / 1000:.2f}s", style=FORGE_TEAL)


def _confidence_bar(score: float) -> Text:
    filled = int(score * 10)
    bar = "█" * filled + "░" * (10 - filled)
    color = SUCCESS_GREEN if score >= 0.7 else WARN_YELLOW if score >= 0.4 else ERROR_RED
    return Text(f"{bar}  {score:.0%}", style=color)

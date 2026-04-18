"""forge doctor — Quick environment diagnostic.

Prevents the top 80% of "it doesn't work on my machine" support tickets
on day one: catches mismatched Python versions, missing optional
extras, and broken default-backend imports before the user runs into
them via ``forge wrap``.

Usage:
    forge doctor              # local checks, no network
    forge doctor --network    # also ping Anthropic / OpenAI endpoints
"""

from __future__ import annotations

import importlib
import platform
import sys

import typer
from rich.console import Console
from rich.table import Table

from forge_observe.exporters.console import FORGE_ORANGE, FORGE_TEAL

doctor_app = typer.Typer(invoke_without_command=True)
console = Console()


_MIN_PY = (3, 11)

# (pretty_name, import_path, extras hint)
_OPTIONAL_EXTRAS: list[tuple[str, str, str]] = [
    ("LangGraph adapter", "langgraph", "pip install 'forge-os[langgraph]'"),
    ("CrewAI adapter", "crewai", "pip install 'forge-os[crewai]'"),
    ("AutoGen adapter", "autogen_core", "pip install 'forge-os[autogen]'"),
    ("LangChain callback", "langchain_core", "pip install langchain-core"),
]

_CORE_BACKENDS: list[tuple[str, str]] = [
    ("ChromaDB (default vector memory)", "chromadb"),
    ("NetworkX (default graph memory)", "networkx"),
]


def _check_module(module: str) -> tuple[bool, str]:
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # pragma: no cover — env-dependent
        return False, str(exc)
    version = getattr(mod, "__version__", "?")
    return True, version


@doctor_app.callback(invoke_without_command=True)
def doctor(
    network: bool = typer.Option(
        False, "--network", help="Also ping Anthropic / OpenAI healthz endpoints."
    ),
) -> None:
    """Diagnose the local Forge installation."""
    console.print(f"\n  [{FORGE_ORANGE}]⚡ forge doctor[/]\n")

    table = Table(show_header=True, header_style=f"bold {FORGE_TEAL}")
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail", style="dim")

    issues = 0

    # Python version
    py_version = sys.version_info
    py_ok = py_version >= _MIN_PY
    if py_ok:
        table.add_row(
            "Python version",
            "[green]\u2713[/]",
            f"{platform.python_version()} (\u2265 3.11 required)",
        )
    else:
        issues += 1
        table.add_row(
            "Python version",
            "[red]\u2717[/]",
            f"{platform.python_version()} — need \u2265 3.11",
        )

    # Forge core packages
    for pkg in ("forge_core", "forge_cli", "forge_observe", "forge_memory", "forge_adapters"):
        ok, ver = _check_module(pkg)
        if ok:
            table.add_row(pkg, "[green]\u2713[/]", ver)
        else:
            issues += 1
            table.add_row(pkg, "[red]\u2717[/]", f"Import failed: {ver[:60]}")

    # Default memory backends
    for label, mod in _CORE_BACKENDS:
        ok, detail = _check_module(mod)
        if ok:
            table.add_row(label, "[green]\u2713[/]", f"{mod} {detail}")
        else:
            issues += 1
            table.add_row(label, "[red]\u2717[/]", f"Missing: pip install {mod}")

    # Optional extras
    for label, mod, hint in _OPTIONAL_EXTRAS:
        ok, detail = _check_module(mod)
        if ok:
            table.add_row(label, "[green]\u2713[/]", f"{mod} {detail}")
        else:
            table.add_row(label, "[yellow]\u26a0[/]", f"not installed ({hint})")

    if network:
        _check_network(table)

    console.print(table)

    if issues:
        console.print(
            f"\n  [red]\u2717 {issues} issue(s) found.[/] "
            "Fix the red rows above before running [bold]forge wrap[/].\n"
        )
        raise typer.Exit(1)

    console.print(
        "\n  [green]\u2713 Everything looks good.[/] "
        "Try [bold]forge wrap examples/langgraph_research/flow.py[/] next.\n"
    )


def _check_network(table: Table) -> None:  # pragma: no cover — network-dependent
    """Best-effort liveness check of the two LLM providers Forge instruments."""
    try:
        import httpx
    except ImportError:
        table.add_row("Network check", "[yellow]\u26a0[/]", "httpx not installed; skipped")
        return

    endpoints = [
        ("Anthropic API", "https://api.anthropic.com"),
        ("OpenAI API", "https://api.openai.com"),
    ]
    for label, url in endpoints:
        try:
            resp = httpx.get(url, timeout=3.0)
            # Any non-5xx means the endpoint is reachable; the actual
            # API needs an API key we don't want to exercise here.
            if resp.status_code < 500:
                table.add_row(label, "[green]\u2713[/]", f"reachable (HTTP {resp.status_code})")
            else:
                table.add_row(label, "[yellow]\u26a0[/]", f"HTTP {resp.status_code}")
        except Exception as exc:
            table.add_row(label, "[yellow]\u26a0[/]", f"unreachable: {str(exc)[:50]}")

"""forge observe — Launch the Forge dashboard and metrics server."""

from __future__ import annotations

import typer
from rich.console import Console

from forge_observe.exporters.console import FORGE_ORANGE, FORGE_TEAL

observe_app = typer.Typer(invoke_without_command=True)
console = Console()


@observe_app.callback(invoke_without_command=True)
def observe(
    host: str = typer.Option("127.0.0.1", "--host", help="API server host."),
    port: int = typer.Option(8787, "--port", "-p", help="API server port."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open dashboard in browser."),
) -> None:
    """Start the Forge observe REST + SSE API server.

    \b
    [dim]The API serves real-time metrics, run traces, and evolution history.
    A web UI on top of this API is planned for v0.2.0; until then, consume the
    endpoints directly or wire them into your own Grafana / Datadog stack.[/dim]
    """
    import uvicorn

    from forge_observe.exporters.api import app as api_app

    console.print(f"\n  [{FORGE_ORANGE}]⚡ Forge Observe API[/]")
    console.print(f"  [{FORGE_TEAL}]OpenAPI:[/] http://{host}:{port}/docs")
    console.print(f"  [{FORGE_TEAL}]SSE stream:[/] http://{host}:{port}/api/v1/stream\n")

    if open_browser:
        import webbrowser

        webbrowser.open(f"http://{host}:{port}/docs")

    uvicorn.run(api_app, host=host, port=port, log_level="info")

"""Forge exposed as an MCP server.

This module turns Forge into an MCP *server* — any MCP-compatible client
(Claude Desktop, Claude Code, custom agents) can connect and call Forge's
capabilities as first-class tools:

    - ``query_memory``       — semantic search across all past agent runs
    - ``get_evolution_journal`` — inspect the mutation history and fitness scores
    - ``run_agent_flow``     — execute a flow via Forge and get cost + output

Contrast with :class:`~forge_mcp.server.MetaMCPServer`, which is a
meta-orchestrator for tool calls that Forge agents make *to external* MCP
servers. That's the opposite direction. This module is for clients that
want to *call Forge* over MCP.

Usage (programmatic)
--------------------
    from forge_mcp.forge_server import ForgeMCPServer
    from forge_core.harness import MetaOrchestrator

    orchestrator = MetaOrchestrator()
    srv = ForgeMCPServer(orchestrator=orchestrator)
    await srv.serve_stdio()   # or srv.serve_sse(host=..., port=...)

Usage (CLI)
-----------
    forge mcp serve [--host 0.0.0.0] [--port 8765] [--stdio]

Dependencies
------------
Requires the ``[sdk]`` extra:
    pip install "forge-mcp[sdk]"
    # or:
    pip install "forge-os[mcp]"
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from forge_core.harness import MetaOrchestrator
    from forge_memory.hybrid import HybridMemory

logger = structlog.get_logger()


class ForgeMCPServer:
    """MCP server that exposes Forge's capabilities as tools.

    Instantiate with a :class:`~forge_core.harness.MetaOrchestrator` (and
    optionally a :class:`~forge_memory.hybrid.HybridMemory`). Call one of
    the transport methods to start serving:

    - :meth:`serve_stdio` — stdin/stdout transport (for Claude Desktop / MCP CLI)
    - :meth:`serve_sse`   — HTTP + SSE transport (for web clients)

    All tool handlers are async-safe and share no mutable state beyond what
    the injected ``orchestrator`` already owns.
    """

    def __init__(
        self,
        *,
        orchestrator: MetaOrchestrator | None = None,
        memory: HybridMemory | None = None,
        server_name: str = "forge",
    ) -> None:
        self._orchestrator = orchestrator
        self._memory = memory
        self._server_name = server_name

    # ------------------------------------------------------------------
    # Transport entry points
    # ------------------------------------------------------------------

    async def serve_stdio(self) -> None:
        """Serve over stdin/stdout — the standard MCP desktop transport."""
        app = self._build_app()
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    async def serve_sse(self, *, host: str = "0.0.0.0", port: int = 8765) -> None:
        """Serve over HTTP + SSE — suitable for remote / web clients."""
        app = self._build_app()
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.routing import Mount, Route

        sse_transport = SseServerTransport("/messages")

        async def handle_sse(request: Any) -> Any:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await app.run(streams[0], streams[1], app.create_initialization_options())

        starlette_app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages", app=sse_transport.handle_post_message),
            ]
        )
        import uvicorn

        config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        logger.info("forge_mcp_server.starting", host=host, port=port)
        await server.serve()

    # ------------------------------------------------------------------
    # App factory
    # ------------------------------------------------------------------

    def _build_app(self) -> Any:
        """Construct and return the mcp.server.Server with all tools registered."""
        try:
            from mcp.server import Server
        except ImportError as exc:
            raise ImportError(
                "forge-mcp[sdk] is required to run the Forge MCP server. "
                "Install with: pip install 'forge-mcp[sdk]'"
            ) from exc

        app: Any = Server(self._server_name)

        # ----------------------------------------------------------------
        # Tool: query_memory
        # ----------------------------------------------------------------

        @app.tool()
        async def query_memory(query: str, top_k: int = 5) -> list[dict[str, Any]]:
            """Query Forge's living memory across all past agent runs.

            Returns up to ``top_k`` entries ranked by relevance, each with:
            id, score, content, tags, version, source_run_id.
            """
            if self._memory is None:
                return [{"error": "No memory backend attached to this Forge MCP server."}]

            from forge_core.types import MemoryQuery

            results = await self._memory.query(MemoryQuery(text=query, top_k=top_k))
            return [
                {
                    "id": entry.id,
                    "score": float(score),
                    "content": entry.content,
                    "tags": dict(entry.tags),
                    "version": entry.version,
                    "source_run_id": entry.source_run_id,
                }
                for entry, score in results
            ]

        # ----------------------------------------------------------------
        # Tool: get_evolution_journal
        # ----------------------------------------------------------------

        @app.tool()
        async def get_evolution_journal(limit: int = 10) -> list[dict[str, Any]]:
            """Get the last N mutation entries from the Forge evolution journal.

            Each entry describes a proposed or applied mutation: kind, rationale,
            confidence, fitness_before/after, and whether it was rolled back.
            """
            if self._orchestrator is None:
                return [{"error": "No orchestrator attached to this Forge MCP server."}]

            mutations = await self._orchestrator.get_evolution_journal(limit=limit)
            out = []
            for m in mutations:
                entry: dict[str, Any] = {
                    "id": m.id,
                    "kind": m.kind,
                    "description": m.description,
                    "rationale": m.rationale,
                    "confidence": m.confidence,
                    "applied": m.applied,
                }
                if m.fitness_before:
                    entry["fitness_before"] = m.fitness_before.overall
                if m.fitness_after:
                    entry["fitness_after"] = m.fitness_after.overall
                out.append(entry)
            return out

        # ----------------------------------------------------------------
        # Tool: run_agent_flow
        # ----------------------------------------------------------------

        @app.tool()
        async def run_agent_flow(
            input: dict[str, Any],
            flow_path: str = "",
            tags: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            """Execute an agent flow via Forge and return the result.

            Returns output, cost_usd, duration_ms, status, and any errors.
            If ``flow_path`` is provided, Forge loads the flow first.
            """
            if self._orchestrator is None:
                return {"error": "No orchestrator attached to this Forge MCP server."}

            from forge_core.types import TaskEnvelope

            if flow_path:
                try:
                    await self._orchestrator.load(flow_path)
                except Exception as exc:
                    return {"error": f"Failed to load flow '{flow_path}': {exc}"}

            envelope = TaskEnvelope(input=input, tags=tags or {})
            try:
                result = await self._orchestrator.run(envelope)
            except Exception as exc:
                return {"error": f"Run failed: {exc}"}

            return {
                "output": result.output,
                "status": result.status,
                "cost_usd": str(result.cost.total_cost),
                "duration_ms": result.duration_ms,
                "run_id": result.run_id,
                "errors": result.errors,
            }

        return app


def build_server(
    *,
    orchestrator: MetaOrchestrator | None = None,
    memory: HybridMemory | None = None,
    server_name: str = "forge",
) -> ForgeMCPServer:
    """Factory for :class:`ForgeMCPServer` — convenience alias for the CLI."""
    return ForgeMCPServer(orchestrator=orchestrator, memory=memory, server_name=server_name)

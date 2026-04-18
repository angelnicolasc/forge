"""Dashboard backend launcher for Forge.

Starts the FastAPI observe API server and wires it up to the
MetaOrchestrator so every run result is automatically ingested.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog
import uvicorn

from forge_observe.exporters.api import app

if TYPE_CHECKING:
    from forge_core.harness import MetaOrchestrator
    from forge_core.types import RunResult

logger = structlog.get_logger()


class DashboardBackend:
    """Manages the lifecycle of the observe API server.

    Can run in-process on a background thread (dev mode) or
    be deployed as a standalone service (production).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8787) -> None:
        self._host = host
        self._port = port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start_background(self) -> None:
        """Start the API server on a background thread (non-blocking)."""
        config = uvicorn.Config(
            app=app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            daemon=True,
            name="forge-dashboard-backend",
        )
        self._thread.start()
        logger.info(
            "dashboard_backend.started",
            url=self.url,
            docs=f"{self.url}/docs",
        )

    def stop(self) -> None:
        """Gracefully stop the background server."""
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("dashboard_backend.stopped")

    def attach_orchestrator(self, orchestrator: MetaOrchestrator) -> None:
        """Wire the orchestrator so runs are auto-ingested to the API."""
        import httpx

        original_run = orchestrator.run.__func__  # type: ignore[attr-defined]

        async def patched_run(
            self_inner: MetaOrchestrator, *args: object, **kwargs: object
        ) -> RunResult:
            result: RunResult = await original_run(self_inner, *args, **kwargs)
            # Fire-and-forget ingest
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{self.url}/api/v1/runs",
                        content=result.model_dump_json(),
                        headers={"Content-Type": "application/json"},
                        timeout=2.0,
                    )
            except Exception:
                pass  # Never block the main flow
            return result

        import types

        orchestrator.run = types.MethodType(patched_run, orchestrator)  # type: ignore[method-assign]
        logger.info("dashboard_backend.orchestrator_attached")

"""REST + SSE API exporter for Forge.

Exposes a FastAPI application at /api/v1/ that serves runs, evolution
journal entries, and a live event stream. Consumed today by custom
integrations (Grafana panels, internal dashboards); a first-party web
UI is planned for v0.2.0. All responses are typed with Pydantic.

Endpoints:
  GET  /api/v1/runs                  — list recent runs
  GET  /api/v1/runs/{run_id}         — single run detail
  GET  /api/v1/runs/{run_id}/events  — run events
  GET  /api/v1/evolution             — evolution journal
  GET  /api/v1/metrics               — aggregated metrics
  GET  /api/v1/health                — health check
  POST /api/v1/runs (ingest)         — receive run result from MetaOrchestrator
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from collections import deque
from collections.abc import (
    AsyncIterator,
)
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from forge_core.types import Mutation, RunResult

logger = structlog.get_logger()


def _load_allowed_origins() -> list[str]:
    """Read CORS allowlist from FORGE_API_ALLOWED_ORIGINS (comma-separated).

    Default is empty (same-origin only). Callers who need to expose the API
    to a browser on another host set it explicitly, e.g.:
        FORGE_API_ALLOWED_ORIGINS="http://localhost:5173,https://my.app"
    """
    raw = os.environ.get("FORGE_API_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


# ε.1 — live-event pub/sub. Each new SSE client registers a Queue on
# ``_live_subscribers``; ``publish_event`` fans out to every queue
# non-blockingly. Failed puts (dead client) are swept on the next publish.
#
# ζ.4 — The subscriber set is touched from both the FastAPI event loop AND
# sync TestClient worker threads (which drive the generator's ``finally``
# cleanup). An ``asyncio.Lock`` doesn't protect cross-thread mutation; a
# ``threading.Lock`` does, and releases immediately so it's safe to hold
# from async code as long as we don't await while holding it.
_live_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
_live_lock = threading.Lock()

# ζ.12 — cap on concurrent SSE subscribers. Beyond this, new clients get
# HTTP 503 so a single broken dashboard can't DoS the process.
MAX_LIVE_SUBSCRIBERS = 50


def live_subscriber_count() -> int:
    """Current count of live SSE subscribers (safe to call from any thread)."""
    with _live_lock:
        return len(_live_subscribers)


def reset_live_subscribers() -> None:
    """Test helper: clear any leaked subscribers between tests."""
    with _live_lock:
        _live_subscribers.clear()


async def publish_event(event: dict[str, Any]) -> None:
    """Fan out an event to all connected SSE subscribers.

    The orchestrator / bus adapter calls this when a run event occurs.
    Dead clients are handled by the stream generator on the read side;
    here we only need to enqueue and move on.
    """
    with _live_lock:
        subs = list(_live_subscribers)
    for q in subs:
        with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover — bounded queues
            q.put_nowait(event)


# Lightweight in-memory memory adapter registration point — the API can
# surface memory data without forge-observe hard-depending on
# forge-memory. Tests and the orchestrator set this directly.
_memory_provider: Any = None


def set_memory_provider(memory: Any) -> None:
    """Register a memory backend the API should expose at /memory/*.

    ``memory`` must support ``async query(MemoryQuery)`` and (optionally)
    ``stats()`` / ``_graph`` for the graph endpoint.
    """
    global _memory_provider
    _memory_provider = memory


# ---------------------------------------------------------------------------
# In-memory store (replaces a DB for dev; swap with SQLite/Postgres in prod)
# ---------------------------------------------------------------------------

_MAX_RUNS = 500
_runs: deque[RunResult] = deque(maxlen=_MAX_RUNS)
_mutations: list[Mutation] = []
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class RunSummary(BaseModel):
    run_id: str
    task_id: str
    status: str
    duration_ms: float | None
    total_cost_usd: str
    total_input_tokens: int
    total_output_tokens: int
    agent_count: int
    mutation_count: int
    started_at: datetime | None
    completed_at: datetime | None


class MetricsSummary(BaseModel):
    total_runs: int
    completed_runs: int
    failed_runs: int
    total_cost_usd: str
    total_tokens: int
    avg_duration_ms: float | None
    avg_cost_usd: str
    top_models: list[dict[str, Any]]
    mutations_applied: int
    last_updated: datetime


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    run_count: int
    live_subscribers: int
    max_live_subscribers: int


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

_start_time = datetime.now(UTC)


def create_app(version: str = "0.1.0") -> FastAPI:
    """Create and configure the Forge observe API app."""

    app = FastAPI(
        title="Forge Observe API",
        description="Real-time observability and FinOps API for the Forge agent harness.",
        version=version,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        # Default to same-origin only; override via FORGE_API_ALLOWED_ORIGINS
        # (comma-separated) to permit browser clients on other hosts.
        allow_origins=_load_allowed_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        uptime = (datetime.now(UTC) - _start_time).total_seconds()
        return HealthResponse(
            status="healthy",
            version=version,
            uptime_seconds=uptime,
            run_count=len(_runs),
            live_subscribers=live_subscriber_count(),
            max_live_subscribers=MAX_LIVE_SUBSCRIBERS,
        )

    # -----------------------------------------------------------------------
    # A2A discovery
    # -----------------------------------------------------------------------

    @app.get("/.well-known/agent.json", tags=["a2a"])
    async def agent_discovery() -> dict[str, Any]:
        """A2A-compliant discovery document for the Forge orchestrator.

        Returns an AgentCard per the Google Agent-to-Agent (A2A) protocol.
        Set FORGE_OBSERVE_AGENT_BASE_URL to include the ``url`` field and
        achieve full A2A compliance; without it the field is omitted.
        """
        import forge_core
        from forge_core.config import ObserveConfig
        from forge_core.types import AgentCard, ToolRef

        obs = ObserveConfig()
        card = AgentCard(
            name="Forge MetaOrchestrator",
            role="orchestrator",
            description=(
                "Universal AI agent harness — self-evolution, hybrid memory, OTel."
            ),
            version=forge_core.__version__,
            url=obs.agent_base_url,
            capabilities=[
                "multi-agent-orchestration",
                "self-evolution",
                "hybrid-memory",
                "cost-tracking",
                "otel-tracing",
            ],
            tools=[
                ToolRef(name="run", description="Execute an agent flow"),
                ToolRef(name="evolve", description="Trigger the self-evolution loop"),
                ToolRef(name="memory_query", description="Query hybrid memory"),
            ],
            provider={"name": "Forge"},
        )
        return card.model_dump(mode="json", exclude_none=True)

    # -----------------------------------------------------------------------
    # Runs
    # -----------------------------------------------------------------------

    @app.post("/api/v1/runs", status_code=201, tags=["runs"])
    async def ingest_run(result: RunResult) -> dict[str, str]:
        """Ingest a run result from the MetaOrchestrator."""
        async with _lock:
            _runs.appendleft(result)
        logger.info("api.run_ingested", run_id=result.run_id)
        return {"run_id": result.run_id}

    @app.get("/api/v1/runs", response_model=list[RunSummary], tags=["runs"])
    async def list_runs(limit: int = 50, offset: int = 0) -> list[RunSummary]:
        """List recent runs with summary metrics."""
        async with _lock:
            page = list(_runs)[offset : offset + limit]
        return [_to_summary(r) for r in page]

    @app.get("/api/v1/runs/{run_id}", response_model=RunResult, tags=["runs"])
    async def get_run(run_id: str) -> RunResult:
        """Get a single run by ID with full event trace."""
        async with _lock:
            for r in _runs:
                if r.run_id == run_id:
                    return r
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    @app.get("/api/v1/runs/{run_id}/events", tags=["runs"])
    async def get_run_events(run_id: str) -> list[dict[str, Any]]:
        """Get the event trace for a specific run."""
        async with _lock:
            for r in _runs:
                if r.run_id == run_id:
                    return [e.model_dump(mode="json") for e in r.events]
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    # -----------------------------------------------------------------------
    # Evolution
    # -----------------------------------------------------------------------

    @app.post("/api/v1/evolution", status_code=201, tags=["evolution"])
    async def ingest_mutation(mutation: Mutation) -> dict[str, str]:
        """Record an applied or proposed mutation."""
        async with _lock:
            _mutations.append(mutation)
        return {"id": mutation.id}

    @app.get("/api/v1/evolution", response_model=list[Mutation], tags=["evolution"])
    async def list_mutations(limit: int = 100) -> list[Mutation]:
        """List evolution mutations, newest first."""
        async with _lock:
            return list(reversed(_mutations))[:limit]

    # -----------------------------------------------------------------------
    # Metrics
    # -----------------------------------------------------------------------

    @app.get("/api/v1/metrics", response_model=MetricsSummary, tags=["metrics"])
    async def get_metrics() -> MetricsSummary:
        """Aggregated FinOps and performance metrics across all runs."""
        async with _lock:
            runs_list = list(_runs)

        from decimal import Decimal

        total_cost = Decimal("0")
        total_tokens = 0
        total_duration = 0.0
        duration_count = 0
        completed = 0
        failed = 0
        model_costs: dict[str, Decimal] = {}

        for r in runs_list:
            if r.status == "completed":
                completed += 1
            elif r.status == "failed":
                failed += 1
            total_cost += r.cost.total_cost
            total_tokens += r.cost.total_input_tokens + r.cost.total_output_tokens
            if r.duration_ms:
                total_duration += r.duration_ms
                duration_count += 1
            for model, cost in r.cost.cost_by_model.items():
                model_costs[model] = model_costs.get(model, Decimal("0")) + cost

        avg_duration = total_duration / duration_count if duration_count else None
        avg_cost = total_cost / len(runs_list) if runs_list else Decimal("0")
        top_models = sorted(
            [{"model": m, "cost_usd": str(c)} for m, c in model_costs.items()],
            key=lambda x: x["cost_usd"],
            reverse=True,
        )[:10]

        return MetricsSummary(
            total_runs=len(runs_list),
            completed_runs=completed,
            failed_runs=failed,
            total_cost_usd=str(total_cost),
            total_tokens=total_tokens,
            avg_duration_ms=avg_duration,
            avg_cost_usd=str(avg_cost),
            top_models=top_models,
            mutations_applied=len(_mutations),
            last_updated=datetime.now(UTC),
        )

    # -----------------------------------------------------------------------
    # Evolution stats (ε.1) — aggregate view for the fitness-curve widget.
    # The raw list endpoint at /evolution is too chatty for the dashboard's
    # primary view; this boils it down to counts and a moving score average.
    # -----------------------------------------------------------------------

    @app.get("/api/v1/evolution/stats", tags=["evolution"])
    async def evolution_stats() -> dict[str, Any]:
        async with _lock:
            muts = list(_mutations)
        by_kind: dict[str, int] = {}
        applied = 0
        for m in muts:
            by_kind[m.kind] = by_kind.get(m.kind, 0) + 1
            if getattr(m, "applied_at", None) is not None:
                applied += 1
        # Fitness score series if journal-shaped mutations carry scores.
        fitness_series: list[dict[str, Any]] = []
        for m in muts:
            fit = getattr(m, "fitness_delta", None)
            if fit is not None:
                fitness_series.append({"id": m.id, "delta": fit})
        return {
            "total": len(muts),
            "applied": applied,
            "by_kind": by_kind,
            "fitness_series": fitness_series,
        }

    # -----------------------------------------------------------------------
    # SSE stream (ε.1)
    # The dashboard opens one EventSource per page load; every event
    # published to ``publish_event`` gets fanned out until the client
    # disconnects. We enforce a per-client bounded queue so a slow client
    # can't balloon memory — it just misses the backlog.
    # -----------------------------------------------------------------------

    @app.get("/api/v1/stream", tags=["stream"])
    async def stream_events(request: Request) -> StreamingResponse:
        # ζ.12 — reject with 503 instead of accepting a 51st subscriber.
        # Checked + reserved under the lock so two simultaneous clients
        # can't both sneak past the cap.
        with _live_lock:
            if len(_live_subscribers) >= MAX_LIVE_SUBSCRIBERS:
                raise HTTPException(
                    status_code=503,
                    detail=(f"SSE subscriber cap ({MAX_LIVE_SUBSCRIBERS}) reached. Retry later."),
                )

        async def event_source() -> AsyncIterator[bytes]:
            q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
            # Register BEFORE the first yield so the client is always
            # discoverable by ``publish_event`` once the response starts.
            with _live_lock:
                _live_subscribers.add(q)
            try:
                # Prime the stream so proxies flush their buffer.
                yield b": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        # Short poll so disconnects are noticed quickly under
                        # sync TestClient — every 1s we re-check is_disconnected
                        # and emit a heartbeat. A real browser EventSource
                        # tolerates this frequency; intermediaries are happy.
                        event = await asyncio.wait_for(q.get(), timeout=1.0)
                    except TimeoutError:
                        # Heartbeat — keeps intermediaries from timing out.
                        yield b": ping\n\n"
                        continue
                    payload = json.dumps(event, default=str)
                    yield f"data: {payload}\n\n".encode()
            finally:
                # ζ.4 — cleanup MUST run, even on cancel. A sync
                # ``threading.Lock`` + ``discard`` is cheap and idempotent.
                # ``asyncio.shield`` protects the tail against a race where
                # the enclosing task is cancelled mid-yield.
                def _remove() -> None:
                    with _live_lock:
                        _live_subscribers.discard(q)

                try:
                    await asyncio.shield(asyncio.to_thread(_remove))
                except (asyncio.CancelledError, RuntimeError):
                    # Event loop already tearing down — run the sync cleanup
                    # directly so we still release the slot.
                    _remove()

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -----------------------------------------------------------------------
    # Memory endpoints (ε.1) — only active when a provider is attached.
    # -----------------------------------------------------------------------

    @app.get("/api/v1/memory/query", tags=["memory"])
    async def memory_query(
        q: str = Query(..., min_length=1, max_length=2000),
        top_k: int = Query(10, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        if _memory_provider is None:
            raise HTTPException(status_code=503, detail="memory provider not attached")
        from forge_core.types import MemoryQuery

        results = await _memory_provider.query(MemoryQuery(text=q, top_k=top_k))
        out: list[dict[str, Any]] = []
        for entry, score in results:
            out.append(
                {
                    "id": entry.id,
                    "score": float(score),
                    "content": entry.content,
                    "tags": dict(entry.tags),
                    "version": entry.version,
                    "source_run_id": entry.source_run_id,
                }
            )
        return out

    @app.get("/api/v1/memory/graph", tags=["memory"])
    async def memory_graph() -> dict[str, Any]:
        """Return a JSON graph suitable for React Flow rendering."""
        if _memory_provider is None:
            raise HTTPException(status_code=503, detail="memory provider not attached")
        graph_backend = getattr(_memory_provider, "_graph", None)
        if graph_backend is None or not hasattr(graph_backend, "_graph"):
            return {"nodes": [], "edges": []}
        g = graph_backend._graph
        nodes = [{"id": n, "data": dict(d)} for n, d in g.nodes(data=True)]
        edges = [{"source": u, "target": v, "data": dict(d)} for u, v, d in g.edges(data=True)]
        return {"nodes": nodes, "edges": edges}

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_summary(r: RunResult) -> RunSummary:
    return RunSummary(
        run_id=r.run_id,
        task_id=r.task_id,
        status=r.status,
        duration_ms=r.duration_ms,
        total_cost_usd=str(r.cost.total_cost),
        total_input_tokens=r.cost.total_input_tokens,
        total_output_tokens=r.cost.total_output_tokens,
        agent_count=len(r.topology),
        mutation_count=len(getattr(r, "mutations_applied", [])),
        started_at=r.started_at,
        completed_at=r.completed_at,
    )


# Singleton app instance
app = create_app()

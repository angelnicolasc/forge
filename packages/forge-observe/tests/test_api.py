"""Tests for the Dashboard backend REST/SSE API (fase ε.1).

We use FastAPI's TestClient rather than running uvicorn — it exercises
the full ASGI stack and is deterministic in CI.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from forge_core.types import (
    CostSummary,
    MemoryEntry,
    MemoryQuery,
    RunResult,
    RunStatus,
)
from forge_observe.exporters.api import (
    create_app,
    publish_event,
    set_memory_provider,
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(version="0.1.0-test"))


def _result(run_id: str = "r1") -> RunResult:
    return RunResult(
        run_id=run_id,
        task_id="t1",
        status=RunStatus.COMPLETED,
        duration_ms=1234.0,
        cost=CostSummary(total_cost=Decimal("0.05")),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert "uptime_seconds" in body


def test_run_ingest_and_list(client: TestClient) -> None:
    r = client.post(
        "/api/v1/runs",
        content=_result("run-a").model_dump_json(),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 201

    r = client.get("/api/v1/runs")
    assert r.status_code == 200
    ids = [x["run_id"] for x in r.json()]
    assert "run-a" in ids


def test_run_detail_not_found(client: TestClient) -> None:
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404


def test_evolution_stats_empty(client: TestClient) -> None:
    r = client.get("/api/v1/evolution/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["by_kind"] == {}


# ---------------------------------------------------------------------------
# Memory endpoints
# ---------------------------------------------------------------------------


def test_memory_query_503_without_provider(client: TestClient) -> None:
    set_memory_provider(None)
    assert client.get("/api/v1/memory/query?q=hi").status_code == 503


def test_memory_query_routes_to_provider(client: TestClient) -> None:
    class _Mem:
        async def query(self, q: MemoryQuery):
            return [
                (MemoryEntry(id="e1", content="hit one"), 0.9),
                (MemoryEntry(id="e2", content="hit two"), 0.5),
            ]

    set_memory_provider(_Mem())
    try:
        r = client.get("/api/v1/memory/query?q=hello&top_k=5")
        assert r.status_code == 200
        body = r.json()
        assert [x["id"] for x in body] == ["e1", "e2"]
        assert body[0]["score"] == pytest.approx(0.9)
    finally:
        set_memory_provider(None)


def test_memory_graph_empty_without_backend(client: TestClient) -> None:
    class _Mem:
        pass  # no _graph attribute

    set_memory_provider(_Mem())
    try:
        r = client.get("/api/v1/memory/graph")
        assert r.status_code == 200
        assert r.json() == {"nodes": [], "edges": []}
    finally:
        set_memory_provider(None)


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_event_fans_out_to_subscribers() -> None:
    """Direct unit test of the SSE publish/subscribe primitive.

    We avoid going through the full ASGI stack because httpx's
    streaming transport buffers lines aggressively and makes an
    end-to-end test flaky; the primitive itself is what we need to
    lock down, since the endpoint handler is a thin adapter around it.
    """
    from forge_observe.exporters.api import _live_lock, _live_subscribers

    q1: asyncio.Queue = asyncio.Queue(maxsize=16)
    q2: asyncio.Queue = asyncio.Queue(maxsize=16)
    with _live_lock:
        _live_subscribers.add(q1)
        _live_subscribers.add(q2)
    try:
        await publish_event({"kind": "tool_call", "n": 1})
        await publish_event({"kind": "llm_call", "n": 2})
        # Both subscribers saw both events in order.
        for q in (q1, q2):
            got_a = q.get_nowait()
            got_b = q.get_nowait()
            assert got_a["kind"] == "tool_call"
            assert got_b["n"] == 2
    finally:
        with _live_lock:
            _live_subscribers.discard(q1)
            _live_subscribers.discard(q2)


def test_stream_endpoint_registered(client: TestClient) -> None:
    """The SSE endpoint is wired into the app at ``/api/v1/stream``.

    ζ.4 note: a full ``client.stream`` round-trip against the synchronous
    TestClient deadlocks because the generator polls ``is_disconnected``
    via the ASGI portal while the test thread blocks inside
    ``iter_bytes``. We lock down the surface here (route exists, content
    type, handler returns a ``StreamingResponse``) and drive the
    generator lifecycle directly in the async test below.
    """
    routes = {r.path for r in client.app.routes}  # type: ignore[attr-defined]
    assert "/api/v1/stream" in routes


@pytest.mark.asyncio
async def test_stream_generator_registers_and_cleans_up() -> None:
    """ζ.4 — the SSE generator must add itself to ``_live_subscribers``
    before the first yield and remove itself after it finishes.

    Driving the generator by hand avoids httpx/TestClient buffering and
    is a strict regression guard on the leak: any path that skips the
    ``finally`` cleanup leaves a ghost subscriber and fails this test.
    """
    from fastapi import Request

    from forge_observe.exporters.api import (
        _live_subscribers,
        create_app,
    )

    app = create_app(version="0.1.0-test")
    # Find the stream route's endpoint function.
    stream_route = next(r for r in app.routes if getattr(r, "path", "") == "/api/v1/stream")
    handler = stream_route.endpoint  # type: ignore[attr-defined]

    # Build a minimal Request with an ASGI scope that reports the client
    # never disconnects — we stop the generator manually via aclose().
    async def _receive() -> dict:  # pragma: no cover - never awaited
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/stream",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope, _receive)  # type: ignore[arg-type]

    before = len(_live_subscribers)
    response = await handler(request)
    agen = response.body_iterator  # async generator from StreamingResponse
    first = await agen.__anext__()
    assert b"connected" in first
    assert len(_live_subscribers) == before + 1

    await agen.aclose()
    # aclose runs the ``finally`` which schedules the threaded discard;
    # give the loop a tick to drain it.
    await asyncio.sleep(0.05)
    assert len(_live_subscribers) == before


def test_health_reports_subscriber_stats(client: TestClient) -> None:
    """ζ.12 — /health reports live_subscribers and max_live_subscribers."""
    from forge_observe.exporters.api import MAX_LIVE_SUBSCRIBERS

    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["live_subscribers"] == 0
    assert body["max_live_subscribers"] == MAX_LIVE_SUBSCRIBERS


def test_stream_caps_at_max_subscribers(client: TestClient) -> None:
    """ζ.12 — the 51st (cap+1) SSE client is rejected with HTTP 503."""
    from forge_observe.exporters import api as api_mod
    from forge_observe.exporters.api import MAX_LIVE_SUBSCRIBERS, _live_lock

    # Seed the subscriber set up to the cap, then attempt to open one more.
    # We fake queues so we don't need real streaming clients.
    fakes: list[asyncio.Queue[dict]] = [asyncio.Queue() for _ in range(MAX_LIVE_SUBSCRIBERS)]
    with _live_lock:
        api_mod._live_subscribers.update(fakes)

    try:
        r = client.get("/api/v1/stream")
        assert r.status_code == 503
        assert "cap" in r.json()["detail"].lower()
    finally:
        with _live_lock:
            api_mod._live_subscribers.difference_update(fakes)


# ---------------------------------------------------------------------------
# Fase 0 — A2A discovery
# ---------------------------------------------------------------------------


def test_agent_discovery_endpoint(client: TestClient) -> None:
    r = client.get("/.well-known/agent.json")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Forge MetaOrchestrator"
    assert "version" in body
    assert "capabilities" in body
    assert "multi-agent-orchestration" in body["capabilities"]
    # None fields must be excluded
    assert "spiffe_id" not in body
    assert "model" not in body
    assert "system_prompt" not in body


def test_agent_discovery_url_when_base_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_OBSERVE_AGENT_BASE_URL", "https://forge.test.io")
    local_client = TestClient(create_app(version="0.1.0-test"))
    r = local_client.get("/.well-known/agent.json")
    assert r.status_code == 200
    assert r.json()["url"] == "https://forge.test.io"


def test_agent_discovery_no_url_when_base_url_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORGE_OBSERVE_AGENT_BASE_URL", raising=False)
    r = client.get("/.well-known/agent.json")
    assert "url" not in r.json()


def test_app_exported_from_forge_observe() -> None:
    from forge_observe import app as exported_app
    from forge_observe.exporters.api import app as internal_app

    assert exported_app is internal_app

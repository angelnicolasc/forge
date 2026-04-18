"""ζ.5a — CircuitBreaker wired into ForgeLLMInterceptor.

We don't mock an LLM here; we drive the interceptor's breaker directly
via ``on_llm_error`` / ``on_llm_end`` and assert the observable
behavior: after N consecutive errors the next ``on_llm_start`` publishes
an ``ERROR`` event with ``circuit_open`` and returns a sentinel span id
that the end/error hooks treat as a no-op.
"""

from __future__ import annotations

import pytest

from forge_core.circuit_breaker import CircuitBreaker, CircuitState
from forge_core.events import EventBus
from forge_core.types import RunEvent, RunEventKind
from forge_observe.interceptor import _BREAKER_OPEN_SPAN, ForgeLLMInterceptor


class _Collector:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def handle(self, event: RunEvent) -> None:
        self.events.append(event)


@pytest.fixture
def bus_and_collector() -> tuple[EventBus, _Collector]:
    bus = EventBus()
    collector = _Collector()
    bus.subscribe("*", collector.handle)
    return bus, collector


@pytest.mark.asyncio
async def test_interceptor_opens_breaker_after_five_failures(
    bus_and_collector: tuple[EventBus, _Collector],
) -> None:
    """Five consecutive on_llm_error calls trip the breaker; the sixth
    on_llm_start short-circuits and publishes a ``circuit_open`` ERROR.
    """
    bus, collector = bus_and_collector
    breaker = CircuitBreaker("llm_api", failure_threshold=5, recovery_timeout_seconds=30.0)
    interceptor = ForgeLLMInterceptor(bus=bus, breaker=breaker)

    # Drive five consecutive failures — each allocates a real span
    # because the breaker is still CLOSED during these calls.
    for _ in range(5):
        span_id = await interceptor.on_llm_start(model="m1", agent_id="a1")
        assert span_id != _BREAKER_OPEN_SPAN
        await interceptor.on_llm_error(span_id, RuntimeError("boom"))

    assert breaker.state is CircuitState.OPEN

    # Clear the events we've collected so the next assertion is unambiguous.
    collector.events.clear()

    # The next on_llm_start must short-circuit: return sentinel and
    # publish exactly one ``circuit_open`` ERROR.
    span_id = await interceptor.on_llm_start(model="m1", agent_id="a1")
    assert span_id == _BREAKER_OPEN_SPAN

    error_events = [e for e in collector.events if e.kind == RunEventKind.ERROR]
    assert len(error_events) == 1
    assert "circuit_open" in (error_events[0].error or "")

    # Downstream on_llm_end with the sentinel is a no-op — no extra events.
    collector.events.clear()
    await interceptor.on_llm_end(_BREAKER_OPEN_SPAN, input_tokens=1, output_tokens=1)
    assert collector.events == []


@pytest.mark.asyncio
async def test_interceptor_success_resets_failure_counter(
    bus_and_collector: tuple[EventBus, _Collector],
) -> None:
    """One success inside the grace window resets the breaker's counter —
    an intermittent LLM hiccup shouldn't trip it.
    """
    bus, _ = bus_and_collector
    breaker = CircuitBreaker("llm_api", failure_threshold=3, recovery_timeout_seconds=30.0)
    interceptor = ForgeLLMInterceptor(bus=bus, breaker=breaker)

    for _ in range(2):
        sid = await interceptor.on_llm_start(model="m1")
        await interceptor.on_llm_error(sid, RuntimeError("transient"))
    assert breaker.failure_count == 2
    assert breaker.state is CircuitState.CLOSED

    # A successful call resets the counter.
    sid = await interceptor.on_llm_start(model="m1")
    await interceptor.on_llm_end(sid, input_tokens=10, output_tokens=20)

    assert breaker.failure_count == 0
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_interceptor_defaults_to_breaker_on(monkeypatch) -> None:
    """Without explicit args and with the flag unset, a breaker is wired."""
    monkeypatch.delenv("FORGE_LLM_CIRCUIT_BREAKER", raising=False)
    interceptor = ForgeLLMInterceptor(bus=EventBus())
    assert interceptor._breaker is not None


@pytest.mark.asyncio
async def test_interceptor_flag_off_disables_breaker(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_LLM_CIRCUIT_BREAKER", "0")
    interceptor = ForgeLLMInterceptor(bus=EventBus())
    assert interceptor._breaker is None

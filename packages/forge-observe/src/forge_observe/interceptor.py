"""Canonical :class:`LLMCallInterceptor` implementation.

This is the single place in the entire Forge codebase where raw LLM
telemetry (model name, input/output tokens, output payload) becomes a
first-class :class:`~forge_core.types.RunEvent` with costs populated and an
OpenTelemetry span properly nested under the active run span.

Every adapter — LangGraph's callback handler, CrewAI's monkey-patched
``Agent.execute_task``, the AutoGen log handler, the generic SDK wrappers —
calls into this one class. That centralization is what prevents the "costs
are always $0" regression: if you bypass the interceptor, nothing computes
the cost, and the adapter's tests (which assert on non-zero cost via the
EventBus) fail loudly.

Why the bus instead of returning events?
------------------------------------------
Some frameworks (LangChain callbacks, AutoGen trace logs) invoke us from
contexts where the adapter isn't looping collecting events — there's no
return path. Publishing to the bus lets the cost tracker, tracer, memory
ingester, and any number of other observers receive the same data without
each framework needing bespoke plumbing.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from forge_core.circuit_breaker import CircuitBreaker, CircuitOpenError
from forge_core.context import current_run
from forge_core.types import RunEvent, RunEventKind
from forge_observe.cost_model import DefaultCostModel

if TYPE_CHECKING:
    from forge_core.events import EventBus

logger = structlog.get_logger()

# Sentinel span id returned from ``on_llm_start`` while the breaker is OPEN.
# ``on_llm_end`` detects it and no-ops, so a caller that dutifully calls
# start/end never sees the breaker short-circuit — the event they'd
# normally publish simply collapses into the already-emitted ERROR.
_BREAKER_OPEN_SPAN = "__forge_breaker_open__"


def _breaker_flag_enabled() -> bool:
    """``FORGE_LLM_CIRCUIT_BREAKER`` defaults to ON (ζ.5a).

    This is the only breaker we default ON — there is no downside to
    short-circuiting a dead LLM endpoint, and the alternative is cascading
    timeouts flooding the event bus.
    """
    val = os.environ.get("FORGE_LLM_CIRCUIT_BREAKER", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


class ForgeLLMInterceptor:
    """Reference implementation of the ``LLMCallInterceptor`` protocol.

    A single instance is typically owned by the ``MetaOrchestrator`` and
    handed to adapters via dependency injection. Adapters do not construct
    their own interceptor — that would defeat centralized cost tracking.

    Parameters:
        bus: The ``EventBus`` that receives populated ``LLM_CALL`` events.
        cost_model: Supplies per-model pricing. Defaults to
            :class:`DefaultCostModel` (see :mod:`forge_observe.cost_model`).
        tracer: OpenTelemetry tracer. If ``None``, uses ``trace.get_tracer("forge")``.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        cost_model: DefaultCostModel | None = None,
        tracer: trace.Tracer | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._bus = bus
        self._cost_model = cost_model or DefaultCostModel()
        self._tracer = tracer or trace.get_tracer("forge")
        # span_id -> (model, start_time_perf, agent_id, tool_name, otel_span)
        # We stash a tiny amount of per-span state so on_llm_end can compute
        # latency and cost without re-plumbing through the adapter layer.
        self._open_spans: dict[str, _SpanState] = {}
        # ζ.5a — LLM circuit breaker. Wraps the async side of calls; the
        # interceptor itself doesn't *make* the call, but it does know when
        # a call failed (``on_llm_error``) and when one succeeded
        # (``on_llm_end``), which is enough to drive the breaker.
        if breaker is None and _breaker_flag_enabled():
            breaker = CircuitBreaker(
                "llm_api",
                failure_threshold=5,
                recovery_timeout_seconds=30.0,
            )
        self._breaker = breaker

    # ------------------------------------------------------------------
    # LLMCallInterceptor contract
    # ------------------------------------------------------------------

    async def on_llm_start(
        self,
        *,
        model: str,
        agent_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Open an OTel span and record the call's start time.

        The returned ``span_id`` is a 16-hex-char token minted by Forge. It
        intentionally does NOT correspond to the OTel span id — we keep
        Forge's identifiers independent so upstream systems (dashboard,
        evolution journal) never need an OTel decoder.
        """
        ctx = current_run()
        run_id = ctx.run_id if ctx is not None else None
        resolved_agent_id = agent_id or (ctx.agent_id if ctx is not None else None)

        # ζ.5a — if the breaker is OPEN, publish a single ERROR event and
        # return a sentinel span id. ``on_llm_end`` / ``on_llm_error`` detect
        # it and no-op, preventing downstream cascade failures from a dead
        # LLM endpoint.
        if self._breaker is not None:
            try:
                await self._breaker._before_call()
            except CircuitOpenError as exc:
                event = RunEvent(
                    kind=RunEventKind.ERROR,
                    run_id=run_id,
                    agent_id=resolved_agent_id,
                    tool_name=tool_name,
                    model=model,
                    error=f"circuit_open: {exc.name} (retry_after={exc.retry_after:.1f}s)",
                )
                await self._bus.publish(event)
                return _BREAKER_OPEN_SPAN

        span_id = uuid4().hex[:16]

        otel_span = self._tracer.start_span(
            name=f"forge.llm_call.{resolved_agent_id or 'unknown'}",
            attributes={
                "forge.span_id": span_id,
                "forge.run_id": run_id or "",
                "forge.model": model,
                "forge.agent_id": resolved_agent_id or "",
                "forge.tool_name": tool_name or "",
            },
        )

        self._open_spans[span_id] = _SpanState(
            model=model,
            start_perf=time.perf_counter(),
            agent_id=resolved_agent_id,
            tool_name=tool_name,
            otel_span=otel_span,
            run_id=run_id,
            parent_span_id=ctx.span_id if ctx is not None else None,
            metadata=dict(metadata or {}),
        )
        return span_id

    async def on_llm_end(
        self,
        span_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        output: Any = None,
        thinking_tokens: int = 0,
        cached_input_tokens: int = 0,
    ) -> None:
        """Close the span, compute cost, and publish an ``LLM_CALL`` event."""
        # ζ.5a — breaker-suppressed start returned the sentinel; its "end"
        # is a no-op (the ERROR was already published).
        if span_id == _BREAKER_OPEN_SPAN:
            return
        # Success ⇒ inform the breaker so it can recover from HALF_OPEN.
        if self._breaker is not None:
            self._breaker.record_success()

        state = self._open_spans.pop(span_id, None)
        if state is None:
            # on_llm_end called with no prior on_llm_start — defensive log;
            # still publish an event so the data isn't lost entirely.
            logger.warning("interceptor.orphan_end", span_id=span_id)
            return

        latency_ms = (time.perf_counter() - state.start_perf) * 1000.0
        cost = self._cost_model.cost(
            state.model, input_tokens, output_tokens,
            thinking_tokens=thinking_tokens,
            cached_input_tokens=cached_input_tokens,
        )

        # Set OTel attributes and close span.
        state.otel_span.set_attribute("forge.input_tokens", input_tokens)
        state.otel_span.set_attribute("forge.output_tokens", output_tokens)
        state.otel_span.set_attribute("forge.thinking_tokens", thinking_tokens)
        state.otel_span.set_attribute("forge.cached_input_tokens", cached_input_tokens)
        state.otel_span.set_attribute("forge.cost_usd", str(cost))
        state.otel_span.set_attribute("forge.latency_ms", latency_ms)
        state.otel_span.set_status(Status(StatusCode.OK))
        state.otel_span.end()

        event = RunEvent(
            kind=RunEventKind.LLM_CALL,
            run_id=state.run_id,
            agent_id=state.agent_id,
            tool_name=state.tool_name,
            model=state.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
            cached_input_tokens=cached_input_tokens,
            cost=cost,
            latency_ms=latency_ms,
            span_id=span_id,
            parent_span_id=state.parent_span_id,
            data={"metadata": state.metadata} if state.metadata else {},
        )
        await self._bus.publish(event)

    async def on_llm_token(
        self,
        span_id: str,
        token: str,
        *,
        is_thinking: bool = False,
    ) -> None:
        """Publish a single streaming token chunk to the event bus.

        Best-effort: if the span is unknown (e.g. breaker-suppressed or
        orphaned) the event is still published with ``run_id=None``.
        Adapters that do not stream simply never call this method.
        """
        if span_id == _BREAKER_OPEN_SPAN:
            return
        state = self._open_spans.get(span_id)
        event = RunEvent(
            kind=RunEventKind.LLM_TOKEN,
            run_id=state.run_id if state else None,
            agent_id=state.agent_id if state else None,
            span_id=span_id,
            data={"token": token, "is_thinking": is_thinking},
        )
        await self._bus.publish(event)

    async def on_llm_error(self, span_id: str, error: BaseException) -> None:
        """Close the span with ERROR status and publish an ``ERROR`` event."""
        if span_id == _BREAKER_OPEN_SPAN:
            return
        # Inform the breaker so consecutive failures trip it.
        if self._breaker is not None:
            self._breaker.record_failure()

        state = self._open_spans.pop(span_id, None)
        if state is None:
            logger.warning("interceptor.orphan_error", span_id=span_id)
            return

        latency_ms = (time.perf_counter() - state.start_perf) * 1000.0
        state.otel_span.record_exception(error)
        state.otel_span.set_status(Status(StatusCode.ERROR, str(error)))
        state.otel_span.end()

        event = RunEvent(
            kind=RunEventKind.ERROR,
            run_id=state.run_id,
            agent_id=state.agent_id,
            tool_name=state.tool_name,
            model=state.model,
            latency_ms=latency_ms,
            span_id=span_id,
            parent_span_id=state.parent_span_id,
            error=f"{type(error).__name__}: {error}",
        )
        await self._bus.publish(event)

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def close_orphaned_spans(self) -> int:
        """Forcefully close any spans still open at shutdown.

        Returns the number of spans that were abandoned. In a correctly
        instrumented flow this always returns 0; non-zero values indicate
        a framework that didn't emit an end/error callback and should be
        investigated.
        """
        count = 0
        for span_id, state in list(self._open_spans.items()):
            state.otel_span.set_status(Status(StatusCode.ERROR, "span abandoned"))
            state.otel_span.end()
            logger.warning("interceptor.abandoned_span", span_id=span_id, model=state.model)
            count += 1
        self._open_spans.clear()
        return count


class _SpanState:
    """Per-open-span state held by :class:`ForgeLLMInterceptor`.

    Not a :class:`dataclass` because we want explicit slots for the hot
    path — each ``on_llm_start`` allocates one. Small but nonzero.
    """

    __slots__ = (
        "agent_id",
        "metadata",
        "model",
        "otel_span",
        "parent_span_id",
        "run_id",
        "start_perf",
        "tool_name",
    )

    def __init__(
        self,
        *,
        model: str,
        start_perf: float,
        agent_id: str | None,
        tool_name: str | None,
        otel_span: trace.Span,
        run_id: str | None,
        parent_span_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        self.model = model
        self.start_perf = start_perf
        self.agent_id = agent_id
        self.tool_name = tool_name
        self.otel_span = otel_span
        self.run_id = run_id
        self.parent_span_id = parent_span_id
        self.metadata = metadata

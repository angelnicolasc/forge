"""OpenTelemetry-based tracing for Forge runs.

Instruments every LLM call, tool invocation, and agent handoff
with OpenTelemetry spans for full observability.

After L7/L8 fase α, the tracer attaches to the :class:`EventBus` as a
subscriber — that's how Gap #2 ("``ForgeTracer`` implementado pero nunca
invocado") gets closed. The interceptor already opens nested OTel spans
per LLM call; the tracer's job here is to (a) ensure a tracer provider
is configured (console export by default, OTLP on demand) and (b) mirror
run-level bus events as additional spans for frameworks where the
interceptor isn't the source of truth.

Call :func:`attach_tracer_to_orchestrator` once at startup to connect
the tracer to an orchestrator's bus. After that, every future run gets
OTel spans automatically — no explicit ``tracer.record_run`` call needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from forge_core.events import ALL_EVENTS, EventBus, SubscriptionHandle
from forge_core.types import RunEvent, RunEventKind, RunResult

if TYPE_CHECKING:
    from forge_core.harness import MetaOrchestrator

logger = structlog.get_logger()

_FORGE_RESOURCE = Resource.create(
    {
        "service.name": "forge",
        "service.version": "0.1.0",
    }
)


class ForgeTracer:
    """Manages OpenTelemetry tracing for Forge.

    Creates spans for runs, agents, LLM calls, and tool invocations.
    Supports console export (default), OTLP export, and custom exporters.
    """

    def __init__(self, enable_console: bool = True, otlp_endpoint: str | None = None) -> None:
        self._provider = TracerProvider(resource=_FORGE_RESOURCE)

        if enable_console:
            self._provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                self._provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
                )
            except ImportError:
                logger.warning("otlp_exporter_not_available", endpoint=otlp_endpoint)

        trace.set_tracer_provider(self._provider)
        self._tracer = trace.get_tracer("forge", "0.1.0")

    @property
    def tracer(self) -> trace.Tracer:
        return self._tracer

    def record_run(self, result: RunResult) -> None:
        """Record a complete run as a set of OpenTelemetry spans."""
        with self._tracer.start_as_current_span(
            "forge.run",
            attributes={
                "forge.task_id": result.task_id,
                "forge.status": result.status.value,
                "forge.duration_ms": result.duration_ms,
                "forge.total_cost": str(result.cost.total_cost),
                "forge.llm_calls": result.cost.llm_calls,
                "forge.tool_calls": result.cost.tool_calls,
                "forge.input_tokens": result.cost.total_input_tokens,
                "forge.output_tokens": result.cost.total_output_tokens,
            },
        ):
            for event in result.events:
                self._record_event(event)

    def _record_event(self, event: RunEvent) -> None:
        """Record a single event as a child span."""
        attrs: dict[str, Any] = {
            "forge.event.kind": event.kind.value,
        }

        if event.agent_id:
            attrs["forge.agent_id"] = event.agent_id
        if event.tool_name:
            attrs["forge.tool_name"] = event.tool_name
        if event.model:
            attrs["forge.model"] = event.model
        if event.input_tokens:
            attrs["forge.input_tokens"] = event.input_tokens
        if event.output_tokens:
            attrs["forge.output_tokens"] = event.output_tokens
        if event.cost:
            attrs["forge.cost"] = str(event.cost)
        if event.latency_ms:
            attrs["forge.latency_ms"] = event.latency_ms

        span_name = f"forge.{event.kind.value}"
        if event.agent_id:
            span_name = f"forge.{event.kind.value}.{event.agent_id}"

        with self._tracer.start_as_current_span(span_name, attributes=attrs):
            if event.error:
                trace.get_current_span().set_status(
                    trace.Status(trace.StatusCode.ERROR, event.error)
                )

    def shutdown(self) -> None:
        """Flush and shutdown the tracer provider."""
        self._provider.shutdown()

    # ------------------------------------------------------------------
    # EventBus integration (fase α Gap #2 fix)
    # ------------------------------------------------------------------

    def attach_to_bus(self, bus: EventBus) -> SubscriptionHandle:
        """Subscribe to an ``EventBus`` so every future event becomes a span.

        The interceptor already opens nested LLM-call spans itself (those
        are the high-fidelity ones with token counts and latency). This
        subscription is a *secondary* mirror that catches everything else
        — tool calls, agent starts/ends, run boundaries, errors — so the
        OTel trace is complete regardless of which adapter emitted what.

        Returns a :class:`SubscriptionHandle` that the caller can hold for
        later :meth:`EventBus.unsubscribe`.
        """
        return bus.subscribe(ALL_EVENTS, self._on_bus_event)

    async def _on_bus_event(self, event: RunEvent) -> None:
        """Subscriber that mirrors bus events to OTel spans.

        We skip ``LLM_CALL`` here because the interceptor already owns
        those spans — mirroring would produce duplicates. Everything else
        gets a short-lived span (``start_span`` + immediate ``end``) with
        the event's attributes, which is enough for distributed-trace
        visualization without attempting to reconstruct exact timing.
        """
        # The interceptor is authoritative for LLM spans. Don't duplicate.
        if event.kind == RunEventKind.LLM_CALL:
            return

        attrs: dict[str, Any] = {
            "forge.event.kind": event.kind.value,
            "forge.event.id": event.id,
        }
        if event.run_id:
            attrs["forge.run_id"] = event.run_id
        if event.agent_id:
            attrs["forge.agent_id"] = event.agent_id
        if event.tool_name:
            attrs["forge.tool_name"] = event.tool_name
        if event.span_id:
            attrs["forge.span_id"] = event.span_id
        if event.parent_span_id:
            attrs["forge.parent_span_id"] = event.parent_span_id
        if event.latency_ms:
            attrs["forge.latency_ms"] = event.latency_ms

        span_name = f"forge.{event.kind.value}"
        if event.agent_id:
            span_name = f"forge.{event.kind.value}.{event.agent_id}"

        span = self._tracer.start_span(span_name, attributes=attrs)
        try:
            if event.error:
                span.set_status(trace.Status(trace.StatusCode.ERROR, event.error))
        finally:
            span.end()


def attach_tracer_to_orchestrator(
    orchestrator: MetaOrchestrator,
    *,
    enable_console: bool = True,
    otlp_endpoint: str | None = None,
) -> tuple[ForgeTracer, SubscriptionHandle]:
    """Convenience: construct a tracer and wire it to the orchestrator's bus.

    Typical usage::

        orchestrator = MetaOrchestrator()
        tracer, _handle = attach_tracer_to_orchestrator(orchestrator)

    After this call, every run the orchestrator executes produces OTel
    spans automatically — closing Gap #2 of the remediation plan.
    """
    tracer = ForgeTracer(enable_console=enable_console, otlp_endpoint=otlp_endpoint)
    handle = tracer.attach_to_bus(orchestrator.bus)
    return tracer, handle

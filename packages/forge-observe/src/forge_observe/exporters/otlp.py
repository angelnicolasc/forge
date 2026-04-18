"""OTLP exporter for Forge — sends traces to Grafana, Datadog, Jaeger, etc.

Wraps OpenTelemetry's OTLP exporter and bridges Forge RunEvents
to standard OTel spans. Configure via env vars:
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
  OTEL_SERVICE_NAME=forge
"""

from __future__ import annotations

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from forge_core.types import RunEvent, RunEventKind, RunResult

logger = structlog.get_logger()


def setup_otlp_tracing(
    service_name: str = "forge",
    endpoint: str | None = None,
    use_console_fallback: bool = True,
) -> TracerProvider:
    """Set up OpenTelemetry OTLP tracing for Forge.

    Args:
        service_name: Service name shown in the tracing backend.
        endpoint: OTLP gRPC endpoint (e.g., 'http://localhost:4317').
                  If None, uses OTEL_EXPORTER_OTLP_ENDPOINT env var.
        use_console_fallback: If True and no endpoint is configured,
                              falls back to console export.

    Returns:
        Configured TracerProvider.
    """
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "0.1.0",
            "forge.harness": "true",
        }
    )

    provider = TracerProvider(resource=resource)

    if endpoint:
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("otlp.configured", endpoint=endpoint)
    elif use_console_fallback:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.info("otlp.console_fallback_enabled")

    trace.set_tracer_provider(provider)
    return provider


def export_run_result(result: RunResult, tracer_name: str = "forge") -> None:
    """Export a complete RunResult as OTel spans.

    Creates a root span for the run and child spans for each event,
    enabling waterfall visualization in Grafana/Jaeger/Datadog.
    """
    tracer = trace.get_tracer(tracer_name)

    with tracer.start_as_current_span(
        f"forge.run.{result.task_id[:8]}",
        attributes={
            "forge.run_id": result.run_id,
            "forge.task_id": result.task_id,
            "forge.status": result.status,
            "forge.cost_usd": str(result.cost.total_cost),
            "forge.duration_ms": result.duration_ms or 0.0,
            "forge.agent_count": len(result.topology),
        },
    ) as root_span:
        if result.status == "failed" and result.errors:
            root_span.set_status(trace.Status(trace.StatusCode.ERROR, result.errors[0]))

        # Export each event as a child span
        for event in result.events:
            _export_event(tracer, event, result.run_id)


def _export_event(
    tracer: trace.Tracer,
    event: RunEvent,
    run_id: str,
) -> None:
    """Export a single RunEvent as an OTel span."""
    span_name = f"forge.{event.kind.value}"
    attrs: dict[str, str | int | float | bool] = {
        "forge.run_id": run_id,
        "forge.event_kind": event.kind.value,
    }

    if event.agent_id:
        attrs["forge.agent_id"] = event.agent_id
    if event.input_tokens:
        attrs["forge.input_tokens"] = event.input_tokens
    if event.output_tokens:
        attrs["forge.output_tokens"] = event.output_tokens
    if event.model:
        attrs["llm.model"] = event.model
    if event.latency_ms:
        attrs["forge.latency_ms"] = event.latency_ms

    with tracer.start_as_current_span(span_name, attributes=attrs) as span:
        if event.error:
            span.set_status(trace.Status(trace.StatusCode.ERROR, event.error))
        if event.kind == RunEventKind.LLM_CALL:
            span.set_attribute("forge.cost_usd", str(event.cost))

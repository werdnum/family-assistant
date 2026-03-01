"""OpenTelemetry observability setup.

Provides automatic instrumentation for FastAPI, HTTPX, SQLAlchemy, and logging
correlation. When disabled (the default), all OTel APIs fall back to no-ops.

Lazy imports are used throughout to avoid loading heavy dependencies (grpcio, etc.)
when they aren't needed for the configured exporter type.
"""

# ruff: noqa: PLC0415

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

if TYPE_CHECKING:
    from fastapi import FastAPI

    from family_assistant.config_models import OTelConfig

logger = logging.getLogger(__name__)


@dataclass
class ObservabilityHandle:
    """Handle returned by setup_observability for clean shutdown."""

    tracer_provider: TracerProvider
    meter_provider: metrics.MeterProvider | None = (
        None  # SDK MeterProvider, has shutdown()
    )

    def shutdown(self) -> None:
        """Flush pending spans/metrics and shut down providers."""
        logger.info("Shutting down OpenTelemetry providers...")
        self.tracer_provider.shutdown()
        if self.meter_provider and hasattr(self.meter_provider, "shutdown"):
            # SDK MeterProvider has shutdown() but the base API interface does not
            self.meter_provider.shutdown()  # type: ignore[union-attr]
        logger.info("OpenTelemetry providers shut down.")


def _create_trace_exporter(config: OTelConfig) -> SpanExporter | None:
    """Create a span exporter based on the configured exporter type."""
    exporter_type = config.traces_exporter

    if exporter_type == "none":
        return None

    if exporter_type == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()

    if exporter_type == "otlp-grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcExporter,
        )

        endpoint = config.otlp_traces_endpoint or config.otlp_endpoint
        return GrpcExporter(endpoint=endpoint)

    if exporter_type == "otlp-http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )

        endpoint = config.otlp_traces_endpoint or config.otlp_endpoint
        return HttpExporter(endpoint=endpoint)

    msg = f"Unknown traces exporter type: {exporter_type!r}. Use 'otlp-grpc', 'otlp-http', 'console', or 'none'."
    raise ValueError(msg)


def _create_meter_provider(
    config: OTelConfig, resource: Resource
) -> metrics.MeterProvider | None:
    """Create a meter provider based on the configured metrics exporter."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )

    exporter_type = config.metrics_exporter

    if exporter_type == "none":
        return None

    if exporter_type == "console":
        reader = PeriodicExportingMetricReader(ConsoleMetricExporter())
        return MeterProvider(resource=resource, metric_readers=[reader])

    if exporter_type == "otlp-grpc":
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter as GrpcMetricExporter,
        )

        endpoint = config.otlp_metrics_endpoint or config.otlp_endpoint
        reader = PeriodicExportingMetricReader(GrpcMetricExporter(endpoint=endpoint))
        return MeterProvider(resource=resource, metric_readers=[reader])

    if exporter_type == "otlp-http":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter as HttpMetricExporter,
        )

        endpoint = config.otlp_metrics_endpoint or config.otlp_endpoint
        reader = PeriodicExportingMetricReader(HttpMetricExporter(endpoint=endpoint))
        return MeterProvider(resource=resource, metric_readers=[reader])

    msg = f"Unknown metrics exporter type: {exporter_type!r}. Use 'otlp-grpc', 'otlp-http', 'console', or 'none'."
    raise ValueError(msg)


def setup_observability(
    config: OTelConfig,
    fastapi_app: FastAPI | None = None,
) -> ObservabilityHandle | None:
    """Initialize OpenTelemetry tracing, metrics, and auto-instrumentation.

    Args:
        config: OTel configuration.
        fastapi_app: Optional FastAPI app instance to instrument directly.
            If provided, uses instrument_app() for the already-instantiated app.

    Returns an ObservabilityHandle for shutdown, or None if OTel is disabled.
    """
    if not config.enabled:
        logger.info("OpenTelemetry is disabled (otel.enabled=false).")
        return None

    logger.info("Initializing OpenTelemetry (service=%s)...", config.service_name)

    resource = Resource.create({"service.name": config.service_name})

    # Tracing
    sampler = TraceIdRatioBased(config.traces_sample_rate)
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)

    trace_exporter = _create_trace_exporter(config)
    if trace_exporter is not None:
        tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))

    if config.debug_console_exporter:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(tracer_provider)

    # Metrics
    meter_provider = _create_meter_provider(config, resource)
    if meter_provider is not None:
        metrics.set_meter_provider(meter_provider)

    # Auto-instrumentation
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    if fastapi_app is not None:
        FastAPIInstrumentor().instrument_app(fastapi_app)
    else:
        FastAPIInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument()

    if config.log_correlation:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=False)

    logger.info("OpenTelemetry initialized successfully.")
    return ObservabilityHandle(
        tracer_provider=tracer_provider, meter_provider=meter_provider
    )

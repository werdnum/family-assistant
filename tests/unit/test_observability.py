"""Tests for the OpenTelemetry observability module."""

from __future__ import annotations

import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from family_assistant.config_models import OTelConfig
from family_assistant.observability import ObservabilityHandle, setup_observability


class TestOTelConfig:
    """Tests for OTelConfig model defaults and validation."""

    def test_defaults(self) -> None:
        cfg = OTelConfig()
        assert cfg.enabled is False
        assert cfg.service_name == "family-assistant"
        assert cfg.traces_exporter == "otlp-grpc"
        assert cfg.metrics_exporter == "otlp-grpc"
        assert cfg.otlp_endpoint == "http://localhost:4317"
        assert cfg.otlp_traces_endpoint is None
        assert cfg.otlp_metrics_endpoint is None
        assert cfg.log_correlation is True
        assert cfg.traces_sample_rate == 1.0
        assert cfg.debug_console_exporter is False

    def test_custom_values(self) -> None:
        cfg = OTelConfig(
            enabled=True,
            service_name="test-svc",
            traces_exporter="console",
            metrics_exporter="none",
            otlp_endpoint="http://otel:4317",
            traces_sample_rate=0.5,
        )
        assert cfg.enabled is True
        assert cfg.service_name == "test-svc"
        assert cfg.traces_exporter == "console"
        assert cfg.metrics_exporter == "none"
        assert cfg.traces_sample_rate == 0.5

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            # Intentionally passing invalid argument to test extra="forbid"
            OTelConfig(unknown_field="bad")  # type: ignore[call-arg]

    def test_sample_rate_boundaries(self) -> None:
        cfg_zero = OTelConfig(traces_sample_rate=0.0)
        assert cfg_zero.traces_sample_rate == 0.0
        cfg_one = OTelConfig(traces_sample_rate=1.0)
        assert cfg_one.traces_sample_rate == 1.0

    def test_sample_rate_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OTelConfig(traces_sample_rate=-0.1)
        with pytest.raises(ValidationError):
            OTelConfig(traces_sample_rate=1.1)


class TestSetupObservabilityDisabled:
    """Tests for setup_observability when disabled."""

    def test_returns_none_when_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            result = setup_observability(OTelConfig(enabled=False))
        assert result is None
        assert "disabled" in caplog.text.lower()


class TestSetupObservabilityEnabled:
    """Tests for setup_observability when enabled."""

    def _reset_otel(self) -> None:
        """Reset OTel global state to avoid polluting other tests."""
        trace.set_tracer_provider(trace.NoOpTracerProvider())

    def test_returns_handle_with_console_exporter(self) -> None:
        try:
            handle = setup_observability(
                OTelConfig(
                    enabled=True,
                    traces_exporter="console",
                    metrics_exporter="none",
                    log_correlation=False,
                )
            )
            assert handle is not None
            assert isinstance(handle.tracer_provider, TracerProvider)
        finally:
            self._reset_otel()

    def test_returns_handle_with_none_exporter(self) -> None:
        try:
            handle = setup_observability(
                OTelConfig(
                    enabled=True,
                    traces_exporter="none",
                    metrics_exporter="none",
                    log_correlation=False,
                )
            )
            assert handle is not None
        finally:
            self._reset_otel()

    def test_debug_console_exporter_adds_processor(self) -> None:
        try:
            handle = setup_observability(
                OTelConfig(
                    enabled=True,
                    traces_exporter="none",
                    metrics_exporter="none",
                    debug_console_exporter=True,
                    log_correlation=False,
                )
            )
            assert handle is not None
            # The tracer provider should have at least one span processor
            # (the debug console one)
            assert (
                len(handle.tracer_provider._active_span_processor._span_processors) > 0
            )  # type: ignore[attr-defined]
        finally:
            self._reset_otel()

    def test_unknown_traces_exporter_raises(self) -> None:
        try:
            with pytest.raises(ValueError, match="Unknown traces exporter"):
                setup_observability(
                    OTelConfig(
                        enabled=True,
                        traces_exporter="invalid",
                        metrics_exporter="none",
                        log_correlation=False,
                    )
                )
        finally:
            self._reset_otel()

    def test_unknown_metrics_exporter_raises(self) -> None:
        try:
            with pytest.raises(ValueError, match="Unknown metrics exporter"):
                setup_observability(
                    OTelConfig(
                        enabled=True,
                        traces_exporter="none",
                        metrics_exporter="invalid",
                        log_correlation=False,
                    )
                )
        finally:
            self._reset_otel()


class TestObservabilityHandle:
    """Tests for ObservabilityHandle shutdown."""

    def test_shutdown_calls_providers(self) -> None:
        provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
        handle = ObservabilityHandle(tracer_provider=provider)
        handle.shutdown()
        # Second shutdown should be safe (idempotent)
        handle.shutdown()


class TestAutoInstrumentationSpans:
    """Tests that verify manual tracer produces real spans when OTel is enabled."""

    def _reset_otel(self) -> None:
        trace.set_tracer_provider(trace.NoOpTracerProvider())

    def test_manual_span_recorded(self) -> None:
        """Verify that enabling OTel makes the tracer produce real spans."""
        memory_exporter = InMemorySpanExporter()
        try:
            handle = setup_observability(
                OTelConfig(
                    enabled=True,
                    traces_exporter="none",
                    metrics_exporter="none",
                    log_correlation=False,
                )
            )
            assert handle is not None

            handle.tracer_provider.add_span_processor(
                SimpleSpanProcessor(memory_exporter)
            )

            # Use the handle's tracer provider directly (not the global one)
            # to avoid races with parallel test execution resetting global state
            tracer = handle.tracer_provider.get_tracer("test-tracer")
            with tracer.start_as_current_span("test-span") as span:
                span.set_attribute("test.key", "test-value")

            spans = memory_exporter.get_finished_spans()
            assert len(spans) == 1
            assert spans[0].name == "test-span"
            assert spans[0].attributes is not None
            assert spans[0].attributes.get("test.key") == "test-value"
        finally:
            self._reset_otel()

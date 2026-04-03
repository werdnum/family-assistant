"""Tests for the OpenTelemetry observability module."""

from __future__ import annotations

import logging

import pytest
from opentelemetry import metrics, trace
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

    def teardown_method(self) -> None:
        """Reset OTel global state to avoid polluting other tests."""
        trace.set_tracer_provider(trace.NoOpTracerProvider())
        metrics.set_meter_provider(metrics.NoOpMeterProvider())

    def test_returns_none_when_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            result = setup_observability(OTelConfig(enabled=False))
        assert result is None
        assert "disabled" in caplog.text.lower()

    def test_noop_tracer_produces_nonrecording_spans(self) -> None:
        setup_observability(OTelConfig(enabled=False))
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("test-span") as span:
            assert not span.is_recording()


class TestSetupObservabilityEnabled:
    """Tests for setup_observability when enabled."""

    def teardown_method(self) -> None:
        """Reset OTel global state to avoid polluting other tests."""
        trace.set_tracer_provider(trace.NoOpTracerProvider())
        metrics.set_meter_provider(metrics.NoOpMeterProvider())

    def test_returns_handle_with_console_exporter(self) -> None:
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

    def test_returns_handle_with_none_exporter(self) -> None:
        handle = setup_observability(
            OTelConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="none",
                log_correlation=False,
            )
        )
        assert handle is not None

    def test_metrics_none_installs_noop_meter_provider(self) -> None:
        handle = setup_observability(
            OTelConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="none",
                log_correlation=False,
            )
        )
        assert handle is not None
        assert isinstance(metrics.get_meter_provider(), metrics.NoOpMeterProvider)

    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            ("http://jaeger:4318", "http://jaeger:4318/v1/traces"),
            ("http://jaeger:4318/", "http://jaeger:4318/v1/traces"),
            ("http://jaeger:4318/otel", "http://jaeger:4318/otel/v1/traces"),
            ("http://jaeger:4318/v1/traces", "http://jaeger:4318/v1/traces"),
        ],
    )
    def test_otlp_http_trace_exporter_normalizes_endpoint(
        self, endpoint: str, expected: str
    ) -> None:
        handle = setup_observability(
            OTelConfig(
                enabled=True,
                traces_exporter="otlp-http",
                metrics_exporter="none",
                otlp_endpoint=endpoint,
                log_correlation=False,
            )
        )
        assert handle is not None
        processor = handle.tracer_provider._active_span_processor._span_processors[0]  # type: ignore[attr-defined]
        assert processor.span_exporter._endpoint == expected  # type: ignore[attr-defined]
        handle.shutdown()

    def test_otlp_http_trace_exporter_preserves_explicit_endpoint(self) -> None:
        explicit_endpoint = "http://jaeger:4318/custom/traces?token=secret"
        handle = setup_observability(
            OTelConfig(
                enabled=True,
                traces_exporter="otlp-http",
                metrics_exporter="none",
                otlp_endpoint="http://jaeger:4318",
                otlp_traces_endpoint=explicit_endpoint,
                log_correlation=False,
            )
        )
        assert handle is not None
        processor = handle.tracer_provider._active_span_processor._span_processors[0]  # type: ignore[attr-defined]
        assert processor.span_exporter._endpoint == explicit_endpoint  # type: ignore[attr-defined]
        handle.shutdown()

    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            ("http://jaeger:4318", "http://jaeger:4318/v1/metrics"),
            ("http://jaeger:4318/", "http://jaeger:4318/v1/metrics"),
            ("http://jaeger:4318/otel", "http://jaeger:4318/otel/v1/metrics"),
            ("http://jaeger:4318/v1/metrics", "http://jaeger:4318/v1/metrics"),
        ],
    )
    def test_otlp_http_metrics_exporter_normalizes_endpoint(
        self, endpoint: str, expected: str
    ) -> None:
        handle = setup_observability(
            OTelConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="otlp-http",
                otlp_endpoint=endpoint,
                log_correlation=False,
            )
        )
        assert handle is not None
        assert handle.meter_provider is not None
        reader = handle.meter_provider._sdk_config.metric_readers[0]  # type: ignore[attr-defined]
        assert reader._exporter._endpoint == expected  # type: ignore[attr-defined]
        handle.shutdown()

    def test_otlp_http_metrics_exporter_preserves_explicit_endpoint(self) -> None:
        explicit_endpoint = "http://jaeger:4318/custom/metrics?token=secret"
        handle = setup_observability(
            OTelConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="otlp-http",
                otlp_endpoint="http://jaeger:4318",
                otlp_metrics_endpoint=explicit_endpoint,
                log_correlation=False,
            )
        )
        assert handle is not None
        assert handle.meter_provider is not None
        reader = handle.meter_provider._sdk_config.metric_readers[0]  # type: ignore[attr-defined]
        assert reader._exporter._endpoint == explicit_endpoint  # type: ignore[attr-defined]
        handle.shutdown()

    def test_debug_console_exporter_adds_processor(self) -> None:
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
        assert len(handle.tracer_provider._active_span_processor._span_processors) > 0  # type: ignore[attr-defined]

    def test_unknown_traces_exporter_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown traces exporter"):
            setup_observability(
                OTelConfig(
                    enabled=True,
                    traces_exporter="invalid",
                    metrics_exporter="none",
                    log_correlation=False,
                )
            )

    def test_unknown_metrics_exporter_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown metrics exporter"):
            setup_observability(
                OTelConfig(
                    enabled=True,
                    traces_exporter="none",
                    metrics_exporter="invalid",
                    log_correlation=False,
                )
            )


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

    def teardown_method(self) -> None:
        """Reset OTel global state to avoid polluting other tests."""
        trace.set_tracer_provider(trace.NoOpTracerProvider())
        metrics.set_meter_provider(metrics.NoOpMeterProvider())

    def test_manual_span_recorded(self) -> None:
        """Verify that enabling OTel makes the tracer produce real spans."""
        memory_exporter = InMemorySpanExporter()
        handle = setup_observability(
            OTelConfig(
                enabled=True,
                traces_exporter="none",
                metrics_exporter="none",
                log_correlation=False,
            )
        )
        assert handle is not None

        handle.tracer_provider.add_span_processor(SimpleSpanProcessor(memory_exporter))

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

"""Helpers for keeping app-managed OTel config out of SDK auto-config."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

APP_OTEL_ENV_VARS = frozenset({
    "OTEL_ENABLED",
    "OTEL_SERVICE_NAME",
    "OTEL_TRACES_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_LOG_CORRELATION",
    "OTEL_TRACES_SAMPLE_RATE",
    "OTEL_DEBUG_CONSOLE_EXPORTER",
})

AUTO_CONFIG_EXPORTER_OVERRIDES = {
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
}


def neutralize_otel_env(extra_values: Mapping[str, str | None] | None = None) -> None:
    """Move app-owned OTel vars away from SDK auto-configuration."""
    for key in list(os.environ):
        if key in {"OTEL_PYTHON_TRACER_PROVIDER", "OTEL_PYTHON_METER_PROVIDER"}:
            os.environ.pop(key)

    for key in APP_OTEL_ENV_VARS:
        value = os.environ.get(key)
        if value in {None, AUTO_CONFIG_EXPORTER_OVERRIDES.get(key)} and extra_values:
            value = extra_values.get(key)
        if value is None:
            continue
        private_key = f"_FA_{key}"
        if private_key not in os.environ:
            os.environ[private_key] = value
        os.environ.pop(key)

    # The app configures telemetry providers explicitly in setup_observability().
    # Keep the public env visible only as no-op guards for any SDK auto-config path
    # that runs after package import; otherwise the SDK defaults to OTLP localhost.
    os.environ.update(AUTO_CONFIG_EXPORTER_OVERRIDES)

"""Helpers for keeping app-managed OTel config out of SDK auto-config."""

from __future__ import annotations

import os

APP_OTEL_ENV_VARS = frozenset(
    {
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
    }
)


def neutralize_otel_env() -> None:
    """Move app-owned OTel vars away from SDK auto-configuration."""
    for key in list(os.environ):
        if key.startswith("OTEL_PYTHON_"):
            os.environ.pop(key)

    for key in APP_OTEL_ENV_VARS:
        if key not in os.environ:
            continue
        private_key = f"_FA_{key}"
        if private_key not in os.environ:
            os.environ[private_key] = os.environ[key]
        os.environ.pop(key)

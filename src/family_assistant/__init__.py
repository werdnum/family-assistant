"""Family Assistant Package."""

import logging.config
import os

from .otel_env import neutralize_otel_env

# Configure logging as early as possible
LOGGING_CONFIG = os.getenv("LOGGING_CONFIG", "logging.conf")
if os.path.exists(LOGGING_CONFIG):
    logging.config.fileConfig(LOGGING_CONFIG, disable_existing_loggers=False)

# Prevent the OTEL SDK from auto-configuring providers before our
# setup_observability() runs.  This must happen before any submodule
# import that calls trace.get_tracer() / metrics.get_meter() at module
# level, because the OTEL API's get_*_provider() auto-discovers SDK
# providers via entry points when OTEL_PYTHON_*_PROVIDER env vars are
# set (e.g. by the Kubernetes OpenTelemetry Operator).  Without this,
# the auto-discovered provider claims a Once guard that makes our
# subsequent set_*_provider() call a silent no-op.
#
# App-owned OTEL env vars (OTEL_TRACES_EXPORTER, OTEL_METRICS_EXPORTER, ...)
# are also cleared because opentelemetry-instrument / distros read them
# to auto-configure exporters. Values are moved to _FA_OTEL_* so our
# config_loader (which runs later via load_config()) can still read them.
neutralize_otel_env()

__version__ = "0.1.0"

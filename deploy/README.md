# Deployment

## Docker Compose

The `docker-compose.yaml` in this directory runs the application with PostgreSQL. It expects a
`../.env` file (relative to this directory) containing the environment variables used by the defined
services. Create that file and set the required values, then:

```bash
docker compose up -d
```

## OpenTelemetry (Observability)

OpenTelemetry provides distributed tracing, metrics, and log correlation. It is **disabled by
default** and has zero overhead when disabled (all API calls fall back to no-ops).

### Quick Start

To enable tracing with an OTLP-compatible collector (e.g. Jaeger, Grafana Tempo):

```bash
export OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT="http://your-collector:4317"
python -m family_assistant
```

### Environment Variables

| Variable                              | Default                 | Description                                                   |
| ------------------------------------- | ----------------------- | ------------------------------------------------------------- |
| `OTEL_ENABLED`                        | `false`                 | Master switch — must be `true` to enable OTel                 |
| `OTEL_SERVICE_NAME`                   | `family-assistant`      | Service name in traces                                        |
| `OTEL_TRACES_EXPORTER`                | `otlp-grpc`             | `otlp-grpc`, `otlp-http`, `console`, or `none`                |
| `OTEL_METRICS_EXPORTER`               | `otlp-grpc`             | `otlp-grpc`, `otlp-http`, `console`, or `none`                |
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | `http://localhost:4317` | Shared OTLP endpoint (gRPC port; use `:4318` for `otlp-http`) |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`  | _(uses shared)_         | Override endpoint for traces only                             |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | _(uses shared)_         | Override endpoint for metrics only                            |
| `OTEL_LOG_CORRELATION`                | `true`                  | Inject trace/span IDs into Python log records                 |
| `OTEL_TRACES_SAMPLE_RATE`             | `1.0`                   | Sampling rate (0.0–1.0)                                       |
| `OTEL_DEBUG_CONSOLE_EXPORTER`         | `false`                 | Also print spans to console (for debugging)                   |

### When disabled

When `OTEL_ENABLED=false` (the default), no-op tracer and meter providers are explicitly set. No SDK
components are loaded, no connections are attempted, and no errors are produced. You do not need a
collector running.

### When enabled

Ensure an OTLP-compatible collector is reachable at the configured endpoint. Without a collector,
you will see gRPC/HTTP connection errors in the logs. To enable OTel without a collector (e.g. for
local development), set the exporter to `console` or `none`:

```bash
export OTEL_ENABLED=true
export OTEL_TRACES_EXPORTER=console   # Print spans to stdout
export OTEL_METRICS_EXPORTER=none     # Disable metrics export
```

### Docker Compose with Jaeger

Add a Jaeger service to your `docker-compose.yaml`:

```yaml
services:
  jaeger:
    image: jaegertracing/all-in-one:1.76.0
    ports:
      - "4317:4317"   # OTLP gRPC
      - "16686:16686" # Jaeger UI
  app:
    environment:
      - OTEL_ENABLED=true
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317
```

The Jaeger UI will be available at `http://localhost:16686`.

See [docs/design/opentelemetry-observability.md](../docs/design/opentelemetry-observability.md) for
the full design document including span hierarchy, semantic conventions, and implementation details.

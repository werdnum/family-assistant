# Deployment

## Docker Compose

The `docker-compose.yaml` in this directory runs the application with PostgreSQL. It expects a
`../.env` file (relative to this directory) containing the environment variables used by the defined
services. Create that file and set the required values, then:

```bash
docker compose up -d
```

## OpenTelemetry (Observability)

OpenTelemetry provider and exporter setup is handled by the standard `opentelemetry-instrument`
wrapper. The Docker image default command already uses that wrapper and defaults all exporters to
`none`, so plain local launches do not try to export telemetry.

### Quick Start

To enable tracing with an OTLP-compatible collector, export standard SDK variables before starting
the process:

```bash
export OTEL_SERVICE_NAME=family-assistant
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://your-collector:4317"
opentelemetry-instrument family-assistant
```

### Environment Variables

- `OTEL_SERVICE_NAME`: service name in traces. Defaults to `family-assistant`.
- `OTEL_TRACES_EXPORTER`: trace exporter. Defaults to `none`; use `otlp` or `console` to enable.
- `OTEL_METRICS_EXPORTER`: metrics exporter. Defaults to `none`.
- `OTEL_LOGS_EXPORTER`: logs exporter. Defaults to `none`.
- `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`: traces protocol, such as `grpc` or `http/protobuf`.
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`: signal-specific OTLP traces endpoint.
- `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`: signal-specific OTLP metrics endpoint.
- `OTEL_SDK_DISABLED`: set to `true` before wrapper startup to disable SDK setup entirely.

### When disabled

When the exporters are `none` (the Docker default), the SDK installs providers but no network
exporters are configured. You do not need a collector running. Set `OTEL_SDK_DISABLED=true` before
wrapper startup to disable SDK setup entirely.

### When enabled

Ensure an OTLP-compatible collector is reachable at the configured endpoint. Without a collector,
you will see gRPC/HTTP connection errors in the logs. To emit local spans without a collector, set
the traces exporter to `console` and keep metrics/logs disabled:

```bash
export OTEL_TRACES_EXPORTER=console
export OTEL_METRICS_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
```

Use signal-specific endpoints. For OTLP/HTTP, include the signal path such as `/v1/traces`.

### Docker Compose with Jaeger

Add a Jaeger service and enable trace export for the app:

```yaml
services:
  jaeger:
    image: jaegertracing/all-in-one:1.76.0
    ports:
      - "4317:4317"   # OTLP gRPC
      - "16686:16686" # Jaeger UI
  app:
    environment:
      - OTEL_TRACES_EXPORTER=otlp
      - OTEL_METRICS_EXPORTER=none
      - OTEL_LOGS_EXPORTER=none
      - OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=grpc
      - OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://jaeger:4317
```

The Jaeger UI will be available at `http://localhost:16686`.

See [docs/design/opentelemetry-observability.md](../docs/design/opentelemetry-observability.md) for
the application/runtime contract.

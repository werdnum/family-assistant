# OpenTelemetry Observability

Family Assistant uses the OpenTelemetry API in application code for spans, but does not configure
OpenTelemetry SDK providers or exporters at application startup. Runtime SDK setup is owned by the
standard zero-code wrapper:

```sh
opentelemetry-instrument family-assistant
```

The Docker image default command and Poe backend serve tasks run through that wrapper. Exporters
default to `none` for plain local launches so the SDK does not try to export to localhost unless the
process environment explicitly enables an exporter.

## Runtime Configuration

Configure telemetry with standard `OTEL_*` environment variables in the process environment before
`opentelemetry-instrument` starts. Values loaded later by the application from `.env` or YAML are
normal app config only; they are too late to configure the zero-code SDK.

For Jaeger OTLP/HTTP traces without metrics or logs:

```sh
OTEL_SERVICE_NAME=family-assistant
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=none
OTEL_LOGS_EXPORTER=none
OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://jaeger.opentelemetry.svc.cluster.local:4318/v1/traces
opentelemetry-instrument family-assistant
```

For Jaeger OTLP/gRPC traces without metrics or logs:

```sh
OTEL_SERVICE_NAME=family-assistant
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=none
OTEL_LOGS_EXPORTER=none
OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=grpc
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://jaeger.opentelemetry.svc.cluster.local:4317
opentelemetry-instrument family-assistant
```

Use `OTEL_SDK_DISABLED=true` to disable the SDK before the wrapper starts. Family Assistant does not
support the old app-specific YAML section, legacy enable flag, or legacy app-only OTLP exporter
aliases.

## Application Code Contract

Application modules may import `opentelemetry.trace` and create spans through the OpenTelemetry API.
They should not call `set_tracer_provider`, `set_meter_provider`, construct OTLP exporters, or
install metric readers.

Direct launches such as `python -m family_assistant` are valid for local app execution, but they
intentionally do not install an OpenTelemetry SDK provider. Use `opentelemetry-instrument` when
traces are required.

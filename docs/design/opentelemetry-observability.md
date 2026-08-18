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

## LLM Call Attributes

Every provider call produces two records of the same event: a span, and a record in the in-memory
diagnostics ring buffer that `get_llm_request_history` and the diagnostics export read.
`LLMCallTelemetry` (`family_assistant/llm/utils/call_telemetry.py`) assembles both, so a provider
reports the same detail everywhere and a new provider gets timing and request shape for free.

Two layers of spans cover a turn, and they answer different questions:

- `llm.provider.generate` / `llm.provider.generate_stream` — one attempt against one provider.
- `llm.generate` / `llm.generate_stream` — the retrying client's view: what the caller waited for,
  including rate-limit sleeps and a fallback's whole second attempt.

### Request

| Attribute                      | Meaning                                                          |
| ------------------------------ | ---------------------------------------------------------------- |
| `gen_ai.system`                | Provider (`anthropic`, `openai`, `google-genai`).                |
| `gen_ai.request.model`         | Model as configured — which may be an alias.                     |
| `gen_ai.operation.name`        | `chat`, or the agent id for an Interactions API agent run.       |
| `llm.request.id`               | Joins the span to its ring-buffer record.                        |
| `llm.request.streaming`        | Whether the turn was streamed.                                   |
| `llm.request.message_count`    | Messages sent.                                                   |
| `llm.request.payload_chars`    | Approximate serialized request size: messages plus tool schemas. |
| `llm.request.attachment_chars` | Upper bound on attachment bytes, counted apart (see below).      |
| `llm.request.tool_count`       | Tool definitions sent.                                           |
| `llm.request.tool_choice`      | The `tool_choice` sent.                                          |

### Response and timing

| Attribute                       | Meaning                                                            |
| ------------------------------- | ------------------------------------------------------------------ |
| `gen_ai.response.model`         | Model the provider reported serving.                               |
| `llm.response.model_resolved`   | Whether that came from the provider or is the request echoed back. |
| `gen_ai.response.id`            | Provider-side request id, for support tickets.                     |
| `gen_ai.response.finish_reason` | Stop reason.                                                       |
| `llm.duration_ms`               | Wall-clock for the call.                                           |
| `llm.time_to_first_output_ms`   | Until the first content, thinking or tool-call token.              |
| `llm.response.content_chars`    | Characters of user-visible content produced.                       |
| `llm.response.thinking_chars`   | Characters of reasoning; absent when the turn did not stream it.   |
| `llm.response.tool_call_count`  | Tool calls returned.                                               |
| `llm.error.type`                | Exception class, on failure.                                       |
| `gen_ai.usage.*`                | Token usage, including prompt-cache reads and writes.              |

The retry spans add `llm.attempts` (provider calls actually issued, so a non-retriable failure that
goes straight to the fallback reports two, not three), `llm.fallback_used` and `llm.has_fallback`,
and carry `llm.attempt` / `llm.fallback` / `llm.rate_limit_retry` events.

The requested and resolved model are recorded separately on purpose: an alias or provider-side
routing resolves to a dated snapshot, so a latency or quality change with no deploy behind it
usually shows up as `gen_ai.response.model` moving while `gen_ai.request.model` stays put. Splitting
duration from time to first output separates the two ways a turn gets slow — a late first token
(provider queueing, or a large prompt, which `llm.request.payload_chars` will show) from a long tail
of output.

Attachment bytes are counted separately from the serialized payload rather than added to it: a
provider substitutes a short text description for an attachment type it cannot read, and which types
those are differs by provider and model, so folding the two together would let a substituted file
inflate the number that says how much was actually sent.

### Scope: structured and JSON calls

`generate_structured` and `generate_json` are covered by the retry layer's spans
(`llm.generate_structured`, `llm.generate_json`, carrying attempt count, fallback flag and duration)
but not by the per-call helper, so they produce no provider span, no ring-buffer record, and no
resolved model or payload size, and a provider's internal schema-validation retries are not broken
out. That is a deliberate boundary rather than an oversight: these calls serve internal utilities —
classification, duplicate detection — rather than a conversational turn, so they are not what a
latency investigation starts from, and each provider implements them with its own validation-retry
loop that would have to be instrumented separately. Route them through `LLMCallTelemetry` when one
of those calls is what needs explaining.

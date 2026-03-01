# OpenTelemetry Observability

## 1. Introduction

This document proposes adding OpenTelemetry (OTel) instrumentation to the Family Assistant
application, covering distributed tracing, metrics, and log correlation.

### 1.1. Current State

| Capability          | Status                                                           |
| ------------------- | ---------------------------------------------------------------- |
| Logging             | Standard Python `logging` with basic text format                 |
| Error persistence   | Custom `SQLAlchemyErrorHandler` writes ERROR+ to database        |
| Debug flags         | `LITELLM_DEBUG`, `DEBUG_LLM_MESSAGES` for ad-hoc troubleshooting |
| Distributed tracing | None                                                             |
| Metrics             | None                                                             |
| Log correlation     | None (no trace IDs in logs)                                      |

### 1.2. Goals

- **Traces**: End-to-end visibility into conversation processing, LLM calls, tool execution, and
  database queries.
- **Metrics**: Counters and histograms for LLM token usage, request latency, tool execution
  frequency, and error rates.
- **Log correlation**: Inject trace/span IDs into existing Python log records so logs can be
  correlated with traces in observability backends.
- **Opt-in**: Disabled by default. When disabled, zero overhead beyond the thin OTel API layer
  (no-op tracer/meter).
- **Standard export**: OTLP over gRPC (primary) targeting Jaeger, Grafana Tempo, or any
  OTLP-compatible collector.

### 1.3. Non-Goals

- Replacing the existing logging system with structured logging (e.g. structlog).
- Instrumenting the React frontend (browser-side tracing).
- Custom dashboards or alerting rules (those are backend-specific).

## 2. Dependencies

Add to `pyproject.toml` as required dependencies:

```
# OpenTelemetry core
opentelemetry-api >= 1.25.0
opentelemetry-sdk >= 1.25.0

# OTLP exporters
opentelemetry-exporter-otlp-proto-grpc >= 1.25.0
opentelemetry-exporter-otlp-proto-http >= 1.25.0

# Auto-instrumentors
opentelemetry-instrumentation-fastapi >= 0.46b0
opentelemetry-instrumentation-httpx >= 0.46b0
opentelemetry-instrumentation-sqlalchemy >= 0.46b0
opentelemetry-instrumentation-logging >= 0.46b0
```

**Why these packages**:

| Package                                    | Purpose                                                |
| ------------------------------------------ | ------------------------------------------------------ |
| `opentelemetry-api` + `opentelemetry-sdk`  | Core tracing and metrics API/SDK                       |
| `opentelemetry-exporter-otlp-proto-grpc`   | OTLP export over gRPC (preferred for production)       |
| `opentelemetry-exporter-otlp-proto-http`   | OTLP export over HTTP (fallback when gRPC unavailable) |
| `opentelemetry-instrumentation-fastapi`    | Auto-spans for every HTTP request                      |
| `opentelemetry-instrumentation-httpx`      | Auto-spans for outbound httpx calls                    |
| `opentelemetry-instrumentation-sqlalchemy` | Auto-spans for database queries                        |
| `opentelemetry-instrumentation-logging`    | Injects trace_id/span_id into Python log records       |

**Not included**: GenAI-specific OTel packages (e.g. `opentelemetry-instrumentation-openai`). These
instrument at the SDK layer, but our LLM calls go through custom wrapper clients
(`RetryingLLMClient`, `AnthropicClient`, `GoogleGenAIClient`). Manual spans at the wrapper level
give better control and avoid conflicts with LiteLLM's own optional OTel support.

## 3. Configuration

### 3.1. Config Model

Add `OTelConfig` to `src/family_assistant/config_models.py`:

```python
class OTelConfig(BaseModel):
    """OpenTelemetry configuration."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    service_name: str = "family-assistant"

    # Exporter type: "otlp-grpc", "otlp-http", "console", "none"
    traces_exporter: str = "otlp-grpc"
    metrics_exporter: str = "otlp-grpc"

    # OTLP endpoint (shared unless overridden)
    otlp_endpoint: str = "http://localhost:4317"
    otlp_traces_endpoint: str | None = None
    otlp_metrics_endpoint: str | None = None

    log_correlation: bool = True
    traces_sample_rate: float = 1.0
    debug_console_exporter: bool = False
```

Add to `AppConfig`:

```python
otel: OTelConfig = Field(default_factory=OTelConfig)
```

### 3.2. Environment Variables

Add to `ENV_VAR_MAPPINGS` in `config_loader.py`:

| Env Var                               | Config Path                   | Type  |
| ------------------------------------- | ----------------------------- | ----- |
| `OTEL_ENABLED`                        | `otel.enabled`                | bool  |
| `OTEL_SERVICE_NAME`                   | `otel.service_name`           | str   |
| `OTEL_TRACES_EXPORTER`                | `otel.traces_exporter`        | str   |
| `OTEL_METRICS_EXPORTER`               | `otel.metrics_exporter`       | str   |
| `OTEL_EXPORTER_OTLP_ENDPOINT`         | `otel.otlp_endpoint`          | str   |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`  | `otel.otlp_traces_endpoint`   | str   |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | `otel.otlp_metrics_endpoint`  | str   |
| `OTEL_LOG_CORRELATION`                | `otel.log_correlation`        | bool  |
| `OTEL_TRACES_SAMPLE_RATE`             | `otel.traces_sample_rate`     | float |
| `OTEL_DEBUG_CONSOLE_EXPORTER`         | `otel.debug_console_exporter` | bool  |

Standard OTel env var names are used where possible (`OTEL_SERVICE_NAME`,
`OTEL_EXPORTER_OTLP_ENDPOINT`).

### 3.3. defaults.yaml

```yaml
otel:
  enabled: false
  service_name: "family-assistant"
  traces_exporter: "otlp-grpc"
  metrics_exporter: "otlp-grpc"
  otlp_endpoint: "http://localhost:4317"
  log_correlation: true
  traces_sample_rate: 1.0
  debug_console_exporter: false
```

## 4. Setup Module

### 4.1. New File: `src/family_assistant/observability.py`

Entry point: `setup_observability(config, fastapi_app) -> ObservabilityHandle | None`.

Called in `__main__.py` after config loading. The `fastapi_app` instance is passed so that the
already-instantiated app is instrumented via `instrument_app()` (calling `instrument()` alone would
miss it).

See `src/family_assistant/observability.py` for the full implementation. Key design points:

- `BatchSpanProcessor` for network exporters (OTLP gRPC/HTTP) to avoid blocking the event loop
- `SimpleSpanProcessor` only for the debug console exporter (immediate output)
- Lazy imports for OTLP exporters to avoid loading grpcio when using HTTP/console/none
- Returns `None` when disabled; callers just check `if otel_handle:`

### 4.2. Initialization Point

In `__main__.py`, after config loading and before creating the Assistant:

```python
config = load_config()
# ... CLI overrides ...

otel_handle = setup_observability(config.otel, fastapi_app)

if otel_handle and config.otel.log_correlation:
    # Reconfigure log format to include trace/span IDs
    ...

# ... create Assistant, start services ...

# In finally block:
if otel_handle:
    otel_handle.shutdown()
```

## 5. Auto-Instrumentation

These provide tracing with zero code changes to instrumented components:

| Instrumentor | What It Captures                                             |
| ------------ | ------------------------------------------------------------ |
| FastAPI      | HTTP request spans with route, method, status code, duration |
| httpx        | Outbound HTTP spans for the shared `httpx.AsyncClient`       |
| SQLAlchemy   | Database query spans with statement text and duration        |
| Logging      | Injects `otelTraceID`/`otelSpanID` into log records          |

When log correlation is enabled, update the log format in `__main__.py`:

```python
format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
if config.otel.enabled and config.otel.log_correlation:
    format_str = (
        "%(asctime)s - %(name)s - %(levelname)s "
        "[trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] - %(message)s"
    )
```

## 6. Manual Instrumentation

Each module that needs manual spans obtains a tracer via `tracer = trace.get_tracer(__name__)`. When
OTel is disabled, this returns a no-op tracer (zero overhead).

### 6.1. LLM Call Spans

**Files**: `llm/retrying_client.py`, `llm/__init__.py`, `llm/providers/anthropic_client.py`,
`llm/providers/google_genai_client.py`

**Span hierarchy**:

```
llm.generate (RetryingLLMClient)
  └─ llm.generate.attempt (per-provider attempt)
```

**Span attributes** (following
[OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)):

| Attribute                       | Description                             |
| ------------------------------- | --------------------------------------- |
| `gen_ai.system`                 | Provider name (anthropic, google-genai) |
| `gen_ai.request.model`          | Requested model name                    |
| `gen_ai.response.model`         | Actual model that responded             |
| `gen_ai.request.max_tokens`     | Max tokens if set                       |
| `gen_ai.usage.input_tokens`     | Input token count (from response)       |
| `gen_ai.usage.output_tokens`    | Output token count (from response)      |
| `gen_ai.response.finish_reason` | How generation ended                    |
| `llm.is_fallback`               | Whether this was a fallback attempt     |
| `llm.attempt_number`            | 1, 2, or 3 for retry tracking           |

**Async generator pattern**: Since LLM streaming methods are async generators, standard
`with tracer.start_as_current_span()` context managers don't work. Use explicit span management:

```python
async def generate_response_stream(self, messages, tools, tool_choice):
    span = tracer.start_span("llm.generate", attributes={...})
    try:
        with trace.use_span(span, end_on_exit=False):
            async for event in self._inner_generate(...):
                if event.type == "done" and event.metadata:
                    usage = event.metadata.get("usage", {})
                    span.set_attribute("gen_ai.usage.input_tokens", usage.get("prompt_tokens", 0))
                    span.set_attribute("gen_ai.usage.output_tokens", usage.get("completion_tokens", 0))
                yield event
    except Exception as e:
        span.set_status(StatusCode.ERROR, str(e))
        span.record_exception(e)
        raise
    finally:
        span.end()
```

### 6.2. Tool Execution Spans

**File**: `processing.py` - `ProcessingService._execute_single_tool`

**Span name**: `tool.execute.{tool_name}`

| Attribute          | Description                                |
| ------------------ | ------------------------------------------ |
| `tool.name`        | Function name                              |
| `tool.call_id`     | Tool call ID from LLM                      |
| `tool.status`      | "success" or "error"                       |
| `tool.result_size` | Length of result string (not full content) |

Standard `with tracer.start_as_current_span(...)` works here since this is a regular async method.

### 6.3. Conversation Processing Span

**File**: `processing.py` - `ProcessingService.handle_chat_interaction_stream`

**Span name**: `conversation.process`

| Attribute                 | Description                    |
| ------------------------- | ------------------------------ |
| `conversation.id`         | Conversation ID                |
| `conversation.interface`  | Interface type (telegram, web) |
| `conversation.user`       | User name                      |
| `conversation.profile_id` | Service profile ID             |
| `conversation.iterations` | Number of tool loop iterations |

Uses the same explicit span management pattern as LLM spans (async generator).

### 6.4. Context Aggregation Span

**File**: `processing.py` - `ProcessingService._aggregate_context_from_providers`

**Span name**: `context.aggregate`

| Attribute                 | Description                   |
| ------------------------- | ----------------------------- |
| `context.provider_count`  | Number of providers           |
| `context.fragments_count` | Number of fragments collected |

### 6.5. Task Worker Span

**File**: `task_worker.py` - `TaskWorker._process_task`

**Span name**: `task.process.{task_type}`

| Attribute     | Description           |
| ------------- | --------------------- |
| `task.type`   | Task handler name     |
| `task.id`     | Task ID from database |
| `task.status` | success/error         |

Creates new root spans (no incoming request context for background tasks).

## 7. Metrics

Define these metrics using `metrics.get_meter("family_assistant")`:

| Metric                        | Type      | Unit   | Labels            |
| ----------------------------- | --------- | ------ | ----------------- |
| `llm.requests`                | Counter   | count  | model, status     |
| `llm.errors`                  | Counter   | count  | model, error_type |
| `llm.duration`                | Histogram | ms     | model             |
| `llm.tokens.input`            | Counter   | tokens | model             |
| `llm.tokens.output`           | Counter   | tokens | model             |
| `tool.executions`             | Counter   | count  | tool_name, status |
| `tool.duration`               | Histogram | ms     | tool_name         |
| `conversation.turns`          | Counter   | count  | interface         |
| `conversation.iterations`     | Histogram | count  | interface         |
| `task_worker.tasks_processed` | Counter   | count  | task_type, status |

Metrics are recorded alongside span operations (e.g. when an LLM call completes, both the span is
enriched and the counters are incremented).

## 8. Async Context Propagation

OTel's Python SDK uses `contextvars`, which is natively compatible with asyncio:

- Spans created in a coroutine are automatically visible to child coroutines created via
  `asyncio.create_task()`.
- No special handling needed for `asyncio.gather()` or `asyncio.as_completed()`.
- Async generators preserve the `contextvars` context from their creation site.

**Attention areas**:

- **Non-HTTP entry points need explicit root spans**: FastAPI auto-instrumentation creates root
  spans for web requests, but Telegram messages, email webhooks processed outside HTTP context, and
  scheduled tasks have no automatic root span. These entry points must create their own root spans
  explicitly:
  - `TelegramUpdateHandler` (in `telegram/handler.py`): Create a root span when a Telegram update is
    received, before dispatching to `ProcessingService`.
  - `TaskWorker._process_task()`: Create a root span per background task.
  - `EventProcessor`: Create a root span per event processed.
- Tool parallel execution (via `asyncio.create_task()` + `asyncio.as_completed()`): Each tool task
  inherits the parent span context and creates its own child span.

## 9. Privacy

- **Never** record full message content or tool arguments in span attributes.
- Record only metadata: model names, token counts, tool names, result sizes.
- Full conversation content stays in existing logging/database systems.

## 10. End-to-End Trace Examples

### Web request (root span from FastAPI auto-instrumentation)

```
[HTTP POST /api/chat/stream]                          (FastAPI auto-span)
  └─ conversation.process                              (manual)
       ├─ context.aggregate                            (manual)
       │    ├─ [SELECT ... FROM notes ...]             (SQLAlchemy auto-span)
       │    └─ [SELECT ... FROM calendar_events ...]   (SQLAlchemy auto-span)
       ├─ llm.generate                                 (manual - RetryingLLMClient)
       │    └─ llm.generate.attempt                    (manual - GoogleGenAIClient)
       │         └─ [POST https://generativelanguage...] (httpx auto-span)
       ├─ tool.execute.search_notes                    (manual)
       │    └─ [SELECT ... FROM notes ...]             (SQLAlchemy auto-span)
       └─ llm.generate                                 (manual - continuation)
            └─ llm.generate.attempt                    (manual)
                 └─ [POST https://generativelanguage...] (httpx auto-span)
```

### Telegram message (manual root span)

```
telegram.update                                        (manual root span)
  ├─ telegram.message_batch                            (manual - batched messages)
  └─ conversation.process                              (manual)
       ├─ context.aggregate                            (manual)
       ├─ llm.generate                                 (manual)
       │    └─ llm.generate.attempt                    (manual)
       └─ [POST https://api.telegram.org/...]          (httpx auto-span, response)
```

### Background task (manual root span)

```
task.process.llm_callback                              (manual root span)
  └─ conversation.process                              (manual)
       └─ llm.generate                                 (manual)
```

## 11. Implementation Milestones

### Milestone 1+2: Foundation + Auto-Instrumentation — DONE (PR #655)

- [x] Add OTel dependencies to `pyproject.toml` (8 packages)
- [x] Add `OTelConfig` to `config_models.py` with `Field(ge=0.0, le=1.0)` on `traces_sample_rate`
- [x] Add `float` support to `parse_env_value()` in `config_loader.py`
- [x] Add env var mappings to `config_loader.py` (10 `OTEL_*` vars)
- [x] Add defaults to `defaults.yaml`
- [x] Create `observability.py` with `setup_observability()` and `ObservabilityHandle`
- [x] Wire into `__main__.py` (setup, log correlation format, shutdown in finally)
- [x] Enable FastAPI (`instrument_app()`), httpx, SQLAlchemy, and logging auto-instrumentors
- [x] `BatchSpanProcessor` for network exporters, `SimpleSpanProcessor` for debug console only
- [x] Unit tests: `tests/unit/test_observability.py` (13 tests) + config loader tests (4 tests)

### Milestone 3: Manual LLM Spans

- Add spans to `RetryingLLMClient`, `LiteLLMClient`, `AnthropicClient`, `GoogleGenAIClient`
- Capture model name, token usage, finish reason, retry attempt
- Tests verifying LLM span attributes

### Milestone 4: Manual Tool + Conversation Spans

- Add spans to `_execute_single_tool()`, `handle_chat_interaction_stream()`,
  `_aggregate_context_from_providers()`, `TaskWorker._process_task()`
- Tests verifying trace tree structure

### Milestone 5: Metrics

- Define counters and histograms
- Record metrics alongside span operations
- Tests verifying metric emission

### Milestone 6: Documentation and Polish

- Update user guide with OTel configuration instructions
- Add example Docker Compose overlay with Jaeger
- Update architecture docs

## 12. Testing Strategy

- **Unit tests** (`tests/unit/test_observability.py`): Config validation, setup with
  enabled/disabled, exporter selection, shutdown.
- **Integration tests** (`tests/integration/test_otel_spans.py`): Use `InMemorySpanExporter` to
  capture and assert on spans from LLM calls, tool execution, and conversation processing.
- **No impact when disabled**: Existing test suite runs with OTel disabled (the default). The no-op
  API from `opentelemetry-api` ensures zero overhead.
- **Test isolation**: Tests that enable OTel use `InMemorySpanExporter` and reset global providers
  in fixtures.

## 13. Key Files

| File                                          | Change                            |
| --------------------------------------------- | --------------------------------- |
| `pyproject.toml`                              | Add OTel dependencies             |
| `src/family_assistant/config_models.py`       | Add `OTelConfig`                  |
| `src/family_assistant/config_loader.py`       | Add env var mappings              |
| `defaults.yaml`                               | Add `otel` section                |
| `src/family_assistant/observability.py`       | New - setup module                |
| `src/family_assistant/__main__.py`            | Wire setup + shutdown             |
| `src/family_assistant/llm/retrying_client.py` | Manual LLM spans (outer)          |
| `src/family_assistant/llm/providers/*.py`     | Manual LLM spans (per-provider)   |
| `src/family_assistant/processing.py`          | Conversation, tool, context spans |
| `src/family_assistant/telegram/handler.py`    | Root spans for Telegram updates   |
| `src/family_assistant/task_worker.py`         | Root spans for background tasks   |

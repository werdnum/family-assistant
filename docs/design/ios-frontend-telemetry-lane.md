# Frontend telemetry lane (splitting breadcrumbs out of the error log)

## Problem

The iOS app emits a stream of diagnostic "sync breadcrumbs" — stream restarts, stream disconnects,
resync phases, per-operation transport events, alert/inline-error presentations — so that
intermittent connection problems are diagnosable in production without relying on the tester to
describe them. Every one of these is delivered through `ErrorReporter` to `POST /api/errors/`.

That endpoint logs **everything it receives** at `ERROR` (`frontend_logger.error(...)` in
`errors_api.py`). `SQLAlchemyErrorHandler` persists all `ERROR` and above into the `error_logs`
table, which is exactly the table the **engineer profile** reads via `read_error_logs` (and a human
reads via `GET /api/errors/`). So routine, expected breadcrumbs are filed as *errors* and drown the
genuine ones. The engineer profile can no longer find real problems in the noise.

This is a severity-classification bug: breadcrumbs are **telemetry**, not errors. They were never
meant to sit in the error log.

## Why not client-side OpenTelemetry

The backend already depends on OpenTelemetry, but its exporters default to `none` — there is no
collector, and traces do not feed the engineer's error view. Shipping an OTel SDK on iOS would mean
standing up a collector, an OTLP receiver, a storage backend, and a query surface, and it still
would not declutter `/api/errors/` unless the engineer's read path were also rebuilt on top of it.
That is a disproportionate amount of machinery for an app serving a handful of devices (cost/benefit
gate, behaviour-altitude). The right fix is simply to stop mis-filing breadcrumbs as errors.

## Design

Introduce a **severity** on the frontend error report and route non-error reports to a separate,
in-memory telemetry lane that never touches `error_logs`.

### Wire protocol

`FrontendErrorReport` gains an optional `severity` field:

- `severity` absent or `"error"` → **error lane** (logged at `ERROR`, persisted to `error_logs`,
  exactly as today).
- `severity` `"info"` / `"warning"` / `"debug"` → **telemetry lane** (recorded in the ring buffer;
  logged to stdout on the `frontend.telemetry` logger at the mapped level, which is below the
  `error_logs` handler threshold; never persisted to `error_logs`).

The web frontend does not set `severity`, so its reports — including React error-boundary catches,
which legitimately use `error_type: "component_error"` — continue to land in the error log
unchanged. This is why the split keys on an explicit `severity`, not on `error_type`.

### Telemetry ring buffer

`FrontendTelemetryBuffer` is a thread-safe, bounded `deque` ring buffer modelled on the existing
`LLMRequestBuffer` (`llm/request_buffer.py`): a global singleton, default capacity 500, oldest
entries evicted automatically. It holds only non-error frontend reports. It is deliberately
in-memory and non-persistent for now — a process restart drops it, which is acceptable for
live-debugging telemetry (the same tradeoff the LLM request buffer already accepts).

### Read surfaces

- `GET /api/errors/telemetry` — returns recent telemetry records newest-first, with optional
  `component` and `since_minutes` filters. Gated by the same `get_diagnostics_reader` dependency as
  the error endpoints (so the `DIAGNOSTICS_READONLY_TOKEN` unlocks it), and registered ahead of
  `GET /api/errors/{error_id}` so the static path wins.
- `read_frontend_telemetry` — an engineer-profile tool mirroring `get_llm_request_history`, reading
  the same ring buffer, so the engineer can pull the breadcrumb trail on demand while the default
  error view stays clean.

### iOS

`ErrorReporter.ErrorType` gains a `severity`:

- `.component` (`component_error`) → `"info"`. On iOS, `.component` is used exclusively for
  breadcrumb/telemetry events (transport breadcrumbs, resync phases, alert/inline-error presentation
  counters, the sign-in watchdog-breach note), so it maps to the telemetry lane.
- `.handled` (`manual`) and `.uncaught` → `"error"`. Real caught errors and uncaught exceptions stay
  in the error lane, as do the `Chat.stream` error-message reports (which use `.handled`).

`ErrorReportPayload` carries the `severity` string; `makePayload` derives it from the report's
`ErrorType`. No breadcrumb call sites change — they already use `.component`.

## What this deliberately does not do

- No new database table or migration; the buffer is in-memory only.
- No config knob for buffer size (fixed default, like the LLM request buffer).
- No change to the web frontend.
- No attempt to move the rare sign-in watchdog-breach note into the error lane; it is a `.component`
  event and remains queryable in the telemetry lane (reasonable, not ideal — the behaviour-altitude
  tradeoff for an uncommon case).

## Testing

- Backend: POST with `severity="info"` records to the buffer and does **not** log at `ERROR` (so it
  never reaches `error_logs`); POST without `severity` (and with `severity="error"`) still logs at
  `ERROR`. `GET /api/errors/telemetry` returns buffered records and honours filters and the
  diagnostics-reader gate. `read_frontend_telemetry` returns buffer contents.
- iOS: a breadcrumb payload carries `severity: "info"`; a handled/uncaught payload carries
  `severity: "error"`.

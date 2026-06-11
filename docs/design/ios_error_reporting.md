# iOS Native Error Reporting

## Problem

The native iOS app (TestFlight) surfaces failures as on-screen alerts/labels (the many
`errorMessage = error.localizedDescription` sites) and writes to the on-device `os.Logger`. None of
that reaches the server, so a tester hitting a bug in a TestFlight build leaves no trace we can
inspect. The web frontend already reports JavaScript errors to `POST /api/errors/`
(`frontend/src/api/errorClient.ts`), and those land in the server error log
(`SQLAlchemyErrorHandler` → `/errors` UI and `GET /api/errors/`). The native app had no equivalent.

## Goal

Give the native app the same server-side error visibility the web frontend has, so an effective bug
report can be gathered from a TestFlight build without relying on the tester to describe the problem
— covering both:

1. **Handled errors that pop up an alert** — the user-facing `errorMessage` sites.
2. **Uncaught Objective-C exceptions** — global, app-wide capture.

## Reuse of existing infrastructure

We reuse the existing, intentionally **unauthenticated** endpoint:

```
POST /api/errors/        (errors_api.py:report_frontend_error)
```

`^/api(/.*)?$` is in `PUBLIC_PATHS` (`auth.py`), so the report goes through even before sign-in or
when auth is broken. The JSON body matches `FrontendErrorReport`:

| field            | iOS value                                                             |
| ---------------- | --------------------------------------------------------------------- |
| `message`        | `error.localizedDescription` / exception `name: reason`               |
| `stack`          | `nil` for handled errors; `callStackSymbols` for uncaught exceptions  |
| `url`            | synthetic `familyassistant://ios/<component>` (the field is required) |
| `user_agent`     | `FamilyAssistant-iOS/<version> (build <n>; <os version>)`             |
| `component_name` | a stable identifier, e.g. `Notes.editor.save`, `Chat.stream`          |
| `error_type`     | `uncaught` (exceptions) or `manual` (handled)                         |
| `extra_data`     | app version, build, OS version, `is_testflight`, `installation_id`    |

The endpoint is unauthenticated, so reports are **not** attributed to a user via the bearer token
(this also avoids triggering token-refresh side effects from a failure path). Reports instead carry
the device's `installation_id` (the same id `NotificationManager` already persists under
`fa_installation_id`) for correlation.

## Component: `ErrorReporter`

`ios/.../ErrorReporting/ErrorReporter.swift` — a thread-safe singleton (`ErrorReporter.shared`).

- `configure(baseURLProvider:)` — resolves the backend base URL (from
  `AuthManager.validatedServerURL()`); set once at launch.
- `report(_:component:errorType:)` / `report(message:…)` — fire-and-forget. Deduplicates within a
  60s window (matching the web client) and attaches device/build metadata.
- **Best-effort delivery with disk spooling.** When the base URL is unknown (error happened before
  sign-in) or the network send fails, the report is written to a capped spool directory under
  Caches. `flushPersisted()` runs at launch and retries spooled reports.
- `installGlobalHandlers()` — installs an `NSSetUncaughtExceptionHandler` (chained to any
  previously-installed handler) that **synchronously persists** the exception. Async network sends
  cannot complete while the process is terminating, so the report is spooled and delivered on the
  next launch.

### Why no POSIX signal handlers

Hard crashes (Swift runtime traps, `SIGSEGV`, etc.) are deliberately **out of scope**. Apple already
captures those for TestFlight builds and surfaces symbolicated reports in App Store Connect →
TestFlight → Crashes. Re-implementing in-process signal handling is fragile (async-signal-safety
constraints) and would duplicate what Apple already provides. `ErrorReporter` focuses on what
TestFlight cannot see: handled errors and uncaught Objective-C exceptions, tied to our own
server-side error log.

## Wiring

- `FamilyAssistantApp.init()` calls `configure(...)` and `installGlobalHandlers()`; `appContent`
  flushes spooled reports via `.task`.
- Each user-facing `catch` that assigns `errorMessage` from an `Error` gains one additive line:
  `ErrorReporter.shared.report(error, component: "<Area.action>")`. Pure validation messages (e.g.
  "Title and content are required.") are **not** reported — they are expected user input states, not
  bugs.

## Grabbing a bug report from TestFlight

1. **Crashes** → App Store Connect → TestFlight → Crashes (automatic; upload dSYMs for
   symbolication).
2. **Tester-initiated feedback** → TestFlight app screenshot/feedback (includes device + build
   metadata).
3. **Handled errors / ObjC exceptions** → the server error log at `/errors` (filter by
   `logger = frontend.javascript`, since the iOS reports reuse the same ingest path), with the
   `component_name` and `is_testflight` fields identifying native reports.

## Testing

`ErrorReporterTests.swift` exercises payload shape, the dedup window, disk spooling on send failure,
`flushPersisted()` delivery + cleanup, and base-URL resolution, using a `URLProtocol` mock (same
pattern as `NotesAPIClientTests`) and a temporary spool directory.

# iOS App Guide

The native iOS client, a SwiftUI app in `ios/FamilyAssistant/`. It talks to the same backend API as
the web frontend; see [ios/FamilyAssistant/README.md](FamilyAssistant/README.md) for the app's own
structure notes and `docs/design/ios-*.md` for per-feature designs.

## Building and Testing

CI (`.github/workflows/ios-tests.yml`) runs on `macos-26` against an iOS Simulator, using
`xcodebuild build-for-testing` followed by `test-without-building` for the unit and UI test bundles
separately, all with `-scheme FamilyAssistant`. Unit tests live in `FamilyAssistantTests/`, UI tests
in `FamilyAssistantUITests/`.

## Error Reporting and the Telemetry Lane

This is where the split between real errors and diagnostic breadcrumbs originated. The iOS app sends
most breadcrumbs; web voice tool events also set `severity: "info"`.

`ErrorReporting/ErrorReporter.swift` POSTs to `POST /api/errors/`. Each report's `severity` is
derived from its `ErrorType`, and the backend routes on `severity` alone:

- `.handled` (`"manual"`) and `.uncaught` → severity `"error"` → the backend error log, which the
  engineer profile reads via `read_error_logs`.
- `.component` (`"component_error"`) → severity `"info"` → an in-memory telemetry ring buffer, read
  via `GET /api/errors/telemetry` or the engineer-profile `read_frontend_telemetry` tool. Use it
  only for diagnostic breadcrumbs: transport events, resync phases, alert and inline-error counters,
  the sign-in watchdog note. The buffer is dropped on restart.

**Gotcha:** `error_type` does not determine the lane; `severity` does. The web frontend also sends
`error_type: "component_error"` for React error-boundary catches. Its general error client omits
`severity`, so those reports land in the *error* lane. The same `error_type` string therefore means
different things depending on which client sent it — do not infer the lane from it.

Hard crashes (Swift traps, signals) are deliberately out of scope, since Apple already captures
those for TestFlight builds.

See [docs/design/ios_error_reporting.md](../docs/design/ios_error_reporting.md) and
[docs/design/ios-frontend-telemetry-lane.md](../docs/design/ios-frontend-telemetry-lane.md), and
[src/family_assistant/web/CLAUDE.md](../src/family_assistant/web/CLAUDE.md) for the server side of
the contract.

### Reading chat breadcrumbs

Every report carries `client_time`, `client_seq` and `client_session`, stamped when it was raised.
The ring buffer's own timestamp is the time the report reached the server, which can be minutes
later on a bad network, so order events by `client_seq` within a `client_session`.

Chat breadcrumbs also carry `model_id` and `model_role`, which identify the `ChatViewModel` that
wrote them; two models following one thread show up as two `model_id` values.

- `Chat.modelLifecycle`: a model was created or released, or its screen was shown or hidden.
- `Chat.sendStep`: each stage of a send, from the preflight to the server accepting the turn.
- `Chat.requestTiming`: where a request's time went, including how long it waited for a connection.
- `Chat.stateAnomaly`: the client saw a state that should not happen.

## HTTP/1.1 Connection Limits

Production Envoy serves HTTP/1.1 only, so each open event stream holds a whole connection. A
`URLSession` allows six per host by default, and those limits are per session. Long-lived streams
therefore run on `ChatNetworkSessions.streams`, and telemetry on `ChatNetworkSessions.telemetry`, so
neither can queue a send behind them. A new stream must use the stream session.

## Privacy Manifest

`FamilyAssistant/FamilyAssistant/PrivacyInfo.xcprivacy` is a bundled resource of the app target and
is required for App Store submission. Adding a new outbound data flow needs a matching
`NSPrivacyCollectedDataTypes` entry; calling a required-reason API (disk space, boot time, active
keyboards, file timestamps) needs an `NSPrivacyAccessedAPITypes` entry or the upload is rejected.
See [FamilyAssistant/README.md](FamilyAssistant/README.md) for what is currently declared and why.

## Push Notifications

Native push is delivered through APNs. The device-token registration endpoints and every APNs
configuration variable are documented in
[docs/operations/CONFIGURATION_REFERENCE.md](../docs/operations/CONFIGURATION_REFERENCE.md); the
design is in [docs/design/ios_push_notifications.md](../docs/design/ios_push_notifications.md).

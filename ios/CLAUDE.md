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

This is where the split between real errors and diagnostic breadcrumbs originated, and the iOS app
is its only producer — the web frontend never sets `severity` at all.

`ErrorReporting/ErrorReporter.swift` POSTs to `POST /api/errors/`. Each report's `severity` is
derived from its `ErrorType`, and the backend routes on `severity` alone:

- `.handled` (`"manual"`) and `.uncaught` → severity `"error"` → the backend error log, which the
  engineer profile reads via `read_error_logs`.
- `.component` (`"component_error"`) → severity `"info"` → an in-memory telemetry ring buffer, read
  via `GET /api/errors/telemetry` or the engineer-profile `read_frontend_telemetry` tool. Use it
  only for diagnostic breadcrumbs: transport events, resync phases, alert and inline-error counters,
  the sign-in watchdog note. The buffer is dropped on restart.

**Gotcha:** `error_type` does not determine the lane; `severity` does. The web frontend also sends
`error_type: "component_error"` for React error-boundary catches, but because it never sets
`severity` those reports land in the *error* lane. The same `error_type` string therefore means
different things depending on which client sent it — do not infer the lane from it.

Hard crashes (Swift traps, signals) are deliberately out of scope, since Apple already captures
those for TestFlight builds.

See [docs/design/ios_error_reporting.md](../docs/design/ios_error_reporting.md) and
[docs/design/ios-frontend-telemetry-lane.md](../docs/design/ios-frontend-telemetry-lane.md), and
[src/family_assistant/web/CLAUDE.md](../src/family_assistant/web/CLAUDE.md) for the server side of
the contract.

## Push Notifications

Native push is delivered through APNs. The device-token registration endpoints and every APNs
configuration variable are documented in
[docs/operations/CONFIGURATION_REFERENCE.md](../docs/operations/CONFIGURATION_REFERENCE.md); the
design is in [docs/design/ios_push_notifications.md](../docs/design/ios_push_notifications.md).

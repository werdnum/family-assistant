# Automatic TestFlight Delivery for the iOS App

## Goal

Automatically build and upload the `FamilyAssistant` iOS app to TestFlight whenever changes land on
`main`, with zero manual signing or build-number management.

## Decision: Xcode Cloud (hybrid with GitHub Actions)

We deliver to TestFlight via **Xcode Cloud** and keep the existing GitHub Actions
[`ios-tests.yml`](../../.github/workflows/ios-tests.yml) workflow for fast, PR-integrated unit-test
gating.

| Concern | Xcode Cloud | GitHub Actions |
| --- | --- | --- |
| Cost at our volume | Free — 25 compute-hrs/mo; we use ~2.5 hrs | macOS minutes billed 10×; ~$12–25/mo on a private repo |
| Code signing | Automatic via App Store Connect (no secrets) | Must store ASC API key + certs/profiles as repo secrets (fastlane `match`) |
| TestFlight upload | Built in | Add fastlane `pilot` / `altool` step |
| Setup | Mostly clicks in Xcode + App Store Connect | YAML + fastlane + `ExportOptions.plist` + secrets |
| Version control | Workflow config lives in Apple's console | Fully version-controlled |

### Why this split

The build itself is trivial — a clean archive measured **~20s** locally (M-series), and the only
SwiftPM dependencies are `swift-markdown` / `swift-cmark`. End-to-end CI wall time is ~3–5 min,
dominated by runner spin-up, checkout, package resolution, signing, and upload. At an anticipated
20–30 builds/month, performance is a non-factor; **cost and signing-plumbing** decide it.

Xcode Cloud is free at this volume and removes the single most painful part of iOS CI — managing
signing certificates, provisioning profiles, and App Store Connect API keys as CI secrets. The
existing GitHub Actions workflow already gives fast PR test checks, so there is no reason to move
those. Net result:

- **Pull requests:** GitHub Actions runs unit tests (unchanged).
- **Merge to `main`:** Xcode Cloud archives and uploads to TestFlight.

Revisit if the iOS build slows materially or volume grows past the Xcode Cloud free tier, or if we
later want the TestFlight build gated on the Python test suite (which would argue for consolidating
on GitHub Actions).

## What lives in the repo

[`ios/FamilyAssistant/ci_scripts/ci_pre_xcodebuild.sh`](../../ios/FamilyAssistant/ci_scripts/ci_pre_xcodebuild.sh)
— Xcode Cloud runs this automatically before each `xcodebuild` invocation. It stamps
`CURRENT_PROJECT_VERSION` (the source of `CFBundleVersion`, since the target uses
`GENERATE_INFOPLIST_FILE = YES`) with the monotonically increasing `CI_BUILD_NUMBER`, so every
upload has a unique build number with no manual bumping. The edit happens in the ephemeral CI
checkout only and is never committed.

The `ci_scripts` directory must sit next to the `.xcodeproj` and the script must be executable and
not part of any build target.

## One-time console setup (manual)

These steps happen in Xcode / App Store Connect and cannot be scripted from the repo:

1. **App Store Connect** — ensure an app record exists for bundle id `dev.andrewgarrett.assistant`
   under team `H7NBC2S52X`.
2. **Xcode → Product → Xcode Cloud → Create Workflow** (or App Store Connect → the app → Xcode
   Cloud). Grant access to the `werdnum/family-assistant` GitHub repository when prompted.
3. Configure the workflow:
   - **Start condition:** Branch Changes → `main`.
   - **Environment:** latest Xcode (matches `macos-26` / Xcode 26.x used by `ios-tests.yml`).
   - **Action:** Archive — iOS, configuration `Release`.
   - **Post-action:** TestFlight (Internal Testing), select the internal tester group.
4. Let Xcode Cloud manage signing automatically (it provisions a cloud-managed distribution
   certificate; no secrets land in the repo).
5. Trigger the first build manually to confirm the archive uploads and the build number reflects
   `CI_BUILD_NUMBER`.

### Optional: run tests inside Xcode Cloud too

The Archive action can be preceded by a Test action if we ever want the TestFlight build gated on
unit tests independently of the GitHub Actions PR run. We skip this initially to avoid paying for
duplicate test runs.

## Marketing version

`MARKETING_VERSION` (the user-facing `CFBundleShortVersionString`, currently `1.0`) is intentionally
left to manual control — bump it in the Xcode project when shipping a new user-facing version. Only
the build number is automated.

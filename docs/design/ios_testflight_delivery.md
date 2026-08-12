# Automatic TestFlight Delivery for the iOS App

## Goal

Automatically build and upload the `FamilyAssistant` iOS app to TestFlight whenever changes land on
`main`, with zero manual signing or build-number management.

## Decision: Xcode Cloud (hybrid with GitHub Actions)

We deliver to TestFlight via **Xcode Cloud** and keep the existing GitHub Actions
[`ios-tests.yml`](../../.github/workflows/ios-tests.yml) workflow for fast, PR-integrated unit-test
gating.

| Concern            | Xcode Cloud                                  | GitHub Actions                                                             |
| ------------------ | -------------------------------------------- | -------------------------------------------------------------------------- |
| Cost at our volume | Free — 25 compute-hrs/mo; we use ~2.5 hrs    | macOS minutes billed 10×; ~$12–25/mo on a private repo                     |
| Code signing       | Automatic via App Store Connect (no secrets) | Must store ASC API key + certs/profiles as repo secrets (fastlane `match`) |
| TestFlight upload  | Built in                                     | Add fastlane `pilot` / `altool` step                                       |
| Setup              | Mostly clicks in Xcode + App Store Connect   | YAML + fastlane + `ExportOptions.plist` + secrets                          |
| Version control    | Workflow config lives in Apple's console     | Fully version-controlled                                                   |

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

## Build numbers: Xcode Cloud owns them

**Xcode Cloud assigns the TestFlight build number itself — the repo cannot override it.** At the
app-store export step Xcode Cloud passes `buildNumber = CI_BUILD_NUMBER` in its (internally
generated) export options, which re-stamps the `.ipa`'s `CFBundleVersion` *after* the archive is
built. `CI_BUILD_NUMBER` is Xcode Cloud's own per-workflow counter: it starts at 1, increases by 1
per build, and **cannot be offset or reset** from the project, a `ci_scripts` hook, or the App Store
Connect API.

This was learned the hard way. An early attempt stamped `CURRENT_PROJECT_VERSION` from a
`ci_pre_xcodebuild.sh` hook (with and without a large base offset) to force the build number. The
hook ran and the resulting `.xcarchive` genuinely carried the intended number — but the app-store
export overwrote it with `CI_BUILD_NUMBER` on the way to TestFlight, so the uploaded build always
came out as the bare cloud counter. The hook was therefore removed; trying to control the build
number from the repo is futile.

### Implication: bump the marketing version to supersede old builds

TestFlight orders and de-duplicates builds **within a marketing-version train**
(`CFBundleShortVersionString` = `MARKETING_VERSION`), and a higher marketing version always
supersedes a lower one regardless of build number. Xcode Cloud does **not** touch the marketing
version (it is absent from the export options), so it is read straight from the project and is the
one version field the repo fully controls.

Earlier builds uploaded by the now-retired manual script used the **git commit count** as the build
number and reached `1.0 (4972)`. Xcode Cloud's `CI_BUILD_NUMBER` starts at 1, so its builds
(`1.0 (6)`, `1.0 (10)`, …) landed numerically *below* `4972` within the same `1.0` train and
TestFlight treated them as older and refused to install them. Because `CI_BUILD_NUMBER` can't be
raised past `4972`, the fix was to bump `MARKETING_VERSION` to **`1.1`**: `1.1 (N)` is a fresh train
that supersedes `1.0 (4972)` and installs cleanly, and subsequent cloud builds increment normally
within `1.1`. If a future situation ever strands the counter again, bump `MARKETING_VERSION` again —
that, not the build number, is the lever.

### Manual releases are retired

[`ios/testflight-release.sh`](../../ios/testflight-release.sh) previously archived and uploaded to
TestFlight using the git commit count as the build number. Running it alongside Xcode Cloud would
mean two independent counters fighting for the same build-number sequence — whichever ran last would
need to be highest, which no fixed offset can guarantee. The script therefore no longer uploads:
invoking it without `--build-only` errors out and points at Xcode Cloud, and `--build-only` still
archives and exports a local `.ipa` for on-device testing. **Xcode Cloud is the single uploader.**

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
6. **Privacy policy URL** — App Store Connect requires one before a build can be distributed. The
   backend serves the policy unauthenticated at `/privacy`, so enter `SERVER_URL` with `/privacy`
   appended (e.g. `https://assistant.example.com/privacy`) under App Information → Privacy Policy
   URL. Set `PRIVACY_POLICY_OPERATOR` and `PRIVACY_POLICY_CONTACT_EMAIL` on the server first (see
   [CONFIGURATION_REFERENCE.md](../operations/CONFIGURATION_REFERENCE.md)) so the page names a real
   operator and contact.

### Optional: run tests inside Xcode Cloud too

The Archive action can be preceded by a Test action if we ever want the TestFlight build gated on
unit tests independently of the GitHub Actions PR run. We skip this initially to avoid paying for
duplicate test runs.

## Marketing version

`MARKETING_VERSION` (the user-facing `CFBundleShortVersionString`, currently `1.1`) is manually
controlled — bump it in the Xcode project (`project.pbxproj`, all build configs) when shipping a new
user-facing version. The build number is assigned automatically by Xcode Cloud (`CI_BUILD_NUMBER`)
and is not repo-controlled; see
[Build numbers: Xcode Cloud owns them](#build-numbers-xcode-cloud-owns-them).

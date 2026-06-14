#!/usr/bin/env bash
#
# Build the FamilyAssistant iOS app locally and export a signed .ipa for testing.
#
# TestFlight releases are produced by Xcode Cloud, NOT by this script. Xcode
# Cloud assigns the TestFlight build number itself (CI_BUILD_NUMBER, re-stamped
# at the app-store export step) and the repo cannot override it. A second
# uploader (this script) using a different counter would clash with that
# sequence and push "older" builds that TestFlight refuses to install.
# Uploading from here is therefore disabled; this script now only archives and
# exports an .ipa locally for on-device testing.
#
# Usage:
#   ./ios/testflight-release.sh --build-only    # archive + export a local .ipa
#   ./ios/testflight-release.sh --build-only --no-pull   # ... from the current checkout
#
# To ship a TestFlight build, push to the branch Xcode Cloud watches and let the
# cloud workflow build and upload it.
#
# Authentication (App Store Connect API key) -- optional, only for signing:
#   ASC_KEY_ID      App Store Connect API key id      (no default)
#   ASC_ISSUER_ID   App Store Connect API issuer id   (no default)
#   ASC_KEY_PATH    Path to the AuthKey_<id>.p8 file  (default: ~/Downloads/AuthKey_<ASC_KEY_ID>.p8)
#
#   These are read from the environment, or from an untracked credentials file
#   next to this script (ios/.asc-release.env -- gitignored) of the form:
#       ASC_KEY_ID=XXXXXXXXXX
#       ASC_ISSUER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#   When present they let automatic signing fetch profiles under
#   -allowProvisioningUpdates on a host not signed into Xcode Accounts. They are
#   no longer required, since this script does not upload.
#   The .p8, key id, and issuer id are secrets and are never committed
#   (the .gitignore here blocks *.p8 and .asc-release.env).
#
# Other overrides:
#   SCHEME          Xcode scheme   (default: FamilyAssistant)
#   CONFIGURATION   Build config   (default: Release)
#   BUILD_NUMBER    CFBundleVersion to stamp (default: git commit count on HEAD;
#                   cosmetic only here -- the real TestFlight number is assigned
#                   by Xcode Cloud)
#
# Requires Xcode 15.4+.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$SCRIPT_DIR/FamilyAssistant"
PROJECT="$PROJECT_DIR/FamilyAssistant.xcodeproj"
SCHEME="${SCHEME:-FamilyAssistant}"
CONFIGURATION="${CONFIGURATION:-Release}"
EXPORT_OPTIONS="$SCRIPT_DIR/ExportOptions.plist"

DO_PULL=1
UPLOAD_REQUESTED=1
for arg in "$@"; do
    case "$arg" in
        --no-pull)    DO_PULL=0 ;;
        --build-only) UPLOAD_REQUESTED=0 ;;
        -h|--help)
            sed -n '2,42p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

if [[ ! -d "$PROJECT" ]]; then
    echo "ERROR: cannot find Xcode project at $PROJECT" >&2
    exit 1
fi

# TestFlight uploads are retired -- Xcode Cloud owns the build-number sequence.
# Uploading a second, differently-numbered build from here would clash with that
# sequence and push builds TestFlight treats as "older". Refuse upfront and point
# to the supported paths.
if [[ "$UPLOAD_REQUESTED" -eq 1 ]]; then
    cat >&2 <<'EOF'
ERROR: this script no longer uploads to TestFlight.

TestFlight builds are produced by Xcode Cloud, which assigns the build number
itself (CI_BUILD_NUMBER). A second uploader using a different counter would
regress that sequence and push builds TestFlight refuses to install.

To ship a TestFlight build:  push to the branch Xcode Cloud watches.
To build a local .ipa to test on a device:  re-run with --build-only.
EOF
    exit 1
fi

# --- 1. Sync to the latest origin/main ------------------------------------
if [[ "$DO_PULL" -eq 1 ]]; then
    if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
        echo "ERROR: working tree is dirty; commit or stash before a release build." >&2
        echo "       (or pass --no-pull to build the current checkout as-is)" >&2
        exit 1
    fi
    echo "Fetching and fast-forwarding to origin/main..."
    git -C "$REPO_ROOT" fetch origin main
    git -C "$REPO_ROOT" checkout main
    git -C "$REPO_ROOT" merge --ff-only origin/main
fi

echo "Building from commit: $(git -C "$REPO_ROOT" log --oneline -1)"

# --- 2. Pick a build number -----------------------------------------------
# Cosmetic for a local --build-only .ipa; the authoritative TestFlight build
# number is assigned by Xcode Cloud, not here. Default to the commit count on
# HEAD so a locally-installed test build still carries a sensible version.
BUILD_NUMBER="${BUILD_NUMBER:-$(git -C "$REPO_ROOT" rev-list --count HEAD)}"
echo "Using build number (CFBundleVersion): $BUILD_NUMBER"

# --- 3. Resolve App Store Connect API credentials (optional) --------------
# Load identifiers from an untracked, gitignored env file if present so no key
# id / issuer id is ever committed to the repo. These are no longer required
# (no upload happens); when present they let automatic signing fetch profiles.
CREDS_FILE="$SCRIPT_DIR/.asc-release.env"
if [[ -f "$CREDS_FILE" ]]; then
    # shellcheck source=/dev/null
    set -a; source "$CREDS_FILE"; set +a
fi

ASC_KEY_ID="${ASC_KEY_ID:-}"
ASC_ISSUER_ID="${ASC_ISSUER_ID:-}"
ASC_KEY_PATH="${ASC_KEY_PATH:-$HOME/Downloads/AuthKey_${ASC_KEY_ID}.p8}"

# Authentication flags for the archive/export steps. Automatic signing under
# -allowProvisioningUpdates needs an App Store Connect key on a host that is not
# already signed into Xcode Accounts. Populated best-effort whenever a key is
# available; signing falls back to local Xcode Accounts otherwise.
ASC_AUTH_ARGS=()
if [[ -n "$ASC_KEY_ID" && -n "$ASC_ISSUER_ID" && -f "$ASC_KEY_PATH" ]]; then
    ASC_AUTH_ARGS=(
        -authenticationKeyPath "$ASC_KEY_PATH"
        -authenticationKeyID "$ASC_KEY_ID"
        -authenticationKeyIssuerID "$ASC_ISSUER_ID"
    )
fi

# --- 4. Archive -----------------------------------------------------------
BUILD_ROOT="$PROJECT_DIR/build/testflight"
ARCHIVE_PATH="$BUILD_ROOT/FamilyAssistant.xcarchive"
EXPORT_PATH="$BUILD_ROOT/export"
rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT"

echo "Archiving $SCHEME ($CONFIGURATION)..."
xcodebuild \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -configuration "$CONFIGURATION" \
    -destination 'generic/platform=iOS' \
    -archivePath "$ARCHIVE_PATH" \
    -allowProvisioningUpdates \
    "${ASC_AUTH_ARGS[@]}" \
    CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
    archive

# --- 5. Export the .ipa to disk -------------------------------------------
echo "Exporting .ipa to disk..."
# destination=upload in ExportOptions.plist would try to upload, so build a
# temporary export-to-disk plist regardless of the committed setting.
EXPORT_OPTIONS="$BUILD_ROOT/ExportOptions.build-only.plist"
cp "$SCRIPT_DIR/ExportOptions.plist" "$EXPORT_OPTIONS"
/usr/libexec/PlistBuddy -c "Set :destination export" "$EXPORT_OPTIONS"

xcodebuild \
    -exportArchive \
    -archivePath "$ARCHIVE_PATH" \
    -exportPath "$EXPORT_PATH" \
    -exportOptionsPlist "$EXPORT_OPTIONS" \
    -allowProvisioningUpdates \
    "${ASC_AUTH_ARGS[@]}"

echo
echo "Done. Exported .ipa is in: $EXPORT_PATH"
echo "TestFlight uploads are handled by Xcode Cloud, not this script."

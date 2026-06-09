#!/usr/bin/env bash
#
# Build the FamilyAssistant iOS app and upload it to TestFlight (App Store Connect).
#
# Usage:
#   ./ios/testflight-release.sh                 # pull origin/main, archive, upload
#   ./ios/testflight-release.sh --no-pull       # build the current checkout as-is
#   ./ios/testflight-release.sh --build-only    # archive + export .ipa, skip the upload
#
# Authentication (App Store Connect API key):
#   ASC_KEY_ID      App Store Connect API key id      (no default)
#   ASC_ISSUER_ID   App Store Connect API issuer id   (no default)
#   ASC_KEY_PATH    Path to the AuthKey_<id>.p8 file  (default: ~/Downloads/AuthKey_<ASC_KEY_ID>.p8)
#
#   These are read from the environment, or from an untracked credentials file
#   next to this script (ios/.asc-release.env -- gitignored) of the form:
#       ASC_KEY_ID=XXXXXXXXXX
#       ASC_ISSUER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#   Get the values from App Store Connect -> Users and Access -> Integrations ->
#   App Store Connect API. The issuer id is the UUID at the top of that page.
#   The key must have the App Manager or Admin role -- a Developer-role key
#   cannot create the distribution certificate cloud signing needs.
#   The .p8, key id, and issuer id are secrets and are never committed
#   (the .gitignore here blocks *.p8 and .asc-release.env).
#
# Other overrides:
#   SCHEME          Xcode scheme   (default: FamilyAssistant)
#   CONFIGURATION   Build config   (default: Release)
#   BUILD_NUMBER    CFBundleVersion to stamp (default: git commit count on HEAD)
#
# Requires Xcode 15.4+ and an App Store Connect record for the bundle id
# (dev.andrewgarrett.assistant) that this Apple team can publish to.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$SCRIPT_DIR/FamilyAssistant"
PROJECT="$PROJECT_DIR/FamilyAssistant.xcodeproj"
SCHEME="${SCHEME:-FamilyAssistant}"
CONFIGURATION="${CONFIGURATION:-Release}"
EXPORT_OPTIONS="$SCRIPT_DIR/ExportOptions.plist"

DO_PULL=1
DO_UPLOAD=1
for arg in "$@"; do
    case "$arg" in
        --no-pull)    DO_PULL=0 ;;
        --build-only) DO_UPLOAD=0 ;;
        -h|--help)
            sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

# --- 2. Pick a monotonic build number -------------------------------------
# TestFlight rejects a re-used build number for the same marketing version, so
# default to the commit count on HEAD (always increases as work lands).
BUILD_NUMBER="${BUILD_NUMBER:-$(git -C "$REPO_ROOT" rev-list --count HEAD)}"
echo "Using build number (CFBundleVersion): $BUILD_NUMBER"

# --- 3. Resolve App Store Connect API credentials -------------------------
# Load identifiers from an untracked, gitignored env file if present so no key
# id / issuer id is ever committed to the repo.
CREDS_FILE="$SCRIPT_DIR/.asc-release.env"
if [[ -f "$CREDS_FILE" ]]; then
    # shellcheck source=/dev/null
    set -a; source "$CREDS_FILE"; set +a
fi

ASC_KEY_ID="${ASC_KEY_ID:-}"
ASC_ISSUER_ID="${ASC_ISSUER_ID:-}"
ASC_KEY_PATH="${ASC_KEY_PATH:-$HOME/Downloads/AuthKey_${ASC_KEY_ID}.p8}"

if [[ "$DO_UPLOAD" -eq 1 ]]; then
    if [[ -z "$ASC_KEY_ID" || -z "$ASC_ISSUER_ID" ]]; then
        echo "ERROR: ASC_KEY_ID and ASC_ISSUER_ID must be set." >&2
        echo "       Export them, or create $CREDS_FILE (gitignored) with:" >&2
        echo "         ASC_KEY_ID=XXXXXXXXXX" >&2
        echo "         ASC_ISSUER_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" >&2
        echo "       (App Store Connect -> Users and Access -> Integrations -> App Store Connect API)" >&2
        echo "       Or run with --build-only to skip the upload." >&2
        exit 1
    fi
    if [[ ! -f "$ASC_KEY_PATH" ]]; then
        echo "ERROR: API key not found at $ASC_KEY_PATH" >&2
        echo "       Set ASC_KEY_PATH to your AuthKey_${ASC_KEY_ID}.p8, or download it from App Store Connect." >&2
        exit 1
    fi
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
    CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
    archive

# --- 5. Export (+ upload to TestFlight when destination=upload) -----------
EXPORT_AUTH_ARGS=()
if [[ "$DO_UPLOAD" -eq 1 ]]; then
    echo "Exporting and uploading to TestFlight..."
    EXPORT_AUTH_ARGS=(
        -authenticationKeyPath "$ASC_KEY_PATH"
        -authenticationKeyID "$ASC_KEY_ID"
        -authenticationKeyIssuerID "$ASC_ISSUER_ID"
    )
else
    echo "Exporting .ipa only (--build-only); skipping upload..."
    # destination=upload in ExportOptions.plist would still try to upload, so
    # build a temporary export-to-disk plist for the build-only path.
    EXPORT_OPTIONS="$BUILD_ROOT/ExportOptions.build-only.plist"
    /usr/libexec/PlistBuddy -c "Print" "$SCRIPT_DIR/ExportOptions.plist" >/dev/null
    cp "$SCRIPT_DIR/ExportOptions.plist" "$EXPORT_OPTIONS"
    /usr/libexec/PlistBuddy -c "Set :destination export" "$EXPORT_OPTIONS"
fi

xcodebuild \
    -exportArchive \
    -archivePath "$ARCHIVE_PATH" \
    -exportPath "$EXPORT_PATH" \
    -exportOptionsPlist "$EXPORT_OPTIONS" \
    -allowProvisioningUpdates \
    "${EXPORT_AUTH_ARGS[@]}"

echo
if [[ "$DO_UPLOAD" -eq 1 ]]; then
    echo "Done. Build $BUILD_NUMBER uploaded to App Store Connect."
    echo "It will appear in TestFlight after Apple finishes processing (a few minutes)."
else
    echo "Done. Exported .ipa is in: $EXPORT_PATH"
fi

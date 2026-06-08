#!/usr/bin/env bash
#
# Build the FamilyAssistant iOS app and install it on a connected iPhone.
#
# Usage:
#   ./ios/build-and-install.sh                # auto-pick the first connected device
#   ./ios/build-and-install.sh <device-udid>  # target a specific device
#   ./ios/build-and-install.sh --list         # list connected devices and exit
#
# Environment overrides:
#   CONFIGURATION  Debug|Release (default: Debug)
#   SCHEME         Xcode scheme (default: FamilyAssistant)
#
# Requires Xcode 15.4+ and a device that has been paired and trusted in Xcode.
# Code signing uses the team configured in the .xcodeproj (automatic signing).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/FamilyAssistant"
PROJECT="$PROJECT_DIR/FamilyAssistant.xcodeproj"
SCHEME="${SCHEME:-FamilyAssistant}"
CONFIGURATION="${CONFIGURATION:-Debug}"
DERIVED_DATA="$PROJECT_DIR/build/DerivedDataPhone"

if [[ ! -d "$PROJECT" ]]; then
    echo "ERROR: cannot find Xcode project at $PROJECT" >&2
    exit 1
fi

list_devices() {
    # devicectl prints JSON; pull the connected iOS devices out of it.
    local tmp
    tmp="$(mktemp -t fa-devices.XXXXXX.json)"
    trap 'rm -f "$tmp"' RETURN
    xcrun devicectl list devices --quiet --json-output "$tmp" >/dev/null
    /usr/bin/python3 - "$tmp" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
devices = data.get("result", {}).get("devices", [])
for d in devices:
    props = d.get("deviceProperties", {})
    hw = d.get("hardwareProperties", {})
    conn = d.get("connectionProperties", {})
    platform = (hw.get("platform") or "").lower()
    if "ios" not in platform and "iphone" not in platform and "ipad" not in platform:
        continue
    if conn.get("tunnelState") in ("unavailable",):
        continue
    name = props.get("name", "unknown")
    udid = d.get("identifier", "")
    os_ver = props.get("osVersionNumber", "?")
    paired = conn.get("pairingState", "?")
    print(f"{udid}\t{name}\tiOS {os_ver}\t{paired}")
PY
}

if [[ "${1:-}" == "--list" ]]; then
    echo "Connected iOS devices:"
    list_devices | column -t -s $'\t' || true
    exit 0
fi

DEVICE_UDID="${1:-}"
if [[ -z "$DEVICE_UDID" ]]; then
    echo "Discovering connected iOS device..."
    DEVICE_LINE="$(list_devices | head -n 1)"
    if [[ -z "$DEVICE_LINE" ]]; then
        echo "ERROR: no connected iOS device found. Plug in your iPhone, unlock it," >&2
        echo "       trust this Mac, and verify with: $0 --list" >&2
        exit 1
    fi
    DEVICE_UDID="$(echo "$DEVICE_LINE" | cut -f1)"
    DEVICE_NAME="$(echo "$DEVICE_LINE" | cut -f2)"
    echo "Using device: $DEVICE_NAME ($DEVICE_UDID)"
fi

echo "Building $SCHEME ($CONFIGURATION) for device $DEVICE_UDID..."
xcodebuild \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -configuration "$CONFIGURATION" \
    -destination "id=$DEVICE_UDID" \
    -derivedDataPath "$DERIVED_DATA" \
    -allowProvisioningUpdates \
    build

APP_PATH="$DERIVED_DATA/Build/Products/${CONFIGURATION}-iphoneos/${SCHEME}.app"
if [[ ! -d "$APP_PATH" ]]; then
    echo "ERROR: build succeeded but app bundle not found at $APP_PATH" >&2
    exit 1
fi

echo "Installing $APP_PATH to $DEVICE_UDID..."
xcrun devicectl device install app --device "$DEVICE_UDID" "$APP_PATH"

echo
echo "Done. The app is installed on the device."
echo "Launch it from the Home Screen, or run:"
echo "  xcrun devicectl device process launch --device $DEVICE_UDID dev.andrewgarrett.assistant"

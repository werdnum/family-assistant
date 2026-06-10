#!/bin/sh

# ci_pre_xcodebuild.sh
#
# Xcode Cloud runs this automatically before each xcodebuild invocation. It
# stamps the archive's build number (CFBundleVersion) with the monotonically
# increasing Xcode Cloud build number (CI_BUILD_NUMBER) so every TestFlight
# upload is unique without anyone having to bump a counter by hand.
#
# The app target uses GENERATE_INFOPLIST_FILE = YES, so CFBundleVersion is
# derived from the CURRENT_PROJECT_VERSION build setting at build time. We
# rewrite that setting in the ephemeral CI checkout only -- this change is
# never committed back to the repository.
#
# This script must live in a `ci_scripts` directory alongside the .xcodeproj
# (ios/FamilyAssistant/) and must be executable. It must not be added to any
# build target.

set -eu

if [ -z "${CI_BUILD_NUMBER:-}" ]; then
  echo "CI_BUILD_NUMBER is not set; leaving CURRENT_PROJECT_VERSION unchanged."
  exit 0
fi

PROJECT_FILE="${CI_PRIMARY_REPOSITORY_PATH}/ios/FamilyAssistant/FamilyAssistant.xcodeproj/project.pbxproj"

if [ ! -f "$PROJECT_FILE" ]; then
  echo "error: project file not found at $PROJECT_FILE" >&2
  exit 1
fi

echo "Stamping CURRENT_PROJECT_VERSION = ${CI_BUILD_NUMBER}"
/usr/bin/sed -i '' -E \
  "s/CURRENT_PROJECT_VERSION = [^;]+;/CURRENT_PROJECT_VERSION = ${CI_BUILD_NUMBER};/g" \
  "$PROJECT_FILE"

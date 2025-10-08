#!/bin/bash
# Code conformance checker - wrapper around .ast-grep/check-conformance.py
#
# Usage:
#   scripts/check-conformance.sh [files...]
#   scripts/check-conformance.sh           # Check all files
#   scripts/check-conformance.sh src/      # Check specific directory
#   scripts/check-conformance.sh file.py   # Check specific file

set -euo pipefail

# Change to repo root
cd "$(dirname "$0")/.."

# Run conformance checker
exec .ast-grep/check-conformance.py "$@"


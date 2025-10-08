#!/bin/bash
# Audit code conformance exemptions
#
# This script lists all active exemptions from code conformance rules,
# helping to track technical debt and plan cleanup efforts.
#
# Usage:
#   scripts/audit-conformance-exemptions.sh

set -euo pipefail

# Color codes
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Change to repo root
cd "$(dirname "$0")/.."

echo -e "${BLUE}=== Code Conformance Exemptions Audit ===${NC}"
echo ""

# Check if exemptions file exists
if [ ! -f ".ast-grep/exemptions.yml" ]; then
    echo "No exemptions file found"
    exit 0
fi

# Count total exemptions
echo -e "${BLUE}Exemptions Summary:${NC}"
echo ""

# Parse YAML and display exemptions
python3 -c '
import yaml
import sys

with open(".ast-grep/exemptions.yml") as f:
    data = yaml.safe_load(f)

if not data or "exemptions" not in data:
    print("No exemptions defined")
    sys.exit(0)

exemptions = data["exemptions"]
print(f"Total exemption entries: {len(exemptions)}")
print()

for i, exemption in enumerate(exemptions, 1):
    rule = exemption.get("rule", "unknown")
    files = exemption.get("files", [])
    reason = exemption.get("reason", "No reason provided").strip()
    ticket = exemption.get("ticket")

    print(f"{i}. Rule: {rule}")
    print(f"   Files ({len(files)}):")
    for file in files:
        print(f"     - {file}")
    print(f"   Reason: {reason.splitlines()[0]}")  # First line only
    if ticket:
        print(f"   Ticket: {ticket}")
    print()
'

# Show actual violation count
echo ""
echo -e "${BLUE}Actual Violations (exempted):${NC}"
total_violations=$(ast-grep scan --json tests/ 2>/dev/null | python3 -c "import sys, json; print(len(json.load(sys.stdin)))" || echo "0")
echo "Total: $total_violations violations currently exempted"

echo ""
echo -e "${YELLOW}💡 To remove exemptions, update .ast-grep/exemptions.yml and fix the violations${NC}"


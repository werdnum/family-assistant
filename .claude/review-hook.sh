#!/bin/bash

# This hook runs the code review script before git commits
# It reads JSON input from stdin and checks stop_hook_active to prevent infinite loops

# Read JSON input from stdin
JSON_INPUT=$(cat)

# Extract the bash command from the JSON input
# For a Bash tool call, tool_input.command contains the command
COMMAND=$(echo "$JSON_INPUT" | jq -r '.tool_input.command // ""')

# Check if this is a git commit command
if ! echo "$COMMAND" | grep -qE "git\s+(commit|ci)\s+"; then
    # Not a git commit, allow it
    exit 0
fi

# Check if we're already in a stop hook (to prevent infinite loops)
# For PreToolUse hooks, we check if stop_hook_active exists in the JSON
STOP_HOOK_ACTIVE=$(echo "$JSON_INPUT" | jq -r '.stop_hook_active // false')

if [[ "$STOP_HOOK_ACTIVE" == "true" ]]; then
    # We're already in a stop hook, don't review again
    echo "Code review skipped (already in stop hook)" >&2
    exit 0
fi

# Get the repository root
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [[ -z "$REPO_ROOT" ]]; then
    # Not in a git repo, allow the command
    exit 0
fi

# Check if review script exists
REVIEW_SCRIPT="$REPO_ROOT/scripts/review-changes.sh"
if [[ ! -x "$REVIEW_SCRIPT" ]]; then
    # Review script doesn't exist or isn't executable
    exit 0
fi

echo "🔍 Running code review before commit..."
echo ""

# Run the review script
"$REVIEW_SCRIPT"
REVIEW_EXIT_CODE=$?

# Decide what to do based on the review result
if [[ $REVIEW_EXIT_CODE -eq 0 ]]; then
    # No issues found
    echo ""
    echo "✅ Code review passed, proceeding with commit"
    exit 0
elif [[ $REVIEW_EXIT_CODE -eq 1 ]]; then
    # Only warnings found
    echo ""
    echo "⚠️  Code review found warnings, but proceeding with commit"
    exit 0
else
    # Blocking issues found
    echo ""
    echo "❌ Code review found blocking issues"
    echo "Fix the issues above and try again, or override with 'git commit --no-verify'"
    exit 2
fi
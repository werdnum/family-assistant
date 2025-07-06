#!/bin/bash

# This hook runs the code review script before git commits
# It reads JSON input from stdin

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
    # Only warnings found - use exit 1 to show as non-blocking error
    echo "" >&2
    echo "⚠️  Code review found non-blocking issues (warnings/suggestions)" >&2
    echo "" >&2
    echo "Claude Code may want to consider:" >&2
    echo "• Addressing these issues before committing" >&2
    echo "• Adding a note in the commit message acknowledging them" >&2
    echo "• Proceeding if the issues are not relevant to the current task" >&2
    exit 1
else
    # Blocking issues found
    echo ""
    echo "❌ Code review found blocking issues"
    echo ""
    echo "Claude Code should either:"
    echo "1. Fix the issues and try again"
    echo "2. Acknowledge the issues in the commit message to explain why they're acceptable"
    echo "3. Stop and ask for help if unsure how to proceed"
    exit 2
fi
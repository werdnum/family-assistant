#!/bin/bash

# This hook runs the code review script before git commits
# It reads JSON input from stdin

# Read JSON input from stdin
JSON_INPUT=$(cat)

# Extract the bash command from the JSON input
# For a Bash tool call, tool_input.command contains the command
COMMAND=$(echo "$JSON_INPUT" | jq -r '.tool_input.command // ""')

# Check if this is a git commit command
# This regex matches:
# - Simple: git commit -m "message"
# - Compound: git add . && git commit -m "message"
# - With semicolon: git add .; git commit -m "message"
if ! echo "$COMMAND" | grep -qE "(^|[;&|])\s*git\s+(commit|ci)\s+"; then
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

echo "🔍 Running code review before commit..." >&2
echo "" >&2

# Extract the commit message from the git command
# Look for patterns like: -m "message" or --message="message" or --message "message"
COMMIT_MESSAGE=""
if [[ "$COMMAND" =~ -m[[:space:]]+[\"\']([^\"\']+)[\"\'] ]]; then
    COMMIT_MESSAGE="${BASH_REMATCH[1]}"
elif [[ "$COMMAND" =~ --message[[:space:]]*=[[:space:]]*[\"\']([^\"\']+)[\"\'] ]]; then
    COMMIT_MESSAGE="${BASH_REMATCH[1]}"
elif [[ "$COMMAND" =~ --message[[:space:]]+[\"\']([^\"\']+)[\"\'] ]]; then
    COMMIT_MESSAGE="${BASH_REMATCH[1]}"
fi

# Run the review script and capture output
# Pass the commit message via environment variable
COMMIT_MESSAGE="$COMMIT_MESSAGE" REVIEW_OUTPUT=$("$REVIEW_SCRIPT" 2>&1)
REVIEW_EXIT_CODE=$?

# Echo the captured output to stderr
echo "$REVIEW_OUTPUT" >&2

# Decide what to do based on the review result
if [[ $REVIEW_EXIT_CODE -eq 0 ]]; then
    # No issues found
    echo "" >&2
    echo "✅ Code review passed, proceeding with commit" >&2
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
    echo "" >&2
    echo "❌ Code review found blocking issues" >&2
    echo "" >&2
    echo "Claude Code should either:" >&2
    echo "1. Fix the issues and try again" >&2
    echo "2. Acknowledge the issues in the commit message to explain why they're acceptable" >&2
    echo "3. Stop and ask for help if unsure how to proceed" >&2
    exit 2
fi
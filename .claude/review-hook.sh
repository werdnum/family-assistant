#!/bin/bash

# This hook runs formatters, linters, and code review before git commits
# It reads JSON input from stdin

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Read JSON input from stdin
JSON_INPUT=$(cat)

# Extract the bash command from the JSON input
# For a Bash tool call, tool_input.command contains the command
COMMAND=$(echo "$JSON_INPUT" | jq -r '.tool_input.command // ""')

# Check if this is a git commit command or PR creation
# This regex matches:
# - Simple: git commit -m "message"
# - Compound: git add . && git commit -m "message"
# - With semicolon: git add .; git commit -m "message"
# - PR creation: gh pr create
if ! echo "$COMMAND" | grep -qE "(^|[;&|])\s*(git\s+(commit|ci)|gh\s+pr\s+create)\s+"; then
    # Not a git commit or PR creation, allow it
    exit 0
fi

# Get the repository root
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [[ -z "$REPO_ROOT" ]]; then
    # Not in a git repo, allow the command
    exit 0
fi

# Detect if this is a PR creation
IS_PR_CREATE=false
if echo "$COMMAND" | grep -qE "gh\s+pr\s+create"; then
    IS_PR_CREATE=true
fi

echo "${BLUE}🔍 Running pre-commit review...${NC}" >&2
echo "" >&2

# Step 1: Run formatters and linters first
echo "${CYAN}Running formatters and linters...${NC}" >&2

# Run format and lint
if [[ -f "$REPO_ROOT/.venv/bin/poe" ]]; then
    FORMATTER_OUTPUT=$("$REPO_ROOT/.venv/bin/poe" format 2>&1)
    FORMAT_EXIT=$?
    
    if [[ $FORMAT_EXIT -ne 0 ]]; then
        echo "${RED}❌ Formatter failed${NC}" >&2
        echo "$FORMATTER_OUTPUT" >&2
        echo "" >&2
        echo "Please fix formatting issues before committing." >&2
        exit 2
    fi
    
    LINTER_OUTPUT=$("$REPO_ROOT/.venv/bin/poe" lint-fast 2>&1)
    LINT_EXIT=$?
    
    if [[ $LINT_EXIT -ne 0 ]]; then
        echo "${RED}❌ Linter failed${NC}" >&2
        echo "$LINTER_OUTPUT" >&2
        echo "" >&2
        echo "Please fix linting issues before committing." >&2
        exit 2
    fi
    
    echo "${GREEN}✅ Formatting and linting passed${NC}" >&2
else
    echo "${YELLOW}⚠️  poe not found, skipping format/lint${NC}" >&2
fi

echo "" >&2

# Step 2: Handle git add && git commit case
# If the command includes both git add and git commit, we need to:
# 1. Run the git add part first
# 2. Then review the staged changes
# 3. Only proceed with commit if review passes

if echo "$COMMAND" | grep -qE "(^|[;&|])\s*git\s+add\s+.*[;&|]\s*git\s+(commit|ci)"; then
    echo "${CYAN}Detected 'git add && git commit' pattern${NC}" >&2
    echo "${YELLOW}Note: Reviewing changes that will be staged by the git add command${NC}" >&2
    
    # Extract and run just the git add part
    # Replace newlines with spaces to handle multi-line commit messages
    ADD_COMMAND=$(echo "$COMMAND" | tr '\n' ' ' | sed -E 's/^(.*git\s+add\s+[^;&|]*).*/\1/')
    echo "${CYAN}Running: $ADD_COMMAND${NC}" >&2
    eval "$ADD_COMMAND" 2>&1
    ADD_EXIT=$?
    
    if [[ $ADD_EXIT -ne 0 ]]; then
        echo "${RED}❌ git add failed${NC}" >&2
        exit 2
    fi
    echo "" >&2
fi

# Check if review script exists
REVIEW_SCRIPT="$REPO_ROOT/scripts/review-changes.sh"
if [[ ! -x "$REVIEW_SCRIPT" ]]; then
    # Review script doesn't exist or isn't executable
    echo "${YELLOW}Review script not found, skipping code review${NC}" >&2
    exit 0
fi

# Get current HEAD commit hash
HEAD_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "no-head")

# Check for sentinel phrase in the entire command
# The sentinel should be: "Reviewed: HEAD-<commit-hash>"
SENTINEL_PHRASE="Reviewed: HEAD-$HEAD_COMMIT"
HAS_SENTINEL=false

# Simply check if the sentinel phrase appears anywhere in the command
if echo "$COMMAND" | grep -qF "$SENTINEL_PHRASE"; then
    HAS_SENTINEL=true
    echo "${GREEN}✅ Found review override sentinel: $SENTINEL_PHRASE${NC}" >&2
    echo "" >&2
fi

# For PR creation, extract the body text to check for sentinel
if [[ "$IS_PR_CREATE" == "true" ]]; then
    # Extract PR body from --body flag
    PR_BODY=""
    if [[ "$COMMAND" =~ --body[[:space:]]*[\"\']([^\"\']+)[\"\'] ]]; then
        PR_BODY="${BASH_REMATCH[1]}"
    elif [[ "$COMMAND" =~ --body[[:space:]]*=[[:space:]]*[\"\']([^\"\']+)[\"\'] ]]; then
        PR_BODY="${BASH_REMATCH[1]}"
    fi
    
    if [[ -n "$PR_BODY" ]] && echo "$PR_BODY" | grep -qF "$SENTINEL_PHRASE"; then
        HAS_SENTINEL=true
        echo "${GREEN}✅ Found review override sentinel in PR body: $SENTINEL_PHRASE${NC}" >&2
        echo "" >&2
    fi
fi

# Run the review script and capture output
echo "${CYAN}Running code review...${NC}" >&2
echo "" >&2

# Run the review script
REVIEW_OUTPUT=$("$REVIEW_SCRIPT" 2>&1)
REVIEW_EXIT_CODE=$?

# Echo the captured output to stderr
echo "$REVIEW_OUTPUT" >&2

# If sentinel phrase is present, only fail on exit code 2 (blocking issues)
if [[ "$HAS_SENTINEL" == "true" ]]; then
    if [[ $REVIEW_EXIT_CODE -eq 2 ]]; then
        echo "" >&2
        echo "${RED}❌ Code review found blocking issues that cannot be overridden${NC}" >&2
        echo "" >&2
        echo "Even with the review override sentinel, these critical issues must be fixed:" >&2
        echo "• Build-breaking changes" >&2
        echo "• Runtime errors" >&2
        echo "• Security vulnerabilities" >&2
        exit 2
    elif [[ $REVIEW_EXIT_CODE -eq 1 ]]; then
        echo "" >&2
        echo "${YELLOW}⚠️  Minor issues found but proceeding with review override${NC}" >&2
        echo "${GREEN}✅ Commit allowed - you've acknowledged the warnings${NC}" >&2
        exit 0
    else
        echo "" >&2
        echo "${GREEN}✅ No issues found - proceeding${NC}" >&2
        exit 0
    fi
fi

# Without sentinel, block on ANY issues (exit code 1 or 2)
if [[ $REVIEW_EXIT_CODE -eq 0 ]]; then
    # No issues found
    echo "" >&2
    echo "${GREEN}✅ All checks passed, proceeding${NC}" >&2
    exit 0
else
    # Issues found - provide appropriate guidance based on severity
    echo "" >&2
    if [[ $REVIEW_EXIT_CODE -eq 1 ]]; then
        # Minor issues - less intimidating message
        echo "${YELLOW}⚠ Code review found minor issues${NC}" >&2
        echo "" >&2
        echo "${BOLD}To proceed anyway, add this to your commit message:${NC}" >&2
        echo "   ${YELLOW}$SENTINEL_PHRASE${NC}" >&2
        echo "" >&2
        echo "This acknowledges you've reviewed the warnings and decided to proceed." >&2
    else
        # Major issues - stronger message
        echo "${RED}❌ Code review found blocking issues${NC}" >&2
        echo "" >&2
        echo "${BOLD}These issues must be fixed before committing.${NC}" >&2
        echo "The code appears to have serious problems that could break the build or cause runtime errors." >&2
    fi
    exit 2
fi
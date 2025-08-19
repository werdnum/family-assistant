#!/bin/bash

# Read JSON from stdin and extract stop_hook_active
json_input=$(cat)
stop_hook_active=$(echo "$json_input" | jq -r '.stop_hook_active // false')

# Check if stop_hook_active is false (normal stop, not a continuation)
if [ "$stop_hook_active" = "false" ]; then
    # Run format and lint
    if ! (.venv/bin/poe format && .venv/bin/poe lint-fast); then
        echo "Formatting or linting failed. Please fix the issues before proceeding." >&2
        exit 2 # Continue Claude with a failure message
    fi

    echo "Before stopping, please consider:" >&2
    echo >&2
    
    # Check test status using the core verification logic
    source .claude/test-verification-core.sh
    TRANSCRIPT_PATH=$(echo "$json_input" | jq -r '.transcript_path // empty')
    
    if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
        if check_test_status "$TRANSCRIPT_PATH"; then
            echo "• ✓ Tests have been run and are passing" >&2
        else
            echo "• ❌ Tests need attention:" >&2
            echo "  You MUST fix this before finishing" >&2
        fi
    else
        # Fallback to generic message if no transcript available
        echo "• Have you run tests to verify any changes? You MUST run 'poe test' if you have made any changes that could possibly affect the test result." >&2
    fi
    echo >&2
    
    # Check for uncommitted changes
    uncommitted_changes=$(git status --porcelain 2>/dev/null)
    if [ -n "$uncommitted_changes" ]; then
        echo "• There are uncommitted changes. You MUST commit changes before finishing up." >&2
        echo "  Uncommitted changes:" >&2
        echo "$uncommitted_changes" | sed 's/^/    /' >&2
        echo >&2
    fi
    
    # Check for unpushed commits
    # Get the upstream tracking branch
    upstream=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null)
    if [ -n "$upstream" ]; then
        unpushed_commits=$(git log --oneline "$upstream"..HEAD 2>/dev/null)
        if [ -n "$unpushed_commits" ]; then
            echo "• There are unpushed commits. You should push them if the situation warrants it." >&2
            echo "  Unpushed commits:" >&2
            echo "$unpushed_commits" | sed 's/^/    /' >&2
            echo >&2
        fi
    fi
    
    # Always remind to check if request is fulfilled
    echo "• Have you fulfilled the user's request completely?" >&2
fi

# Return 2 to continue Claude Code when stop_hook_active is false
if [ "$stop_hook_active" = "false" ]; then
    exit 2
else
    # Return 0 to allow normal stop when stop_hook_active is true
    exit 0
fi


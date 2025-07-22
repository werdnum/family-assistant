#!/bin/bash

# Read JSON from stdin and extract stop_hook_active
json_input=$(cat)
stop_hook_active=$(echo "$json_input" | jq -r '.stop_hook_active // false')

# Check if stop_hook_active is false (normal stop, not a continuation)
if [ "$stop_hook_active" = "false" ]; then
    echo "Before stopping, please consider:" >&2
    echo >&2
    
    # Suggest running tests
    echo "• Have you run tests to verify any changes? Consider running 'poe test' if appropriate." >&2
    echo >&2
    
    # Check for uncommitted changes
    uncommitted_changes=$(git status --porcelain 2>/dev/null)
    if [ -n "$uncommitted_changes" ]; then
        echo "• There are uncommitted changes. You may want to commit them." >&2
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
            echo "• There are unpushed commits. You may want to push them." >&2
            echo "  Unpushed commits:" >&2
            echo "$unpushed_commits" | sed 's/^/    /' >&2
            echo >&2
        fi
    fi
    
    # Always remind to check if request is fulfilled
    echo "• Have you fulfilled the user's request completely? Ask if they need any additional help." >&2
fi

# Return 2 to continue Claude Code when stop_hook_active is false
if [ "$stop_hook_active" = "false" ]; then
    exit 2
else
    # Return 0 to allow normal stop when stop_hook_active is true
    exit 0
fi
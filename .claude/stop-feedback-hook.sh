#!/bin/bash

# Debug: Show environment and execution info
echo "🔍 STOP HOOK DEBUG:" >&2
echo "   PWD: $(pwd)" >&2
echo "   USER: $(whoami)" >&2
echo "   ONESHOT_MODE: '$ONESHOT_MODE'" >&2
echo "   ONESHOT_STRICT_EXIT: '$ONESHOT_STRICT_EXIT'" >&2
echo "   Script: $(realpath "$0" 2>/dev/null || echo "$0")" >&2
echo >&2

# Read JSON from stdin and extract stop_hook_active
stop_hook_active=$(jq -r '.stop_hook_active // false')
stop_hook_active=$(echo "$json_input" | jq -r '.stop_hook_active // false')

# Check for oneshot mode first - it has different behavior entirely
if [ "$ONESHOT_MODE" = "true" ]; then
    echo "🎯 ONE SHOT MODE - Checking completion status..." >&2
    echo >&2
    
    # Check for failure acknowledgment first
    if [ -f ".claude/FAILURE_REASON" ]; then
        echo "❌ TASK MARKED AS FAILED" >&2
        echo "   Reason: $(cat .claude/FAILURE_REASON)" >&2
        exit 0  # Allow exit with acknowledged failure
    fi
    
    # Collect all issues instead of exiting on first failure
    issues=()
    
    # Check if we're in a git repository
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        issues+=("❌ Not inside a git repository - You MUST initialize git and commit all work")
    else
        # Only do git-related checks if we're in a repository
        
        # Check current branch - suggest feature branch if on main/master
        current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
        if [ "$current_branch" = "main" ] || [ "$current_branch" = "master" ]; then
            issues+=("❌ You're on the $current_branch branch - Create a feature branch first: git checkout -b feature/...")
        fi
        
        # Check for uncommitted changes - MUST be clean
        if [ -n "$(git status --porcelain)" ]; then
            issues+=("❌ There are uncommitted changes - You MUST commit all changes before stopping")
        fi
        
        # Check for unpushed commits - MUST be pushed
        upstream=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null)
        if [ -n "$upstream" ]; then
            unpushed=$(git log --oneline "$upstream"..HEAD)
            if [ -n "$unpushed" ]; then
                issues+=("❌ There are unpushed commits - You MUST push all commits before stopping")
            fi
        elif [ -n "$(git log --oneline | head -1)" ]; then
            # Has commits but no upstream
            issues+=("❌ No upstream branch set - You MUST push to a remote branch before stopping")
        fi
    fi
    
    # Check test status
    if [ -f ".claude/test-verification-core.sh" ]; then
        source .claude/test-verification-core.sh
        TRANSCRIPT_PATH=$(echo "$json_input" | jq -r '.transcript_path // empty')
        
        if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
            if ! check_test_status "$TRANSCRIPT_PATH"; then
                issues+=("❌ Tests have not passed - You MUST run 'poe test' and fix any failures")
            fi
        fi
    fi
    
    # Show all issues at once if any exist
    if [ ${#issues[@]} -gt 0 ]; then
        echo "🎯 ONE SHOT MODE - Multiple issues must be resolved:" >&2
        echo >&2
        for issue in "${issues[@]}"; do
            echo "   $issue" >&2
        done
        echo >&2
        echo "   💡 Fix ALL issues above, or write .claude/FAILURE_REASON if impossible" >&2
        echo "      explaining why (e.g., missing permissions, dependencies, etc.)" >&2
        exit 2  # Block exit with all feedback
    fi
    
    # Success case - all requirements met
    echo "✅ All requirements met for one shot mode:" >&2
    echo "   • Working directory is clean" >&2
    echo "   • All commits pushed to remote" >&2
    echo "   • Tests are passing" >&2
    echo >&2
    
    # Check if we're on a feature branch and suggest creating a PR
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
    if [ "$current_branch" != "main" ] && [ "$current_branch" != "master" ]; then
        echo "💡 Consider creating a pull request:" >&2
        echo "   gh pr create --title \"Brief description\" --body \"Description of changes\"" >&2
        echo >&2
    fi
    
    echo "🎯 ONE SHOT TASK COMPLETE - You may now exit" >&2
    exit 0  # Allow exit

# Regular mode behavior - check if stop_hook_active is false (normal stop, not a continuation)
elif [ "$stop_hook_active" = "false" ]; then
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


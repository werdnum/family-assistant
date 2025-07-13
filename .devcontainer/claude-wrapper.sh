#!/bin/bash
# Wrapper script to ensure virtual environment is activated when running claude

# Auto-pull latest changes
if [ -d "/workspace/.git" ]; then
    cd /workspace
    
    # Save current commit to restore if needed
    ORIG_HEAD=$(git rev-parse HEAD)
    
    # Fetch first to minimize critical period
    git fetch origin >/dev/null 2>&1
    
    # Stash any local changes
    stashed=false
    if ! git diff --quiet || ! git diff --cached --quiet; then
        git stash push -q -m "auto-stash-$$"
        stashed=true
    fi
    
    # Try to pull with rebase
    if ! git pull --rebase --quiet 2>/dev/null; then
        # If rebase fails, abort and restore
        git rebase --abort 2>/dev/null || true
        echo "⚠️  Couldn't pull latest changes due to conflicts"
    fi
    
    # Restore stashed changes
    if [ "$stashed" = true ]; then
        if ! git stash pop -q 2>/dev/null; then
            # If stash pop fails, reset to original state
            git reset --hard "$ORIG_HEAD" >/dev/null 2>&1
            git stash pop -q 2>/dev/null || true
            echo "⚠️  Update skipped due to conflicts with local changes"
        fi
    fi
fi

# Activate the virtual environment if it exists
if [ -f "/workspace/.venv/bin/activate" ]; then
    source /workspace/.venv/bin/activate
fi

# Check if we're running in a TTY context
if [ -t 0 ]; then
    # TTY is available, use throttle_backspaces wrapper
    exec /usr/local/bin/throttle_backspaces.py /home/claude/.npm-global/bin/claude "$@"
else
    # No TTY, run claude directly
    exec /home/claude/.npm-global/bin/claude "$@"
fi
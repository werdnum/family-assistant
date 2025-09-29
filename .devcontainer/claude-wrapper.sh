#!/bin/bash
# Wrapper script to ensure virtual environment is activated when running claude

# Auto-pull latest changes
if [ -d "/workspace/main/.git" ]; then
    cd /workspace
    
    # Save current commit to restore if needed
    ORIG_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
    
    # Fetch first to minimize critical period
    if ! git fetch origin >/dev/null 2>&1; then
        echo "⚠️  Couldn't fetch from remote (network issue or auth problem)"
    else
        # Check if we're behind remote
        LOCAL=$(git rev-parse @ 2>/dev/null || echo "")
        REMOTE=$(git rev-parse @{u} 2>/dev/null || echo "")
        
        if [ -z "$LOCAL" ] || [ -z "$REMOTE" ]; then
            echo "⚠️  Git repository not properly configured"
        elif [ "$LOCAL" != "$REMOTE" ]; then
            # Stash any local changes
            stashed=false
            if ! git diff --quiet || ! git diff --cached --quiet; then
                git stash push -q -m "auto-stash-$$"
                stashed=true
            fi
            
            # Try to pull with rebase
            pull_output=$(git pull --rebase 2>&1)
            pull_result=$?
            
            if [ $pull_result -ne 0 ]; then
                # If rebase fails, abort and restore
                git rebase --abort 2>/dev/null || true
                echo "⚠️  Couldn't pull latest changes: $(echo "$pull_output" | grep -v "^From " | head -1)"
            fi
            
            # Restore stashed changes
            if [ "$stashed" = true ]; then
                if ! git stash pop -q 2>/dev/null; then
                    # If stash pop fails, reset to original state
                    if [ -n "$ORIG_HEAD" ]; then
                        git reset --hard "$ORIG_HEAD" >/dev/null 2>&1
                        git stash pop -q 2>/dev/null || true
                    fi
                    echo "⚠️  Update skipped due to conflicts with local changes"
                fi
            fi
        fi
    fi
fi

# Activate the virtual environment if it exists
if [ -f "/workspace/main/.venv/bin/activate" ]; then
    source /workspace/.venv/bin/activate
fi

export BASH_DEFAULT_TIMEOUT_MS=300000
export BASH_MAX_TIMEOUT_MS=3600000
export CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1

exec /home/claude/.npm-global/bin/claude "$@"


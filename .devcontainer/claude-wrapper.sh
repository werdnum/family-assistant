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

# Set up Claude settings for this worktree if not already configured
if [ ! -f ".claude/settings.local.json" ]; then
    mkdir -p .claude

    # Copy default settings if available
    if [ -f "/opt/claude-settings/settings.local.json" ]; then
        cp /opt/claude-settings/settings.local.json .claude/
    fi

    # Override with user's settings if provided
    if [ -f "/home/claude/.claude/settings.local.json" ]; then
        cp /home/claude/.claude/settings.local.json .claude/
    fi
fi

# Copy user's CLAUDE.local.md if provided and not already present
if [ -f "/home/claude/.claude/CLAUDE.local.md" ] && [ ! -f ".claude/CLAUDE.local.md" ]; then
    mkdir -p .claude
    cp /home/claude/.claude/CLAUDE.local.md .claude/
fi

# Set up Python virtual environment if this is a Python project
if [ -f "pyproject.toml" ]; then
    # Create virtual environment if it doesn't exist or is invalid
    if [ ! -d ".venv" ] || [ ! -f ".venv/bin/python" ]; then
        echo "Creating virtual environment..."
        rm -rf .venv
        uv venv .venv

        # Activate and install dependencies
        source .venv/bin/activate
        echo "Installing Python dependencies..."
        uv sync --extra dev
        uv pip install poethepoet pytest-xdist
    else
        # Just activate existing venv
        source .venv/bin/activate
    fi
fi

# Install frontend dependencies if needed
if [ -f "frontend/package.json" ] && [ ! -d "frontend/node_modules" ]; then
    echo "Installing frontend dependencies..."
    npm install --prefix frontend
fi

export BASH_DEFAULT_TIMEOUT_MS=300000
export BASH_MAX_TIMEOUT_MS=3600000
export CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1

exec /home/claude/.npm-global/bin/claude "$@"


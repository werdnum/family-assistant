#!/bin/bash
# Wrapper script to ensure virtual environment is activated when running claude

# shellcheck disable=SC1091
source /usr/local/bin/wrapper-common.sh

wrapper_common_setup

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

# Update Claude plugin marketplaces (fetches latest from GitHub)
echo "🔄 Updating Claude plugin marketplaces..."
/home/claude/.npm-global/bin/claude plugin marketplace update >/dev/null 2>&1 || {
    echo "⚠️  Warning: Failed to update plugin marketplaces"
}

export CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1

exec /home/claude/.npm-global/bin/claude "$@"

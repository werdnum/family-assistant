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

# The native installer (https://claude.ai/install.sh) installs the real binary
# to ~/.local/bin/claude. Invoke it directly to bypass the wrapper symlink in
# /usr/local/bin and avoid infinite recursion.
CLAUDE_BIN=/home/claude/.local/bin/claude

# Update Claude plugin marketplaces (fetches latest from GitHub)
echo "🔄 Updating Claude plugin marketplaces..."
"$CLAUDE_BIN" plugin marketplace update >/dev/null 2>&1 || {
    echo "⚠️  Warning: Failed to update plugin marketplaces"
}

export CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR=1

exec "$CLAUDE_BIN" "$@"

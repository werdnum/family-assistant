#!/bin/bash
# SessionStart hook for Claude Code
# Runs setup-workspace when CLAUDE_CODE_REMOTE is true
# Sets up environment variables for the activated virtual environment

set -e

# Check if CLAUDE_CODE_REMOTE is set
if [ "${CLAUDE_CODE_REMOTE:-false}" != "true" ]; then
    exit 0
fi

# Go to project root
cd "$(git rev-parse --show-toplevel)"

# Success marker file
SUCCESS_MARKER=".venv/.setup_complete"

# If setup has already run successfully, just set env vars and exit
if [ -f "$SUCCESS_MARKER" ]; then
    VENV_PATH="$(pwd)/.venv"
    PYTHON_BIN="${VENV_PATH}/bin"

    if [ -n "$CLAUDE_ENV_FILE" ]; then
        echo "export VIRTUAL_ENV='${VENV_PATH}'" >> "$CLAUDE_ENV_FILE"
        echo "export PATH='${PYTHON_BIN}:${PATH}'" >> "$CLAUDE_ENV_FILE"
    fi
    echo "✓ Workspace setup previously completed, environment configured." >&2
    exit 0
fi

# Run the setup script
echo "🚀 Setting up workspace for remote Claude Code session..." >&2
if [ -f "scripts/setup-workspace.sh" ]; then
    # Capture output and display on error
    if ! bash scripts/setup-workspace.sh > setup.log 2>&1; then
        echo "❌ Workspace setup failed. See setup.log for details." >&2
        cat setup.log >&2
        exit 1
    fi
fi

# Set environment variables for the activated virtual environment
VENV_PATH="$(pwd)/.venv"
PYTHON_BIN="${VENV_PATH}/bin"

# Persist environment variables for subsequent bash commands
if [ -n "$CLAUDE_ENV_FILE" ]; then
    echo "export VIRTUAL_ENV='${VENV_PATH}'" >> "$CLAUDE_ENV_FILE"
    echo "export PATH='${PYTHON_BIN}:${PATH}'" >> "$CLAUDE_ENV_FILE"
fi

# Create success marker
touch "$SUCCESS_MARKER"

echo "✓ Workspace setup complete" >&2

exit 0

#!/bin/bash
# SessionStart hook for Claude Code
# Runs setup-workspace when CLAUDE_CODE_REMOTE is true
# Sets up environment variables for the activated virtual environment

set -e

# Check if CLAUDE_CODE_REMOTE is set
if [ "${CLAUDE_CODE_REMOTE:-false}" != "true" ]; then
    exit 0
fi

echo "🚀 Setting up workspace for remote Claude Code session..." >&2

# Run the setup script from the project root
cd "$(git rev-parse --show-toplevel)"
if [ -f "scripts/setup-workspace.sh" ]; then
    bash scripts/setup-workspace.sh
fi

# Ensure pgserver extra is installed
echo "🔍 Ensuring pgserver extra is installed..." >&2
source .venv/bin/activate
uv sync --extra dev --extra pgserver

# Set environment variables for the activated virtual environment
VENV_PATH="$(pwd)/.venv"
PYTHON_BIN="${VENV_PATH}/bin"

# Persist environment variables for subsequent bash commands
if [ -n "$CLAUDE_ENV_FILE" ]; then
    echo "export VIRTUAL_ENV='${VENV_PATH}'" >> "$CLAUDE_ENV_FILE"
    echo "export PATH='${PYTHON_BIN}:${PATH}'" >> "$CLAUDE_ENV_FILE"
fi

echo "✓ Workspace setup complete" >&2

exit 0


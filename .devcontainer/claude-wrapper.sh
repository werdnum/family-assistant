#!/bin/bash
# Wrapper script to ensure virtual environment is activated when running claude

# Activate the virtual environment if it exists
if [ -f "/workspace/.venv/bin/activate" ]; then
    source /workspace/.venv/bin/activate
fi

# Execute the real claude command with all arguments
exec /home/claude/.npm-global/bin/claude "$@"
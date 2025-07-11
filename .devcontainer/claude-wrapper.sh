#!/bin/bash
# Wrapper script to ensure virtual environment is activated when running claude

# Activate the virtual environment if it exists
if [ -f "/workspace/.venv/bin/activate" ]; then
    source /workspace/.venv/bin/activate
fi

# Execute the real claude command with throttle_backspaces wrapper
exec /usr/local/bin/throttle_backspaces.py /home/claude/.npm-global/bin/claude "$@"
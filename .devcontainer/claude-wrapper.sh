#!/bin/bash
# Wrapper script to ensure virtual environment is activated when running claude

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
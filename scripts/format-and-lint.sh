#!/bin/sh

# Default to src and tests directories if no arguments provided
if [ $# -eq 0 ]; then
    TARGETS="src tests"
else
    # Filter arguments to only include Python files and directories
    TARGETS=""
    for arg in "$@"; do
        if [ -d "$arg" ]; then
            # If it's a directory, include it
            TARGETS="$TARGETS $arg"
        elif [ -f "$arg" ] && echo "$arg" | grep -q '\.py$'; then
            # If it's a Python file, include it
            TARGETS="$TARGETS $arg"
        elif [ -f "$arg" ]; then
            # If it's a non-Python file, skip it with a warning
            echo "Warning: Skipping non-Python file: $arg"
        else
            echo "Error: File or directory not found: $arg"
            exit 1
        fi
    done
    
    # If no valid targets after filtering, exit
    if [ -z "$TARGETS" ]; then
        echo "Error: No Python files or directories to lint"
        exit 1
    fi
fi

# Run linters on the filtered targets
${VIRTUAL_ENV:-.venv}/bin/ruff check --fix --preview --ignore=E501 $TARGETS && \
${VIRTUAL_ENV:-.venv}/bin/ruff format --preview $TARGETS && \
${VIRTUAL_ENV:-.venv}/bin/basedpyright $TARGETS && \
${VIRTUAL_ENV:-.venv}/bin/pylint -j0 $TARGETS

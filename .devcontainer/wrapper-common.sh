#!/bin/bash
# Shared setup for dev-container CLI wrappers (claude, gemini, codex).
# Source this script from a tool-specific wrapper; it performs the shared
# workspace setup (git auto-pull, Python venv activation, frontend deps)
# so every tool inherits the same environment.

# Auto-pull latest changes
wrapper_common_auto_pull() {
    [ -d ".git" ] || return 0

    local orig_head
    orig_head=$(git rev-parse HEAD 2>/dev/null || echo "")

    if ! git fetch origin >/dev/null 2>&1; then
        echo "⚠️  Couldn't fetch from remote (network issue or auth problem)"
        return 0
    fi

    local local_rev remote_rev
    local_rev=$(git rev-parse @ 2>/dev/null || echo "")
    # shellcheck disable=SC1083  # @{u} is git upstream syntax, not shell brace expansion
    remote_rev=$(git rev-parse @{u} 2>/dev/null || echo "")

    if [ -z "$local_rev" ] || [ -z "$remote_rev" ]; then
        echo "⚠️  Git repository not properly configured"
        return 0
    fi

    if [ "$local_rev" = "$remote_rev" ]; then
        return 0
    fi

    local stashed=false
    if ! git diff --quiet || ! git diff --cached --quiet; then
        git stash push -q -m "auto-stash-$$"
        stashed=true
    fi

    local pull_output pull_result
    pull_output=$(git pull --rebase 2>&1)
    pull_result=$?

    if [ $pull_result -ne 0 ]; then
        git rebase --abort 2>/dev/null || true
        echo "⚠️  Couldn't pull latest changes: $(echo "$pull_output" | grep -v "^From " | head -1)"
    fi

    if [ "$stashed" = true ]; then
        if ! git stash pop -q 2>/dev/null; then
            if [ -n "$orig_head" ]; then
                git reset --hard "$orig_head" >/dev/null 2>&1
                git stash pop -q 2>/dev/null || true
            fi
            echo "⚠️  Update skipped due to conflicts with local changes"
        fi
    fi
}

# Set up & activate Python virtual environment if this is a Python project.
# Exports the activated venv so the exec'd tool inherits it.
wrapper_common_activate_venv() {
    [ -f "pyproject.toml" ] || return 0

    if [ ! -d ".venv" ] || [ ! -f ".venv/bin/python" ]; then
        echo "Creating virtual environment..."
        rm -rf .venv
        uv venv .venv

        # shellcheck disable=SC1091
        source .venv/bin/activate
        echo "Installing Python dependencies..."
        uv sync --extra dev
        uv pip install poethepoet pytest-xdist
    else
        # shellcheck disable=SC1091
        source .venv/bin/activate
    fi
}

# Install frontend dependencies if needed.
wrapper_common_install_frontend() {
    if [ -f "frontend/package.json" ] && [ ! -d "frontend/node_modules" ]; then
        echo "Installing frontend dependencies..."
        npm install --prefix frontend
    fi
}

# Bash timeout defaults shared by all wrapped CLIs.
wrapper_common_export_env() {
    export BASH_DEFAULT_TIMEOUT_MS=300000
    export BASH_MAX_TIMEOUT_MS=3600000
}

# Run the full shared setup pipeline.
wrapper_common_setup() {
    wrapper_common_auto_pull
    wrapper_common_activate_venv
    wrapper_common_install_frontend
    wrapper_common_export_env
}

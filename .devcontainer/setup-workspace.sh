#!/bin/bash
set -e

# Ensure uv is in PATH
export PATH="/root/.local/bin:$PATH"

echo "Starting workspace setup..."

# Clone repository if CLAUDE_PROJECT_REPO is set
if [ -n "$CLAUDE_PROJECT_REPO" ] && [ ! -d ".git" ]; then
    echo "Cloning repository from $CLAUDE_PROJECT_REPO..."
    git clone "$CLAUDE_PROJECT_REPO" .
fi

# Check if we're in a Python project
if [ -f "pyproject.toml" ]; then
    echo "Python project detected. Setting up virtual environment..."
    
    # Create virtual environment (recreate if it exists but is broken)
    if [ ! -d ".venv" ] || ! [ -x ".venv/bin/python" ]; then
        echo "Creating fresh virtual environment..."
        rm -rf .venv
        uv venv .venv
    fi
    
    # Install dependencies
    echo "Installing Python dependencies..."
    uv pip install -e ".[dev]"
    
    # Install pre-commit hooks if available
    if [ -f ".pre-commit-config.yaml" ]; then
        echo "Installing pre-commit hooks..."
        .venv/bin/pre-commit install || true
    fi
fi

# Install Node dependencies if package.json exists
if [ -f "package.json" ]; then
    echo "Installing Node.js dependencies..."
    npm install
fi

# Install frontend dependencies
if [ -f "frontend/package.json" ]; then
    echo "Installing frontend dependencies..."
    (cd frontend && npm install)
fi

# Install Playwright browsers if needed
if [ -f "pyproject.toml" ] && grep -q "playwright" pyproject.toml; then
    echo "Installing Playwright browsers..."
    .venv/bin/playwright install chromium || true
fi

# Copy Claude configuration if provided
if [ -f "/home/claude/.claude/settings.local.json" ]; then
    mkdir -p .claude
    cp /home/claude/.claude/settings.local.json .claude/
fi

if [ -f "/home/claude/.claude/CLAUDE.local.md" ]; then
    mkdir -p .claude
    cp /home/claude/.claude/CLAUDE.local.md .claude/
fi

echo "Workspace setup complete!"

# Execute the command passed to the container
exec "$@"
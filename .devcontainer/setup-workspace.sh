#!/bin/bash
set -e

# Ensure uv is in PATH
export PATH="/root/.local/bin:$PATH"

echo "Starting workspace setup..."

# Clone repository if CLAUDE_PROJECT_REPO is set
if [ -n "$CLAUDE_PROJECT_REPO" ] && [ ! -d ".git" ]; then
    echo "Cloning repository from $CLAUDE_PROJECT_REPO..."
    
    # Use GitHub token if available
    if [ -n "$GITHUB_TOKEN" ]; then
        # Extract repo path from URL
        REPO_PATH=$(echo "$CLAUDE_PROJECT_REPO" | sed -E 's|https://github.com/||; s|\.git$||')
        AUTHED_URL="https://${GITHUB_TOKEN}@github.com/${REPO_PATH}.git"
        git clone "$AUTHED_URL" .
    else
        git clone "$CLAUDE_PROJECT_REPO" .
    fi
fi

# Check if we're in a Python project
if [ -f "pyproject.toml" ]; then
    echo "Python project detected. Setting up virtual environment..."
    
    # Always create fresh virtual environment in isolated workspace
    echo "Creating fresh virtual environment..."
    rm -rf .venv
    uv venv .venv
    
    # Activate the virtual environment
    source .venv/bin/activate
    
    # Install dependencies
    echo "Installing Python dependencies..."
    uv pip install -e ".[dev]"
    
    # Ensure poethepoet is installed
    echo "Installing poethepoet..."
    uv pip install poethepoet
    
    # Ensure pytest-xdist is installed for parallel test execution
    echo "Installing pytest-xdist..."
    uv pip install pytest-xdist
    
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
    echo "Installing Playwright browsers for Python environment..."
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

# Configure MCP servers for Claude
echo "Configuring MCP servers..."
cd /workspace
claude mcp add --scope local context7 "deno run -A npm:@upstash/context7-mcp" || true
claude mcp add --scope local scraper "/workspace-bin/scrape_mcp" || true
claude mcp add --scope local serena "sh -c 'uvx --from git+https://github.com/oraios/serena serena-mcp-server --context ide-assistant --project /workspace'" || true
claude mcp add --scope local playwright "npx @playwright/mcp@latest --allowed-origins localhost:8000;localhost:5173;localhost:8001;unpkg.com;cdn.jsdelivr.net;cdnjs.cloudflare.com;cdn.simplecss.org --headless --isolated --browser chromium" || true

echo "Workspace setup complete!"

# Execute the command passed to the container
exec "$@"
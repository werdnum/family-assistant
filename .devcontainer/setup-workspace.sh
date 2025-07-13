#!/bin/bash
set -e

# Ensure uv is in PATH and include user paths
export PATH="/home/claude/.npm-global/bin:/home/claude/.deno/bin:/home/claude/.local/bin:/root/.local/bin:$PATH"

echo "Starting workspace setup..."

# Create claude user with UID 1001 if needed
if ! id -u claude >/dev/null 2>&1; then
    useradd -m -u 1001 -g 1001 -s /bin/bash claude
fi

# Fix permissions on persistent directories
# The claude-home volume is mounted at /home/claude
mkdir -p /home/claude/.claude
chown claude:claude /home/claude
chown -R claude:claude /home/claude/.claude
chmod 755 /home/claude
chmod -R 755 /home/claude/.claude || true

# Fix postgres data directory permissions
mkdir -p /var/lib/postgresql/data
chown -R 999:999 /var/lib/postgresql/data
chmod 700 /var/lib/postgresql/data

# Check if /home/claude is a mount point
HOME_IS_MOUNTED=false
if mountpoint -q /home/claude 2>/dev/null || [ -n "$(findmnt -n -o SOURCE --target /home/claude 2>/dev/null)" ]; then
    echo "/home/claude is mounted - will install tools if needed"
    HOME_IS_MOUNTED=true
fi

# Install npm tools if they don't exist (e.g., when home is mounted)
if [ "$HOME_IS_MOUNTED" = "true" ] && [ ! -f "/home/claude/.npm-global/bin/claude" ]; then
    echo "Installing npm tools in mounted home directory..."
    
    # Ensure npm global directory exists
    mkdir -p /home/claude/.npm-global
    export NPM_CONFIG_PREFIX=/home/claude/.npm-global
    
    # Install tools
    npm install -g @anthropic-ai/claude-code
    npm install -g @google/gemini-cli
    npm install -g playwright
    
    # Install Playwright browsers
    if [ -n "$PLAYWRIGHT_BROWSERS_PATH" ]; then
        npx playwright install chromium
    fi
    
    # Install LLM tools using uv
    export PATH="/home/claude/.local/bin:$PATH"
    uv tool install --with llm-gemini --with llm-openrouter --with llm-fragments-github llm
    
    # Ensure proper ownership of installed tools (avoid recursive chown on large dirs)
    # Only chown the bin directory and key files, not the entire node_modules
    chown claude:claude /home/claude/.npm-global
    chown -R claude:claude /home/claude/.npm-global/bin
    [ -d "/home/claude/.local" ] && chown -R claude:claude /home/claude/.local
    
    echo "npm tools installation complete"
fi

# If running as root, ensure proper ownership later
RUNNING_AS_ROOT=false
if [ "$(id -u)" = "0" ]; then
    RUNNING_AS_ROOT=true
fi

# Change to workspace directory
cd /workspace

# Clone repository if CLAUDE_PROJECT_REPO is set and .git doesn't exist
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
    
    # Ensure claude owns the workspace if running as root
    if [ "$RUNNING_AS_ROOT" = "true" ]; then
        chown -R claude:claude /workspace
    fi
elif [ -d ".git" ]; then
    echo "Workspace already exists, updating dependencies..."
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
# Copy settings.local.json if it exists in the container
if [ -f "/opt/claude-settings/settings.local.json" ]; then
    echo "Copying default Claude settings..."
    mkdir -p .claude
    cp /opt/claude-settings/settings.local.json .claude/
fi

# Override with user's settings if provided
if [ -f "/home/claude/.claude/settings.local.json" ]; then
    mkdir -p .claude
    cp /home/claude/.claude/settings.local.json .claude/
fi

# Copy user's CLAUDE.local.md if provided
if [ -f "/home/claude/.claude/CLAUDE.local.md" ]; then
    mkdir -p .claude
    cp /home/claude/.claude/CLAUDE.local.md .claude/
fi

# Configure MCP servers for Claude
echo "Configuring MCP servers..."
cd /workspace

# Find full paths for executables
DENO_PATH=$(which deno 2>/dev/null || echo "/home/claude/.deno/bin/deno")
UVX_PATH=$(which uvx 2>/dev/null || echo "uvx")
NPX_PATH=$(which npx 2>/dev/null || echo "npx")

# Configure MCP servers with full paths (bypass wrapper to avoid git pull)
CLAUDE_BIN="/home/claude/.npm-global/bin/claude"
# Remove existing servers if any exist
if $CLAUDE_BIN mcp list 2>/dev/null | grep -q ":"; then
    for server in $($CLAUDE_BIN mcp list | cut -d: -f1); do
        $CLAUDE_BIN mcp remove --scope user "$server" || true
    done
fi
$CLAUDE_BIN mcp add --scope user context7 $(which npx) -- -y -q @upstash/context7-mcp
$CLAUDE_BIN mcp add --scope user scraper /workspace-bin/scrape_mcp
$CLAUDE_BIN mcp add --scope user serena -- sh -c "$(which uvx) -q --from git+https://github.com/oraios/serena serena-mcp-server --context ide-assistant --project /workspace"
$CLAUDE_BIN mcp add --scope user playwright $(which npx) -- -y -q @playwright/mcp@latest --allowed-origins "localhost:8000;localhost:5173;localhost:8001;unpkg.com;cdn.jsdelivr.net;cdnjs.cloudflare.com;cdn.simplecss.org" --headless --isolated --browser chromium

# Ensure proper ownership if running as root
if [ "$RUNNING_AS_ROOT" = "true" ]; then
    # Ensure claude owns the entire workspace
    chown -R claude:claude /workspace
fi

echo "Workspace setup complete!"

# Execute the command passed to the container
exec "$@"
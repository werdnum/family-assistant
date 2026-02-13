#!/bin/bash
set -e

# Ensure uv is in PATH and include user paths and PostgreSQL binaries
export PATH="/workspace/main/.venv/bin:/home/claude/.npm-global/bin:/home/claude/.deno/bin:/home/claude/.local/bin:/root/.local/bin:/usr/lib/postgresql/17/bin:$PATH"

# Role detection: defaults to claude for full setup
CONTAINER_ROLE="${CONTAINER_ROLE:-claude}"
echo "Container role: $CONTAINER_ROLE"

# Backend: wait for workspace, skip Claude-specific setup
if [ "$CONTAINER_ROLE" = "backend" ]; then
    WORKTREE_DIR="/workspace/${WORKTREE_NAME:-main}"
    MAX_WAIT=300
    WAITED=0

    echo "Backend mode: waiting for workspace setup by claude container..."
    while [ ! -f "$WORKTREE_DIR/.venv/.ready" ]; do
        if [ $WAITED -ge $MAX_WAIT ]; then
            echo "ERROR: Timeout waiting for workspace at $WORKTREE_DIR/.venv/.ready"
            echo "       Start the claude container first to set up the workspace."
            exit 1
        fi
        echo "Waiting for workspace setup... ($WAITED/$MAX_WAIT sec)"
        sleep 5
        WAITED=$((WAITED + 5))
    done

    # Verify the venv is usable
    if ! "$WORKTREE_DIR/.venv/bin/python" --version >/dev/null 2>&1; then
        echo "ERROR: Virtual environment exists but is not usable"
        exit 1
    fi

    echo "Workspace ready, starting backend..."
    cd "$WORKTREE_DIR"
    source .venv/bin/activate
    exec "$@"
fi

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

# PostgreSQL data directory setup not needed
# - In Kubernetes: PostgreSQL container manages its own data
# - In standalone: Subprocess PostgreSQL uses temporary directories

# Check if /home/claude is a mount point
HOME_IS_MOUNTED=false
if mountpoint -q /home/claude 2>/dev/null || [ -n "$(findmnt -n -o SOURCE --target /home/claude 2>/dev/null)" ]; then
    echo "/home/claude is mounted - will install tools if needed"
    HOME_IS_MOUNTED=true
fi

# Install npm tools if they don't exist (e.g., when home is mounted)
if [ "$HOME_IS_MOUNTED" = "true" ] && ( ! which claude >/dev/null 2>&1 || ! which happy >/dev/null 2>&1 ); then
    echo "Installing npm tools in mounted home directory..."
    
    # Ensure npm global directory exists
    mkdir -p /home/claude/.npm-global
    export NPM_CONFIG_PREFIX=/home/claude/.npm-global
    
    # Install tools
    npm install -g @anthropic-ai/claude-code
    npm install -g @google/gemini-cli@nightly
    npm install -g playwright
    npm install -g happy-coder
    
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

# Set up GitHub App authentication
if [ "$RUNNING_AS_ROOT" = "true" ]; then
    runuser -u claude -- /usr/local/bin/setup-git-auth.sh
else
    /usr/local/bin/setup-git-auth.sh
fi

(flock -x 200

# Handle migration to worktree structure
if [ -d "/workspace/.git" ] && [ -z $(ls "/workspace/main") ]; then
    echo "Migrating to worktree-enabled structure..."

    # Create main directory
    mkdir -p /workspace/main

    # Move all files to main (excluding the main directory itself)
    find /workspace -maxdepth 1 -mindepth 1 ! -name 'main' \
         -exec mv {} /workspace/main/ \;

    echo "Migration complete - repository now at /workspace/main"
fi

# Change to worktree directory
WORKTREE_DIR="/workspace/${WORKTREE_NAME:-main}"
mkdir -p "$WORKTREE_DIR"
cd "$WORKTREE_DIR"
echo "Moving to ${WORKTREE_DIR}"

# Clone repository if CLAUDE_PROJECT_REPO is set and .git doesn't exist
if [ -n "$CLAUDE_PROJECT_REPO" ] && [ ! -d "${WORKTREE_DIR}/.git" ]; then
    echo "🔍 Git clone debug:"
    echo "   CLAUDE_PROJECT_REPO: '$CLAUDE_PROJECT_REPO'"
    echo "   CLAUDE_PROJECT_BRANCH: '${CLAUDE_PROJECT_BRANCH:-main}'"
    echo "   Target branch: '${CLAUDE_PROJECT_BRANCH:-main}'"
    echo
    
    echo "Cloning repository from $CLAUDE_PROJECT_REPO (branch: ${CLAUDE_PROJECT_BRANCH:-main})..."
    
    # Use GitHub token if available
    if [ -n "$GITHUB_TOKEN" ]; then
        # Extract repo path from URL
        REPO_PATH=$(echo "$CLAUDE_PROJECT_REPO" | sed -E 's|https://github.com/||; s|\.git$||')
        AUTHED_URL="https://${GITHUB_TOKEN}@github.com/${REPO_PATH}.git"
        echo "   Running: git clone --branch '${CLAUDE_PROJECT_BRANCH:-main}' [AUTHED_URL] ."
        git clone --branch "${CLAUDE_PROJECT_BRANCH:-main}" "$AUTHED_URL" .
    else
        echo "   Running: git clone --branch '${CLAUDE_PROJECT_BRANCH:-main}' '$CLAUDE_PROJECT_REPO' ."
        git clone --branch "${CLAUDE_PROJECT_BRANCH:-main}" "$CLAUDE_PROJECT_REPO" .
    fi
    
    # Verify clone was successful and show actual branch
    if [ -d ".git" ]; then
        ACTUAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
        echo "   ✅ Repository cloned successfully"
        echo "   📋 Requested branch: '${CLAUDE_PROJECT_BRANCH:-main}'"
        echo "   📋 Actual branch: '$ACTUAL_BRANCH'"
        
        # Check if we got the right branch
        if [ "$ACTUAL_BRANCH" != "${CLAUDE_PROJECT_BRANCH:-main}" ]; then
            echo "   ⚠️  WARNING: Cloned branch '$ACTUAL_BRANCH' differs from requested '${CLAUDE_PROJECT_BRANCH:-main}'"
        fi
    else
        echo "   ❌ Repository clone failed"
        if [ "$ONESHOT_MODE" = "true" ]; then
            echo "   ONESHOT MODE: Cannot continue without repository"
            exit 1
        fi
    fi
    
    # Ensure claude owns the worktree if running as root
    if [ "$RUNNING_AS_ROOT" = "true" ]; then
        chown -R claude:claude "$WORKTREE_DIR"
    fi
elif [ -d ".git" ]; then
    echo "Workspace already exists, updating dependencies..."
elif [ "$ONESHOT_MODE" = "true" ]; then
    echo "❌ ONESHOT MODE: No repository available and CLAUDE_PROJECT_REPO not set"
    echo "   Cannot run oneshot mode without a git repository"
    echo "   Please ensure the source directory contains a git repository"
    exit 1
fi

) 200>/workspace/setup.lock

rm /workspace/setup.lock

# Check if we're in a Python project
if [ -f "pyproject.toml" ]; then
    echo "Python project detected. Setting up virtual environment..."
    
    # Create virtual environment if it doesn't exist
    if [ ! -d ".venv" ] || [ ! -f ".venv/bin/python" ]; then
        echo "Creating fresh virtual environment..."
        rm -rf .venv
        uv venv .venv
    else
        echo "Virtual environment already exists, checking if it's valid..."
        # Verify the venv works
        if .venv/bin/python --version >/dev/null 2>&1; then
            echo "Existing virtual environment is valid"
        else
            echo "Existing virtual environment is broken, recreating..."
            rm -rf .venv
            uv venv .venv
        fi
    fi
    
    # Activate the virtual environment
    source .venv/bin/activate
    
    # Install dependencies using uv sync to respect lock file
    echo "Installing Python dependencies..."
    uv sync --extra dev && touch .venv/.ready

    uv pip install poethepoet pytest-xdist pre-commit

    # Symlink basedpyright-langserver as pyright-langserver for the Claude Code pyright-lsp plugin
    if [ -f ".venv/bin/basedpyright-langserver" ] && [ ! -f ".venv/bin/pyright-langserver" ]; then
        ln -s basedpyright-langserver .venv/bin/pyright-langserver
    fi
    
    # Install pre-commit hooks if available (skip if running as root or in CI)
    if [ -f ".pre-commit-config.yaml" ] && [ "$RUNNING_AS_ROOT" != "true" ] && [ "$IS_CI_CONTAINER" != "true" ]; then
        echo "Installing pre-commit hooks..."
        .venv/bin/pre-commit install || true
    fi
fi

# Skip Node dependencies in CI (not needed for tests)
if [ "$IS_CI_CONTAINER" != "true" ]; then
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
fi

# Install Playwright browsers if needed
# In CI, they should already be pre-installed, but ensure they're available
if [ -f "pyproject.toml" ] && grep -q "playwright" pyproject.toml; then
    # Ensure playwright cache directory has correct permissions
    # The volume mount may have created it with wrong ownership
    # Note: This block requires running as root to work.
    # Current docker-compose configuration runs as user 1001.
    PLAYWRIGHT_CACHE="${PLAYWRIGHT_BROWSERS_PATH:-/home/claude/.cache/playwright-browsers}"
    if [ "$RUNNING_AS_ROOT" = "true" ]; then
        mkdir -p "$PLAYWRIGHT_CACHE"
        chown -R claude:claude "$(dirname "$PLAYWRIGHT_CACHE")"
    fi

    if [ "$IS_CI_CONTAINER" = "true" ]; then
        echo "Verifying Playwright browsers are installed..."
        # Just verify they exist, don't re-download (using rebrowser-playwright)
        .venv/bin/python -m rebrowser_playwright install chromium --dry-run || .venv/bin/python -m rebrowser_playwright install chromium || true
    else
        echo "Installing Playwright browsers for Python environment..."
        if [ "$RUNNING_AS_ROOT" = "true" ]; then
            runuser -u claude -- .venv/bin/python -m rebrowser_playwright install chromium || echo "Failed to install browsers"
        else
            .venv/bin/python -m rebrowser_playwright install chromium || echo "Failed to install browsers"
        fi
    fi
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

# One Shot Mode Configuration
if [ "$ONESHOT_MODE" = "true" ]; then
    echo "🎯 ONE SHOT MODE ACTIVE"
    echo "🔍 Debug: Container environment:"
    echo "   HOME=$HOME"
    echo "   CLAUDE_HOME_DIR=$CLAUDE_HOME_DIR"
    echo "   WORKSPACE_DIR=$WORKSPACE_DIR"
    echo "   PWD=$(pwd)"
    echo "   User: $(whoami) ($(id))"
    
    # Merge oneshot settings with existing settings
    mkdir -p .claude
    
    # Determine base settings file
    if [ -f "/home/claude/.claude/settings.local.json" ]; then
        BASE_SETTINGS="/home/claude/.claude/settings.local.json"
    elif [ -f "/opt/claude-settings/settings.local.json" ]; then
        BASE_SETTINGS="/opt/claude-settings/settings.local.json"
    else
        echo '{}' > /tmp/empty_settings.json
        BASE_SETTINGS="/tmp/empty_settings.json"
    fi
    
    # Merge settings using jsonmerge Python library for proper array concatenation
    if [ -f "/opt/oneshot-config/settings-oneshot.json" ]; then
        # Use dedicated merge script for clean, maintainable JSON merging
        if /venv/bin/python /usr/local/bin/merge-settings.py "$BASE_SETTINGS" "/opt/oneshot-config/settings-oneshot.json" > .claude/settings.local.json; then
            echo "   Settings merged using jsonmerge - permissions arrays concatenated"
        else
            echo "   ❌ Settings merge failed, falling back to base settings only"
            cp "$BASE_SETTINGS" .claude/settings.local.json
        fi
    else
        cp "$BASE_SETTINGS" .claude/settings.local.json
        echo "   No oneshot settings found, using base settings only"
    fi
    
    # Add oneshot instructions to CLAUDE.local.md
    if [ -f "/opt/oneshot-config/CLAUDE.oneshot.md" ]; then
        echo "" >> CLAUDE.local.md
        cat "/opt/oneshot-config/CLAUDE.oneshot.md" >> CLAUDE.local.md
    fi
    
    echo "One shot mode configuration complete"
    echo "  • Settings merged with oneshot permissions"
    echo "  • Instructions added to CLAUDE.local.md"
    echo "  • Strict stop hook installed"
    echo ""
fi

# Configure MCP servers for Claude
echo "Configuring MCP servers..."
cd "$WORKTREE_DIR"

# Find full paths for executables
DENO_PATH=$(which deno 2>/dev/null || echo "/home/claude/.deno/bin/deno")
UVX_PATH=$(which uvx 2>/dev/null || echo "uvx")
NPX_PATH=$(which npx 2>/dev/null || echo "npx")

# Configure MCP servers with full paths (bypass wrapper to avoid git pull)
CLAUDE_BIN="/home/claude/.npm-global/bin/claude"
# Remove existing servers if any exist
# Filter out status messages like "Checking MCP server health..." by only matching lines
# that look like server entries (start with alphanumeric, no spaces before colon)
MCP_SERVERS=$($CLAUDE_BIN mcp list 2>/dev/null | grep -E '^[a-zA-Z0-9_-]+:' | cut -d: -f1 || true)
if [ -n "$MCP_SERVERS" ]; then
    for server in $MCP_SERVERS; do
        $CLAUDE_BIN mcp remove --scope user "$server" || true
    done
fi
$CLAUDE_BIN mcp add --scope user context7 $(which npx) -- -y -q @upstash/context7-mcp
$CLAUDE_BIN mcp add --scope user scraper /workspace-bin/scrape_mcp
$CLAUDE_BIN mcp add --scope user serena -- sh -c "$(which uvx) -q --from git+https://github.com/oraios/serena serena-mcp-server --context ide-assistant --project $WORKTREE_DIR"
$CLAUDE_BIN mcp add --scope user playwright $(which npx) -- -y -q @playwright/mcp@latest --no-sandbox --allowed-origins "localhost:8000;localhost:5173;localhost:8001;unpkg.com;cdn.jsdelivr.net;cdnjs.cloudflare.com;cdn.simplecss.org;devcontainer-backend-1" --headless --isolated --browser chromium
# $CLAUDE_BIN mcp add --scope user github -t http https://api.githubcopilot.com/mcp/ -H "Authorization: Bearer $GITHUB_TOKEN"

# Configure Claude plugin marketplace from public GitHub repo
echo "Setting up Claude plugin marketplace..."
# Remove old marketplaces to avoid duplicates
$CLAUDE_BIN plugin marketplace remove claude-code-plugins 2>/dev/null || true
$CLAUDE_BIN plugin marketplace remove werdnum-plugins 2>/dev/null || true
# Add the marketplace directly from GitHub (repo is now public)
$CLAUDE_BIN plugin marketplace add werdnum/claude-code-plugins
echo "Claude plugin marketplace configured from GitHub"

# Ensure proper ownership if running as root
if [ "$RUNNING_AS_ROOT" = "true" ]; then
    # Ensure claude owns the entire workspace
    chown -R claude:claude /workspace
fi

echo "Workspace setup complete!"

happy doctor clean

# Execute the command passed to the container
exec "$@"


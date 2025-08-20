#!/bin/bash
# Run container in oneshot mode with isolated workspace

set -e

# Check if task/prompt provided
if [ $# -eq 0 ]; then
    echo "❌ Error: No task provided for oneshot mode" >&2
    echo "" >&2
    echo "Usage:" >&2
    echo "  $0 \"<task description>\"" >&2
    echo "  $0 \"<task>\" [workspace-id]" >&2
    echo "" >&2
    echo "Examples:" >&2
    echo "  $0 \"Fix the failing tests in test_api.py\"" >&2
    echo "  $0 \"Add user authentication to the web interface\" auth-feature" >&2
    echo "  $0 \"Investigate the memory leak in the processing service\"" >&2
    exit 1
fi

# First argument is the task
TASK="$1"

# Second argument is optional workspace ID
WORKSPACE_ID=${2:-$(date +%Y%m%d_%H%M%S)_$$}

# Load environment variables from .env file if it exists
if [ -f ".env" ]; then
    echo "Loading environment from .env file..."
    # Safely load .env file without command injection risk
    set -a
    source .env
    set +a
fi

# Create isolated workspace directory on host
# Allow customization of oneshot workspaces directory via env var
ONESHOT_WORKSPACES_DIR="${ONESHOT_WORKSPACES_DIR:-./workspace-oneshot}"
WORKSPACE_HOST_DIR="${ONESHOT_WORKSPACES_DIR}/${WORKSPACE_ID}"
mkdir -p "$WORKSPACE_HOST_DIR"

# Set environment for oneshot mode
export ONESHOT_MODE=true
export ONESHOT_STRICT_EXIT=true
export ONESHOT_TASK="$TASK"
# For oneshot mode, always use isolated workspace regardless of .env
export WORKSPACE_DIR="$WORKSPACE_HOST_DIR"
# CLAUDE_HOME_DIR should already be set from .env if it exists, don't override it
if [ -z "$CLAUDE_HOME_DIR" ]; then
    export CLAUDE_HOME_DIR="./claude-home"
fi

# Pass through other environment variables that might be needed
export CLAUDE_PROJECT_REPO="${CLAUDE_PROJECT_REPO:-}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-}"
export GEMINI_API_KEY="${GEMINI_API_KEY:-}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"

echo "🎯 Starting One Shot Mode"
echo "   Task: $TASK"
echo "   Workspace ID: $WORKSPACE_ID"
echo "   Host workspace: $WORKSPACE_HOST_DIR"
echo "   Shared home: $CLAUDE_HOME_DIR"
echo ""
echo "🔍 Debug: Environment variables being passed to container:"
echo "   ONESHOT_MODE=$ONESHOT_MODE"
echo "   ONESHOT_WORKSPACES_DIR=$ONESHOT_WORKSPACES_DIR"
echo "   WORKSPACE_DIR=$WORKSPACE_DIR"
echo "   CLAUDE_HOME_DIR=$CLAUDE_HOME_DIR"
echo ""
echo "One shot mode features:"
echo "  • Isolated workspace (changes won't affect other instances)"
echo "  • Strict exit controls (must complete work before stopping)"
echo "  • Auto-approved git/GitHub tools for autonomous operation"
echo "  • Shared Claude home directory for settings and cache"
echo "  • Non-interactive autonomous execution"
echo ""

# Check if docker compose (v2) or docker-compose (v1) is available
DOCKER_COMPOSE_CMD=""
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DOCKER_COMPOSE_CMD="docker-compose"
else
    echo "❌ Error: Neither 'docker compose' nor 'docker-compose' found" >&2
    echo "Please install Docker Compose to use oneshot mode" >&2
    exit 1
fi

# Check if the docker-compose file exists
if [ ! -f ".devcontainer/docker-compose.yml" ]; then
    echo "❌ Error: .devcontainer/docker-compose.yml not found" >&2
    echo "Please run this script from the project root directory" >&2
    exit 1
fi

# Run docker-compose with oneshot environment and pass the task
echo "Starting claude container in oneshot mode..."
echo "Claude will work on: $TASK"
echo ""

# Run claude with the task as argument - this makes it non-interactive
$DOCKER_COMPOSE_CMD -f .devcontainer/docker-compose.yml run --rm claude claude "$TASK"

echo ""
echo "🎯 One shot session ended"
echo "   Workspace preserved at: $WORKSPACE_HOST_DIR"
echo ""
echo "To clean up this workspace later:"
echo "   rm -rf $WORKSPACE_HOST_DIR"
echo ""
echo "To view all oneshot workspaces:"
echo "   ls -la $ONESHOT_WORKSPACES_DIR/"


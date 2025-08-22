#!/bin/bash
# Run container in oneshot mode with isolated workspace

set -e

# Parse command line arguments
BRANCH="main"
TASK=""
WORKSPACE_ID=""
NONINTERACTIVE=false
PLAN_MODE=false
MODEL=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --branch|-b)
            BRANCH="$2"
            shift 2
            ;;
        --workspace-id)
            WORKSPACE_ID="$2"
            shift 2
            ;;
        --noninteractive|-p)
            NONINTERACTIVE=true
            shift
            ;;
        --plan)
            PLAN_MODE=true
            shift
            ;;
        --model|-m)
            MODEL="$2"
            shift 2
            ;;
        *)
            if [ -z "$TASK" ]; then
                TASK="$1"
            else
                # Second positional argument is workspace ID (for backward compatibility)
                WORKSPACE_ID="$1"
            fi
            shift
            ;;
    esac
done

# Check if task/prompt provided
if [ -z "$TASK" ]; then
    echo "❌ Error: No task provided for oneshot mode" >&2
    echo "" >&2
    echo "Usage:" >&2
    echo "  $0 [OPTIONS] \"<task description>\" [workspace-id]" >&2
    echo "" >&2
    echo "Options:" >&2
    echo "  --branch, -b <branch>        Clone specific git branch (default: main)" >&2
    echo "  --workspace-id <id>          Use specific workspace ID" >&2
    echo "  --noninteractive, -p         Use claude -p for truly non-interactive mode" >&2
    echo "  --plan                       Start in planning mode (create plan before implementation)" >&2
    echo "  --model, -m <model>          Claude model (default: opusplan)" >&2
    echo "" >&2
    echo "Examples:" >&2
    echo "  $0 \"Fix the failing tests in test_api.py\"" >&2
    echo "  $0 --plan \"Add user authentication\"" >&2
    echo "  $0 --model sonnet \"Investigate memory leak\"" >&2
    echo "  $0 --plan --model sonnet \"Refactor API endpoints\"" >&2
    echo "  $0 --branch feature/new-api \"Debug performance issue\"" >&2
    exit 1
fi

# Set default workspace ID if not provided
if [ -z "$WORKSPACE_ID" ]; then
    WORKSPACE_ID=$(date +%Y%m%d_%H%M%S)_$$
fi

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
# For oneshot mode, we want to clone the current repo into the isolated workspace
DETECTED_REPO_URL=$(git remote get-url origin 2>/dev/null || echo "")
export CLAUDE_PROJECT_REPO="${CLAUDE_PROJECT_REPO:-"$DETECTED_REPO_URL"}"
export CLAUDE_PROJECT_BRANCH="$BRANCH"
export GITHUB_TOKEN="${GITHUB_TOKEN:-}"
export GEMINI_API_KEY="${GEMINI_API_KEY:-}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"

echo "🎯 Starting One Shot Mode"
echo "   Task: $TASK"
echo "   Workspace ID: $WORKSPACE_ID"
echo "   Host workspace: $WORKSPACE_HOST_DIR"
echo "   Shared home: $CLAUDE_HOME_DIR"
echo "   Model: ${MODEL:-opusplan}"
if [ "$PLAN_MODE" = "true" ]; then
    echo "   Mode: Planning mode (create plan before implementation)"
elif [ "$NONINTERACTIVE" = "true" ]; then
    echo "   Mode: Non-interactive (claude -p)"
else
    echo "   Mode: Interactive with auto-approval"
fi
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
echo "  • Default opusplan model (opus for planning, sonnet for implementation)"
if [ "$PLAN_MODE" = "true" ]; then
    echo "  • Planning mode (create comprehensive plan before implementation)"
elif [ "$NONINTERACTIVE" = "true" ]; then
    echo "  • Truly non-interactive mode (claude -p, no prompts at all)"
else
    echo "  • Interactive mode with auto-approval"
fi
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
# Set default model to opusplan
MODEL="${MODEL:-opusplan}"

# Build the Claude command based on mode
if [ "$PLAN_MODE" = "true" ]; then
    # Planning mode
    $DOCKER_COMPOSE_CMD -f .devcontainer/docker-compose.yml run --rm claude claude --permission-mode plan --model "$MODEL" "$TASK"
elif [ "$NONINTERACTIVE" = "true" ]; then
    # Non-interactive mode
    $DOCKER_COMPOSE_CMD -f .devcontainer/docker-compose.yml run --rm claude claude -p --model "$MODEL" "$TASK"
else
    # Default: Interactive with auto-approval
    $DOCKER_COMPOSE_CMD -f .devcontainer/docker-compose.yml run --rm claude claude --permission-mode acceptEdits --model "$MODEL" "$TASK"
fi

echo ""
echo "🎯 One shot session ended"
echo "   Workspace preserved at: $WORKSPACE_HOST_DIR"
echo ""
echo "To clean up this workspace later:"
echo "   rm -rf $WORKSPACE_HOST_DIR"
echo ""
echo "To view all oneshot workspaces:"
echo "   ls -la $ONESHOT_WORKSPACES_DIR/"


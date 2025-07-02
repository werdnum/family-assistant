#!/bin/bash
# Run Claude Code in an isolated container with PostgreSQL support

set -e

echo "Starting Claude Code in isolated container..."

# Source .env to get GitHub token
if [ -f .env ]; then
    source .env
fi

# Check for Claude auth directory
if [ ! -d ~/.claude ]; then
    echo "Error: Claude auth directory ~/.claude not found"
    echo "Please run 'claude login' on your host system first"
    exit 1
fi

# Ensure network exists
podman network exists devcontainer_devnet || podman network create devcontainer_devnet

# Start a fresh postgres container
echo "Starting PostgreSQL service..."
# Stop any existing postgres container
podman stop devcontainer-postgres-claude 2>/dev/null || true
podman rm devcontainer-postgres-claude 2>/dev/null || true

# Start fresh postgres
podman run -d \
  --name devcontainer-postgres-claude \
  --network devcontainer_devnet \
  --network-alias postgres \
  -e POSTGRES_USER=test \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=test \
  pgvector/pgvector:0.8.0-pg17

# Wait for postgres to be healthy
echo "Waiting for PostgreSQL to be ready..."
for i in {1..30}; do
    if podman exec devcontainer-postgres-claude pg_isready -U test >/dev/null 2>&1; then
        echo "PostgreSQL is ready!"
        break
    fi
    echo -n "."
    sleep 1
done

# Build the image if needed
if ! podman images | grep -q family-assistant-devcontainer; then
    echo "Building container image..."
    podman build -t family-assistant-devcontainer:latest .devcontainer/
fi

# Run Claude container
echo "Starting Claude Code container..."
echo "Note: Changes must be committed and pushed to persist outside the container"
echo ""

# Create a cleanup function
cleanup() {
    echo -e "\n\nCleaning up..."
    podman stop devcontainer-postgres-claude 2>/dev/null || true
    podman rm devcontainer-postgres-claude 2>/dev/null || true
    podman volume rm -f family-assistant-claude-workspace 2>/dev/null || true
}

# Set trap to cleanup on exit
trap cleanup EXIT

# Run interactive Claude container
podman run -it --rm \
  --name family-assistant-claude \
  --network devcontainer_devnet \
  --env CLAUDE_PROJECT_REPO="${CLAUDE_PROJECT_REPO:-https://github.com/werdnum/family-assistant.git}" \
  --env GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
  --env TEST_DATABASE_URL="postgresql+asyncpg://test:test@postgres:5432/test" \
  --env DATABASE_URL="postgresql+asyncpg://test:test@postgres:5432/test" \
  --env PATH="/root/.local/bin:/root/.deno/bin:$PATH" \
  --env DEV_MODE=true \
  --volume family-assistant-claude-workspace:/workspace \
  --volume ~/.claude:/root/.claude:ro \
  --workdir /workspace \
  --entrypoint /bin/bash \
  localhost/family-assistant-devcontainer:latest \
  -c '
    # Setup workspace
    echo "Setting up workspace..."
    /usr/local/bin/setup-workspace.sh
    
    # Activate virtualenv
    source .venv/bin/activate
    
    # Create a simple CLAUDE.md if it doesn'\''t exist
    if [ ! -f CLAUDE.md ]; then
        cat > CLAUDE.md << EOF
# CLAUDE.md

You are running in an isolated container environment with:
- PostgreSQL available at postgres:5432
- All development tools pre-installed
- Fresh clone of the repository
- Isolated Python virtual environment

Important notes:
- Changes must be committed and pushed to persist
- Use "poe test" to run tests
- Use "poe lint" to run linters
- PostgreSQL is already running, no need for testcontainers
EOF
    fi
    
    echo ""
    echo "✅ Workspace ready! Starting Claude Code..."
    echo ""
    
    # Start Claude
    exec claude
  '
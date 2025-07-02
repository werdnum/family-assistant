#!/bin/bash
# Run tests in container with isolated workspace (no host mounts)

set -e

echo "Running tests in container with isolated workspace..."

# Source .env to get GitHub token
if [ -f .env ]; then
    source .env
fi

# Ensure network exists
podman network exists devcontainer_devnet || podman network create devcontainer_devnet

# Start a fresh postgres container for testing
echo "Starting PostgreSQL service..."
# Stop any existing test postgres container
podman stop devcontainer-postgres-test 2>/dev/null || true
podman rm devcontainer-postgres-test 2>/dev/null || true

# Start fresh postgres
podman run -d \
  --name devcontainer-postgres-test \
  --network devcontainer_devnet \
  --network-alias postgres \
  -e POSTGRES_USER=test \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=test \
  pgvector/pgvector:0.8.0-pg17

# Wait for postgres to be healthy
echo "Waiting for PostgreSQL to be ready..."
for i in {1..30}; do
    if podman exec devcontainer-postgres-test pg_isready -U test >/dev/null 2>&1; then
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

# Run container with isolated workspace
echo "Starting test container..."
podman run --rm \
  --name family-assistant-test \
  --network devcontainer_devnet \
  --env CLAUDE_PROJECT_REPO="https://github.com/werdnum/family-assistant.git" \
  --env GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
  --env TEST_DATABASE_URL="postgresql+asyncpg://test:test@postgres:5432/test" \
  --env DATABASE_URL="postgresql+asyncpg://test:test@postgres:5432/test" \
  --env PATH="/root/.local/bin:/root/.deno/bin:$PATH" \
  --env DEV_MODE=true \
  --volume family-assistant-test-workspace:/workspace \
  --workdir /workspace \
  --entrypoint /bin/bash \
  localhost/family-assistant-devcontainer:latest \
  -c '
    # Setup workspace
    echo "Setting up workspace..."
    /usr/local/bin/setup-workspace.sh
    
    # Activate virtualenv and run tests
    echo "Running tests..."
    source .venv/bin/activate
    
    # Install test dependencies if needed
    if ! python -c "import pytest_xdist" 2>/dev/null; then
        echo "Installing pytest-xdist..."
        uv pip install pytest-xdist
    fi
    
    # Run integration tests
    echo "Running integration tests with PostgreSQL..."
    timeout 300 pytest tests/integration/ --db=postgres -x --tb=short -q
  '

# Cleanup
echo "Cleaning up..."
podman stop devcontainer-postgres-test 2>/dev/null || true
podman rm devcontainer-postgres-test 2>/dev/null || true
podman volume rm -f family-assistant-test-workspace || true

echo "Test complete!"
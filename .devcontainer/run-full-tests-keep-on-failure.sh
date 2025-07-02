#!/bin/bash
# Run full test suite (poe test) in container and keep it running on failure for inspection

set -e

echo "Running full test suite in isolated container (keeping on failure)..."

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

# Remove any existing test container
podman stop family-assistant-full-test 2>/dev/null || true
podman rm family-assistant-full-test 2>/dev/null || true

# Run container with isolated workspace (detached to keep it running)
echo "Starting test container..."
echo "This will run linting and all tests (may take 10-15 minutes)..."
podman run -d \
  --name family-assistant-full-test \
  --network devcontainer_devnet \
  --env CLAUDE_PROJECT_REPO="https://github.com/werdnum/family-assistant.git" \
  --env GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
  --env TEST_DATABASE_URL="postgresql+asyncpg://test:test@postgres:5432/test" \
  --env DATABASE_URL="postgresql+asyncpg://test:test@postgres:5432/test" \
  --env PATH="/root/.local/bin:/root/.deno/bin:$PATH" \
  --env DEV_MODE=true \
  --volume family-assistant-test-workspace:/workspace \
  --workdir /workspace \
  localhost/family-assistant-devcontainer:latest \
  sleep infinity

# Wait for container to be ready
echo "Waiting for container to start..."
sleep 5

# Run the setup and tests
echo "Setting up workspace and running tests..."
podman exec family-assistant-full-test bash -c '
    # Setup workspace
    echo "Setting up workspace..."
    /usr/local/bin/setup-workspace.sh
    
    # Activate virtualenv and run full test suite
    echo "Running full test suite..."
    source .venv/bin/activate
    
    # Run poe test with a long timeout
    echo "Starting poe test (linting + tests)..."
    timeout 900 poe test || exit_code=$?
    
    # Save exit code to file for later retrieval
    echo ${exit_code:-0} > /workspace/test-exit-code
    
    # Exit with the same code
    exit ${exit_code:-0}
' || true

# Get the exit code
EXIT_CODE=$(podman exec family-assistant-full-test cat /workspace/test-exit-code 2>/dev/null || echo "1")

# Report results
if [ "$EXIT_CODE" -eq 0 ]; then
    echo "✅ All tests passed!"
    echo ""
    echo "Cleaning up..."
    podman stop family-assistant-full-test
    podman rm family-assistant-full-test
    podman stop devcontainer-postgres-test
    podman rm devcontainer-postgres-test
    podman volume rm -f family-assistant-test-workspace
else
    echo "❌ Tests failed with exit code: $EXIT_CODE"
    echo ""
    echo "Container is still running for inspection. To investigate:"
    echo "  podman exec -it family-assistant-full-test bash"
    echo ""
    echo "To view test results:"
    echo "  podman exec family-assistant-full-test jq '.summary' /workspace/.report.json"
    echo "  podman exec family-assistant-full-test jq '.tests | map(select(.outcome == \"failed\"))' /workspace/.report.json"
    echo ""
    echo "To cleanup when done:"
    echo "  podman stop family-assistant-full-test devcontainer-postgres-test"
    echo "  podman rm family-assistant-full-test devcontainer-postgres-test"
    echo "  podman volume rm family-assistant-test-workspace"
fi

exit $EXIT_CODE
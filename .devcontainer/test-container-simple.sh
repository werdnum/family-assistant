#!/bin/bash
# Simple test to run tests in container with mounted code

echo "Running tests in container with mounted code..."

# Source .env to get GitHub token
source .env

# Run tests directly with mounted code
podman run --rm \
  --network devcontainer_devnet \
  --env TEST_DATABASE_URL=postgresql+asyncpg://test:test@postgres:5432/test \
  --env PATH="/root/.local/bin:/root/.deno/bin:$PATH" \
  --volume "${PWD}:/workspace:z" \
  --workdir /workspace \
  --entrypoint /bin/bash \
  localhost/family-assistant-devcontainer:latest \
  -c "rm -rf .venv && uv venv .venv && source .venv/bin/activate && uv sync --dev && uv pip install pytest-xdist && echo 'Running some integration tests with postgres...' && timeout 300 pytest tests/integration/ --db=postgres -x --tb=short -q"

echo "Test complete!"

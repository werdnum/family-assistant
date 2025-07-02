#!/bin/bash
# Test script to run tests inside container with local code

echo "Testing inside container..."

# Build a test image with current code
echo "Building test image with current code..."
cat > .devcontainer/Dockerfile.test << 'EOF'
FROM localhost/family-assistant-devcontainer:latest

# Copy current code into container
COPY . /workspace

WORKDIR /workspace

# Skip the setup script since we're copying code directly
ENTRYPOINT ["/bin/bash"]
EOF

# Build the test image
podman build -f .devcontainer/Dockerfile.test -t family-assistant-test .

# Run tests using the external postgres
echo "Running tests in container..."
podman run --rm \
  --network container:devcontainer_postgres_1 \
  --env TEST_DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test \
  --env PATH="/root/.local/bin:/root/.deno/bin:$PATH" \
  family-assistant-test \
  -c "uv pip install -e '.[dev]' && poe test"

# Cleanup
rm -f .devcontainer/Dockerfile.test

echo "Test complete!"
#!/bin/bash
# Test script for basic devcontainer functionality

echo "Testing basic devcontainer with podman..."

# Start PostgreSQL
echo "Starting PostgreSQL container..."
podman run -d \
  --name test-postgres \
  --env POSTGRES_USER=test \
  --env POSTGRES_PASSWORD=test \
  --env POSTGRES_DB=test \
  --health-cmd="pg_isready -U test" \
  --health-interval=5s \
  --health-timeout=5s \
  --health-retries=5 \
  pgvector/pgvector:0.8.0-pg17

# Wait for PostgreSQL to be healthy
echo "Waiting for PostgreSQL to be healthy..."
for i in {1..30}; do
  if podman healthcheck run test-postgres 2>/dev/null; then
    echo "PostgreSQL is healthy!"
    break
  fi
  echo -n "."
  sleep 1
done

# Test Claude container
echo "Testing Claude container..."
podman run --rm -it \
  --name test-claude \
  --env TEST_DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test \
  --volume "${PWD}:/workspace:z" \
  localhost/family-assistant-devcontainer:latest \
  bash -c "echo 'Container started successfully!' && claude --version"

# Cleanup
echo "Cleaning up..."
podman stop test-postgres
podman rm test-postgres

echo "Test complete!"
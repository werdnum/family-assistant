#!/bin/bash
# Run development container with isolated workspace (no host mounts)

set -e

echo "Starting development container with isolated workspace..."

# Source .env to get GitHub token and other env vars
if [ -f .env ]; then
    source .env
fi

# Use docker-compose to start all services
echo "Starting services..."
podman-compose -f .devcontainer/docker-compose.yml up -d

# Wait for claude container to be ready
echo "Waiting for development container to be ready..."
for i in {1..30}; do
    if podman exec devcontainer_claude_1 test -f /workspace/.venv/bin/python 2>/dev/null; then
        echo "Development container is ready!"
        break
    fi
    echo -n "."
    sleep 1
done

# Show container status
echo ""
echo "Container status:"
podman-compose -f .devcontainer/docker-compose.yml ps

echo ""
echo "Development environment is ready!"
echo ""
echo "To connect to the container:"
echo "  podman exec -it devcontainer_claude_1 bash"
echo ""
echo "To run tests inside the container:"
echo "  podman exec -it devcontainer_claude_1 bash -c 'source .venv/bin/activate && poe test'"
echo ""
echo "To run specific tests:"
echo "  podman exec -it devcontainer_claude_1 bash -c 'source .venv/bin/activate && pytest tests/integration/ --db=postgres'"
echo ""
echo "To stop all services:"
echo "  podman-compose -f .devcontainer/docker-compose.yml down"
echo ""
echo "To view logs:"
echo "  podman-compose -f .devcontainer/docker-compose.yml logs -f"
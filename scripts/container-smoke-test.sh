#!/bin/bash
set -e

# Container Smoke Test Script
# Verifies that a Docker image starts completely, returns healthy, and that the MCP servers it
# ships actually launch inside it.

IMAGE_NAME=${1:-"family-assistant:smoke-test"}
PORT=${2:-8000}
HEALTH_PATH=${3:-"/health"}
CONTAINER_NAME="smoke-test-$(date +%s)"
MAX_RETRIES=150
SLEEP_INTERVAL=2

# MCP servers whose entry points the image installs itself, so they must start without reaching the
# network. Servers fetched at runtime (the Deno ones) are deliberately left out. A failed MCP server
# does not make the app unhealthy — it just silently loses its tools — so this is checked directly.
MCP_SERVERS=${MCP_SERVERS:-"time"}

echo "Starting smoke test for image: $IMAGE_NAME"
echo "Health check endpoint: http://localhost:$PORT$HEALTH_PATH"

# Ensure cleanup on exit
cleanup() {
    echo "--- Smoke Test Cleanup ---"

    # Check if container exists
    if docker ps -a --format '{{.Names}}' | grep -q "^$CONTAINER_NAME$"; then
        echo "Container logs for $CONTAINER_NAME:"
        docker logs "$CONTAINER_NAME" || true

        echo "Container state:"
        docker inspect "$CONTAINER_NAME" --format '{{.State.Status}}' || true

        echo "Removing container: $CONTAINER_NAME"
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    else
        echo "Container $CONTAINER_NAME was not found, no cleanup needed."
    fi
}
trap cleanup EXIT

# Run the container
# Use dummy API keys to avoid startup failures from MCP servers that validate keys
echo "Launching container..."
docker run -d --name "$CONTAINER_NAME" -p "$PORT:$PORT" \
    -e GEMINI_API_KEY=dummy \
    -e OPENAI_API_KEY=dummy \
    -e ANTHROPIC_API_KEY=dummy \
    -e BRAVE_API_KEY=dummy \
    -e HOMEASSISTANT_API_KEY=dummy \
    -e GOOGLE_MAPS_API_KEY=dummy \
    "$IMAGE_NAME"

# Wait for health check
RETRY_COUNT=0
HEALTH="never_checked"
RESPONSE=""
until [ $RETRY_COUNT -ge $MAX_RETRIES ]
do
    # Check if container is still running
    STATE=$(docker inspect "$CONTAINER_NAME" --format '{{.State.Status}}' 2>/dev/null || echo "not_found")
    if [ "$STATE" != "running" ]; then
        echo "ERROR: Container is not running! State: $STATE"
        # The trap will print logs on exit
        exit 1
    fi

    # Fetch status, handle potential curl failures during startup
    RESPONSE=$(curl -s "http://localhost:$PORT$HEALTH_PATH" || echo '{"status":"failed_to_connect"}')
    HEALTH=$(echo "$RESPONSE" | jq -r .status 2>/dev/null || echo "unknown")

    if [ "$HEALTH" = "healthy" ] || [ "$HEALTH" = "ok" ]; then
        echo "Container is $HEALTH!"
        break
    fi

    echo "Waiting for container to be healthy... ($RETRY_COUNT/$MAX_RETRIES) Status: $HEALTH"
    RETRY_COUNT=$((RETRY_COUNT+1))
    sleep "$SLEEP_INTERVAL"
done

if [ "$HEALTH" != "healthy" ] && [ "$HEALTH" != "ok" ]; then
    echo "Container failed to become healthy within $MAX_RETRIES retries."
    echo "Final response: $RESPONSE"
    exit 1
fi

# Verify the MCP servers the image installs actually start. This runs inside the container so it
# exercises the entry points as installed there, and goes through the same MCPToolsProvider the
# application uses -- including the MCP SDK's environment stripping, which is what stops a `uvx`
# style command from finding the pre-installed tool environment.
echo "--- Verifying MCP servers: $MCP_SERVERS ---"
# shellcheck disable=SC2086 # MCP_SERVERS is a deliberately word-split list of server ids.
if ! docker exec "$CONTAINER_NAME" python scripts/check_mcp_servers.py $MCP_SERVERS; then
    echo "ERROR: MCP server verification failed. Smoke test FAILED."
    exit 1
fi

echo "Smoke test PASSED."
exit 0

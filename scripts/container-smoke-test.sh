#!/bin/bash
set -e

# Container Smoke Test Script
# Verifies that a Docker image starts completely and returns healthy.

IMAGE_NAME=${1:-"family-assistant:smoke-test"}
PORT=${2:-8000}
HEALTH_PATH=${3:-"/health"}
CONTAINER_NAME="smoke-test-$(date +%s)"
MAX_RETRIES=30
SLEEP_INTERVAL=2

echo "Starting smoke test for image: $IMAGE_NAME"
echo "Health check endpoint: http://localhost:$PORT$HEALTH_PATH"

# Ensure cleanup on exit
cleanup() {
    echo "Cleaning up container: $CONTAINER_NAME"
    docker logs "$CONTAINER_NAME" || true
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Run the container
# Use GEMINI_API_KEY=dummy to avoid startup failures if it validates keys
docker run -d --name "$CONTAINER_NAME" -p "$PORT:$PORT" -e GEMINI_API_KEY=dummy "$IMAGE_NAME"

# Wait for health check
RETRY_COUNT=0
until [ $RETRY_COUNT -ge $MAX_RETRIES ]
do
    # Fetch status, handle potential curl failures during startup
    # Use -s for silent, -f for fail on 4xx/5xx
    RESPONSE=$(curl -s "http://localhost:$PORT$HEALTH_PATH" || echo '{"status":"failed_to_connect"}')
    HEALTH=$(echo "$RESPONSE" | jq -r .status 2>/dev/null || echo "unknown")

    if [ "$HEALTH" = "healthy" ] || [ "$HEALTH" = "ok" ]; then
        echo "Container is $HEALTH! Smoke test PASSED."
        exit 0
    fi

    echo "Waiting for container to be healthy... ($RETRY_COUNT/$MAX_RETRIES) Status: $HEALTH"
    RETRY_COUNT=$((RETRY_COUNT+1))
    sleep "$SLEEP_INTERVAL"
done

echo "Container failed to become healthy within $MAX_RETRIES retries."
echo "Final response: $RESPONSE"
exit 1

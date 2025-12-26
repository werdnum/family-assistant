#!/bin/bash
# Wait for the backend server to be ready by polling the health endpoint

set -e

TIMEOUT="${1:-120}"
HEALTH_URL="http://devcontainer-backend-1:8000/api/health"
INTERVAL=2
ELAPSED=0

echo "Waiting for server at ${HEALTH_URL}..."
echo "Timeout: ${TIMEOUT}s"

while [ $ELAPSED -lt $TIMEOUT ]; do
    if curl -sf "${HEALTH_URL}" > /dev/null 2>&1; then
        echo "Server is ready! (${ELAPSED}s elapsed)"
        exit 0
    fi

    echo "Server not ready yet... (${ELAPSED}s elapsed)"
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
done

echo "Timeout waiting for server after ${TIMEOUT}s"
exit 1

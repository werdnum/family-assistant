#!/bin/bash
# Wait for backend to be ready

MAX_WAIT=300  # 5 minutes
WAIT_INTERVAL=2
elapsed=0

echo "Waiting for backend to be ready..."

while [ $elapsed -lt $MAX_WAIT ]; do
    if curl -s http://devcontainer-backend-1:8000/health > /dev/null 2>&1; then
        echo "✓ Backend is ready!"
        exit 0
    fi

    echo -n "."
    sleep $WAIT_INTERVAL
    elapsed=$((elapsed + WAIT_INTERVAL))
done

echo ""
echo "❌ Backend did not become ready within ${MAX_WAIT} seconds"
exit 1


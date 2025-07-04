#!/bin/bash
set -e

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Configuration
REGISTRY="containers.andrewgarrett.dev"
IMAGE_NAME="family-assistant-devcontainer"
TAG="${1:-latest}"

# Change to the .devcontainer directory to ensure correct context
cd "${SCRIPT_DIR}"

# Safety check: ensure we're using the dev container Dockerfile
if [ ! -f "Dockerfile" ]; then
    echo "Error: Dockerfile not found in ${SCRIPT_DIR}"
    exit 1
fi

# Additional safety: check this is the dev container Dockerfile (should have claude-code installation)
if ! grep -q "claude-code" Dockerfile; then
    echo "Error: This doesn't appear to be the development container Dockerfile"
    echo "The development Dockerfile should contain 'claude-code' installation"
    exit 1
fi

echo "Building development container image with podman..."
echo "Using Dockerfile: ${SCRIPT_DIR}/Dockerfile"
echo "Build context: ${SCRIPT_DIR}"

# Explicitly use the development Dockerfile in .devcontainer
podman build -t "${REGISTRY}/${IMAGE_NAME}:${TAG}" -t "${REGISTRY}/${IMAGE_NAME}:latest" -f Dockerfile .

echo "Pushing image to registry..."
podman push "${REGISTRY}/${IMAGE_NAME}:${TAG}"
if [ "${TAG}" != "latest" ]; then
    podman push "${REGISTRY}/${IMAGE_NAME}:latest"
fi

echo "Image pushed successfully: ${REGISTRY}/${IMAGE_NAME}:${TAG}"
if [ "${TAG}" != "latest" ]; then
    echo "Also pushed as: ${REGISTRY}/${IMAGE_NAME}:latest"
fi
echo ""
echo "To use this image in Kubernetes, update the image tag in:"
echo "  ${SCRIPT_DIR}/k8s/kustomization.yaml"
echo "Or use: kubectl set image pod/family-assistant-dev *=${REGISTRY}/${IMAGE_NAME}:${TAG} -n family-assistant-dev"

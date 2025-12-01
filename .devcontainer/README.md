# Claude Code Development Container

This directory contains the Docker/Podman configuration for running Claude Code in isolated
development containers.

## Overview

The development container system provides:

- **Isolated Workspaces**: Each container gets its own fresh git clone in an isolated Docker volume
- **No Host Mounts**: Containers don't mount host directories, preventing conflicts
- **PostgreSQL Sidecar**: Dedicated PostgreSQL service replacing testcontainers for faster tests
- **Full Development Tooling**: All required tools pre-installed (uv, poe, playwright, ast-grep,
  etc.)
- **Private Repository Support**: Handles authentication via GitHub tokens

## Architecture

### Container Hierarchy

The development environment uses a three-layer Docker architecture for optimal caching and
separation of concerns:

```
┌──────────────────────────────────────────────────────┐
│ Dockerfile.base (~2-3GB, stable)                     │
│ - OS packages (Ubuntu, PostgreSQL, Node.js)          │
│ - Language runtimes (Python, uv, Deno)               │
│ - Build tools (ripgrep, fd, ast-grep, yq)            │
│ - Playwright + browsers (for CI tests)               │
│ - NO dev tools (claude-code, gemini-cli, etc.)       │
└──────────────────────────────────────────────────────┘
                       ▲
                       │
        ┌──────────────┴───────────────┐
        │                              │
┌───────────────────┐    ┌────────────────────────────┐
│ Dockerfile        │    │ Dockerfile.ci (~3-4GB)     │
│ (~5-6GB)          │    │                            │
│                   │    │ - Pre-built frontend       │
│ - Dev tools:      │    │ - Python test deps         │
│   * claude-code   │    │ - NO dev tools (smaller!)  │
│   * gemini-cli    │    │                            │
│   * llm           │    │ Used by: CI workflows      │
│   * claudecodeui  │    │                            │
│                   │    │                            │
│ Used by: devs     │    │                            │
└───────────────────┘    └────────────────────────────┘
```

**Benefits:**

- **Base layer caching**: OS/runtimes change rarely, cache hit rate >90%
- **Smaller CI images**: CI doesn't need dev tools, saves 2-3GB
- **Faster CI builds**: ~75% reduction in build time (45 min → 10-12 min)
- **Clear separation**: CI vs dev concerns architecturally distinct

### Service Architecture

```
┌─────────────────────────┐     ┌─────────────────────────┐
│   PostgreSQL Sidecar    │     │    Claude Container     │
│  pgvector/pgvector:pg17 │     │   Ubuntu 24.04 base     │
│                         │◄────┤                         │
│  - Test database        │     │  - Fresh git clone      │
│  - Vector support       │     │  - Isolated .venv       │
│  - Shared network       │     │  - All dev tools        │
└─────────────────────────┘     └─────────────────────────┘
```

## Quick Start

### Running Tests in Isolated Container

```bash
# Run integration tests only
./.devcontainer/run-tests-isolated.sh

# Run full test suite (linting + all tests)
./.devcontainer/run-full-tests-isolated.sh
```

This script:

1. Starts a fresh PostgreSQL container
2. Builds the development container image
3. Creates an isolated workspace volume
4. Clones the repository
5. Installs all dependencies
6. Runs the tests
7. Cleans up resources

### Interactive Development

```bash
# Start the development environment
./.devcontainer/run-dev-isolated.sh

# Connect to the container
podman exec -it devcontainer_claude_1 bash

# Inside the container, run tests
source .venv/bin/activate
poe test
```

### Using Docker Compose

```bash
# Start all services (using podman-compose)
podman-compose -f .devcontainer/docker-compose.yml up -d

# Connect to Claude container
podman exec -it devcontainer_claude_1 bash

# View logs
podman-compose -f .devcontainer/docker-compose.yml logs -f

# Stop all services
podman-compose -f .devcontainer/docker-compose.yml down
```

## Environment Variables

Set these in your `.env` file:

```bash
# For private repository access
GITHUB_TOKEN=your_github_token

# Repository to clone (defaults to werdnum/family-assistant)
CLAUDE_PROJECT_REPO=https://github.com/your/repo.git

# Database connection (automatically configured)
TEST_DATABASE_URL=postgresql+asyncpg://test:test@postgres:5432/test

# API keys for MCP servers
BRAVE_API_KEY=...
HOMEASSISTANT_API_KEY=...
GOOGLE_MAPS_API_KEY=...
```

### GitHub App Authentication

The development container supports authenticating with GitHub using a GitHub App, which provides higher rate limits and finer-grained permissions than Personal Access Tokens (PATs).

You can configure GitHub App authentication by setting environment variables in your `.env` file (located in the project root or `.devcontainer/`).

### Configuration

To enable GitHub App authentication:

1.  Place your GitHub App private key file somewhere on your host machine (e.g., `~/.ssh/github-app.pem` or inside the project directory).
2.  Set the `GITHUB_APP_PRIVATE_KEY_PATH` in your `.env` file to the path of this file.
3.  Set the App ID and Installation ID.

```bash
# ID of the GitHub App
GITHUB_APP_ID=123456

# Installation ID for your account/organization
GITHUB_APP_INSTALLATION_ID=78901234

# Path to the private key file on your host
# Relative paths are relative to the docker-compose.yml file (i.e., .devcontainer/)
# Absolute paths are recommended for clarity
GITHUB_APP_PRIVATE_KEY_PATH=/home/user/.ssh/github-app.pem
```

The container setup will automatically mount this file to `/run/secrets/github_app_private_key` inside the container and configure `git` to use it for authentication.

### Verification

To verify that authentication is working, you can check the git credential helper configuration inside the container:

```bash
git config --global credential.helper
```

It should show a function that calls `gh-token`.

## Implementation Status

See [docs/design/claude-dev-container.md](../docs/design/claude-dev-container.md) for the full
design and implementation plan.

### Completed

- [x] Phase 1.1: Test infrastructure updates (TEST_DATABASE_URL support)
- [x] Phase 1.2: Project configuration (.gitignore, placeholders)
- [x] Phase 2.1: Dockerfile creation with all tools
- [x] Phase 2.2: Workspace setup script with auto-cloning
- [x] Phase 2.3: Basic Docker Compose with PostgreSQL sidecar
- [x] Isolated workspace implementation (no host mounts)

### Pending

- [ ] Phase 3: Multi-service architecture (separate backend/frontend)
- [ ] Phase 4: Claude integration (MCP servers, configuration)
- [ ] Phase 5: Advanced features (multiple instances, K8s manifests)
- [ ] Phase 6: Documentation and comprehensive testing

## Advantages of Isolated Workspaces

1. **No Conflicts**: Host and container environments are completely separate
2. **Clean State**: Each container starts with a fresh clone
3. **Parallel Execution**: Multiple containers can run simultaneously without interference
4. **Resource Isolation**: Container resource limits don't affect host
5. **Security**: No risk of container modifying host files
6. **Reproducibility**: Every run starts from the same clean state

## Building the Container

The development container can be built using the provided script:

```bash
# Run from anywhere - the script handles the correct directory
.devcontainer/build-and-push.sh

# Or with a specific tag
.devcontainer/build-and-push.sh v1.0.0
```

Or manually with Docker/Podman (must be run from .devcontainer directory):

```bash
cd .devcontainer
docker build -t family-assistant-devcontainer .
# or
podman build -t family-assistant-devcontainer .
```

**Important**: Always build from the `.devcontainer` directory to use the development Dockerfile.
The root directory contains a production Dockerfile that builds frontend assets.

## Troubleshooting

### Container Issues

```bash
# Check running containers
podman ps -a | grep devcontainer

# View container logs
podman logs devcontainer_claude_1

# Clean up old containers
podman ps -a | grep devcontainer | awk '{print $1}' | xargs podman rm -f
```

### Network Issues

```bash
# Check if network exists
podman network ls | grep devcontainer_devnet

# Recreate network if needed
podman network rm devcontainer_devnet
podman network create devcontainer_devnet
```

### Volume Management

```bash
# List volumes
podman volume ls | grep family-assistant

# Inspect a volume
podman volume inspect workspace-claude

# Clean up test volumes
podman volume ls | grep family-assistant-test | awk '{print $2}' | xargs podman volume rm
```

## Container Caching Strategy

### Cache Backends

The CI workflows use **registry-primary caching** with unique scopes:

- **Base image cache**: `type=registry,ref=ghcr.io/.../cache-devcontainer-base`
- **CI image cache**: `type=registry,ref=ghcr.io/.../cache-devcontainer-ci`
- **Dev image cache**: `type=registry,ref=ghcr.io/.../cache-devcontainer-dev`

**Why registry over GHA cache?**

- No size limits (GHA has 10GB limit per repo)
- Better performance for large images (>2GB)
- Shared across workflows and branches
- No rate limiting issues

### Pinned Dependencies

**claudecodeui** is pinned to commit `3c9a4cab82` (as of 2025-10-01) to ensure:

- Layer caching works consistently
- Builds are reproducible
- Git clone doesn't invalidate cache on every build

To update the pinned commit:

1. Find latest commit:
   `curl -s https://api.github.com/repos/siteboon/claudecodeui/commits/main | jq -r '.sha'`
2. Update `Dockerfile` line 24 with new commit SHA
3. Rebuild and test

### Cache Troubleshooting

**Check cache usage in CI:**

```bash
# View recent build logs
gh run view <run-id> --log | grep -i "cached\|cache\|pulling"

# Look for "CACHED" steps (good!)
# vs "downloading" or "pulling" (cache miss)
```

**Force cache rebuild:**

```bash
# Trigger workflow dispatch with new tag
gh workflow run build-containers.yml -f tag=rebuild-$(date +%s)
```

**Clear registry caches (if corrupted):**

```bash
# Delete cache images from GHCR
# Go to: https://github.com/werdnum/family-assistant/pkgs/container/family-assistant
# Delete images with "cache-" prefix
```

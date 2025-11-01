# Claude Code Development Container

## Overview

This document describes the containerized development environment for Claude Code working on the
family-assistant project. The system provides completely isolated workspaces with no host mounts,
ensuring clean, reproducible environments for development and testing.

## Key Features

1. **Complete Isolation**: Each container gets its own git clone - no host filesystem mounts
2. **Podman Compatible**: Works with both Docker and Podman (rootless containers)
3. **Fast Testing**: PostgreSQL sidecar replaces testcontainers for instant database access
4. **Parallel Safe**: Multiple instances can run simultaneously without interference
5. **Full Tool Support**: All development tools (ast-grep, ripgrep, uv, playwright, etc.)
   pre-installed

## Architecture

### Current Implementation (Phase 2)

The system currently provides a single development container with PostgreSQL sidecar:

1. **Development Container** (`family-assistant-devcontainer`)

   - Ubuntu 24.04 base with all development tools
   - Clones fresh copy of the repository
   - Creates isolated Python virtual environment
   - Runs tests with PostgreSQL sidecar

2. **PostgreSQL Sidecar** (`devcontainer-postgres-test`)

   - pgvector/pgvector:0.8.0-pg17 image
   - Provides instant database for tests (no testcontainers startup)
   - Cleaned up after each test run

### Isolated Workspace Design

Key design decision: **No host filesystem mounts**

- Each container clones the repository fresh from GitHub
- Ensures complete isolation between host and container
- Prevents virtualenv conflicts and permission issues
- Changes must be committed and pushed to test in container

## Implementation Details

### Container Image (`.devcontainer/Dockerfile`)

The Dockerfile installs all required development tools:

- **Base**: Ubuntu 24.04 with build essentials
- **Search/Edit Tools**: ripgrep, fd-find, micro, ast-grep
- **Languages**: Python 3, Node.js 20 LTS, Deno
- **Package Managers**: uv (Python), npm
- **Claude Code**: Latest CLI version
- **Testing**: Playwright with Chromium, jq for JSON processing

### Workspace Setup Script (`.devcontainer/setup-workspace.sh`)

The setup script creates a completely isolated environment:

1. **Repository Cloning**

   - Uses `GITHUB_TOKEN` from `.env` for private repos
   - Clones from `CLAUDE_PROJECT_REPO` environment variable
   - Falls back to running command if already in workspace

2. **Python Environment**

   - Always creates fresh virtual environment
   - Installs all dependencies with `uv sync --extra dev`

3. **Frontend Setup**

   - Installs npm dependencies
   - Configures Playwright browsers

4. **Pre-commit Hooks**

   - Installs git hooks for code quality

## Test Scripts

The implementation includes several test runner scripts:

### Basic Testing Scripts

1. **`run-tests-isolated.sh`**

   - Runs integration tests in isolated container
   - Creates fresh PostgreSQL and workspace
   - Quick feedback (~30 seconds for integration tests)

2. **`run-full-tests-isolated.sh`**

   - Runs complete test suite (`poe test`)
   - Includes linting and all tests
   - Takes 10-15 minutes
   - Returns proper exit codes

3. **`run-full-tests-keep-on-failure.sh`**

   - Same as above but keeps container running on failure
   - Useful for debugging test failures
   - Provides commands to inspect results

### Docker Compose (Phase 3 - Future)

The `docker-compose.yml` file is prepared for multi-service architecture:

- Separate containers for backend, frontend, and Claude
- Shared workspace volumes
- Service dependencies and health checks

## Current Status

### Completed (Phases 1-2)

✅ Infrastructure preparation (TEST_DATABASE_URL support) ✅ Basic container with all development
tools ✅ Isolated workspace implementation (no host mounts) ✅ PostgreSQL sidecar integration ✅ Test
runner scripts ✅ Podman compatibility verified ✅ Proper exit code handling

### Pending (Phases 3-6)

- Multi-service architecture (backend/frontend separation)
- Claude configuration files (CLAUDE.local.md, settings)
- MCP server integration
- Documentation and examples

## Usage

### Prerequisites

1. Create a GitHub personal access token with repo read permissions

2. Add to `.env` file:

   ```bash
   GITHUB_TOKEN=your_token_here
   ```

### Running Tests

```bash
# Quick integration tests (~30 seconds)
.devcontainer/run-tests-isolated.sh

# Full test suite (~10-15 minutes)
.devcontainer/run-full-tests-isolated.sh

# Debug failures (keeps container running)
.devcontainer/run-full-tests-keep-on-failure.sh
```

### Building the Container

```bash
# With Docker
docker build -t family-assistant-devcontainer .devcontainer/

# With Podman
podman build -t family-assistant-devcontainer .devcontainer/
```

## Key Design Decisions

### Why No Host Mounts?

The decision to avoid host filesystem mounts was made to ensure:

- **Complete isolation** between host and container environments
- **No virtualenv conflicts** (container .venv vs host .venv)
- **Clean testing** - forces proper git workflow
- **Reproducibility** - every run starts from known state

### Why PostgreSQL Sidecar?

Using a PostgreSQL sidecar instead of testcontainers provides:

- **Instant startup** - database ready immediately
- **Consistent behavior** - same database for all tests
- **Podman compatibility** - avoids Docker-in-Docker issues
- **Resource efficiency** - one database instance per test run

## Known Limitations

1. **Test Isolation**: Some tests may fail when run in parallel due to shared state in the test
   suite (not a container issue)
2. **Push Required**: Changes must be committed and pushed to test in container
3. **Resource Usage**: Full test suite requires significant CPU/memory

## Troubleshooting

### Container won't start

- Check if podman/docker daemon is running
- Verify GITHUB_TOKEN is set in .env
- Ensure no conflicting containers with same names

### Tests fail in container but pass locally

- Likely due to test parallelization issues
- Try running specific tests individually
- Check test report with jq: `podman exec CONTAINER jq '.summary' /workspace/.report.json`

### Debugging failed tests

```bash
# Use the keep-on-failure script
.devcontainer/run-full-tests-keep-on-failure.sh

# When it fails, inspect with:
podman exec -it family-assistant-full-test bash
cat /workspace/.report.json
```

## Conclusion

This containerized development environment provides a clean, isolated way to run Claude Code with
the family-assistant project. The focus on complete isolation ensures reproducible environments
while the PostgreSQL sidecar pattern enables fast, reliable testing.

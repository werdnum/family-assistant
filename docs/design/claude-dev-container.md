# Claude Code Development Container Design

## Overview

This document outlines the design and implementation plan for a containerized development
environment for Claude Code. The system provides isolated, resource-controlled environments for
parallel development work on the family-assistant project using a multi-container architecture with
shared volumes.

## Goals

1. **Isolation**: Provide safe, isolated environments for Claude Code experimentation
2. **Parallelization**: Enable multiple Claude instances to work simultaneously without interference
3. **Consistency**: Ensure identical development environments across instances
4. **Performance**: Optimize for fast startup and test execution
5. **Flexibility**: Support various deployment scenarios (local Docker, Podman, Kubernetes)

## Architecture

### Container Services

The system uses a sidecar pattern with four main services:

1. **claude** - Interactive Claude Code environment

   - Runs Claude Code CLI
   - Has access to all project files and tools
   - Connects to other services via Docker networking

2. **backend** - Python backend server

   - Runs FastAPI application on port 8000
   - Auto-reloads on code changes
   - Uses shared workspace volume

3. **frontend** - Vite development server

   - Runs on port 5173
   - Hot module replacement enabled
   - Proxies API requests to backend

4. **postgres** - PostgreSQL database

   - Uses pgvector/pgvector:0.8.0-pg17 image
   - Replaces testcontainers for faster test execution
   - Persistent data volume for development

### Shared Resources

All containers share:

- **workspace volume**: Contains project code and dependencies
- **Docker network**: Enables inter-container communication
- **Environment variables**: API keys and configuration

## Technical Design

### Base Container Image

```dockerfile
FROM ubuntu:24.04

# System essentials
RUN apt-get update && apt-get install -y \
    curl git wget gnupg ca-certificates \
    ripgrep fd-find micro \
    && rm -rf /var/lib/apt/lists/*

# Development tools not in pyproject.toml
# ast-grep is required by CLAUDE.md instructions
RUN curl -L https://github.com/ast-grep/ast-grep/releases/latest/download/ast-grep-x86_64-unknown-linux-gnu.tar.gz \
    | tar xz -C /usr/local/bin/

# Node.js 20 LTS for Claude Code and npm packages
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs

# Python package manager
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

# Deno for MCP servers
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:$PATH"

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /workspace
```

### Workspace Initialization

The setup script handles:

1. Git repository cloning (if CLAUDE_PROJECT_REPO is set)
2. Python virtual environment creation
3. Dependency installation via `uv pip install -e .[dev]`
4. Playwright browser installation
5. Node.js dependency installation
6. MCP server tool installation

### Docker Compose Configuration

```yaml
version: '3.8'

volumes:
  workspace:     # Shared code and virtual environment
  postgres-data: # Database persistence

services:
  postgres:
    image: pgvector/pgvector:0.8.0-pg17
    environment:
      POSTGRES_USER: test
      POSTGRES_PASSWORD: test
      POSTGRES_DB: test
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U test"]
      interval: 5s
      retries: 5

  backend:
    build: .devcontainer
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      - DATABASE_URL=postgresql+asyncpg://test:test@postgres:5432/test
      - TEST_DATABASE_URL=postgresql+asyncpg://test:test@postgres:5432/test
      - DEV_MODE=true
    volumes:
      - workspace:/workspace
      - ${PWD}:/workspace:z  # Optional local mount override
    command: ["/usr/local/bin/setup-workspace.sh", "poe", "serve"]
    ports:
      - "8000:8000"

  frontend:
    build: .devcontainer
    volumes:
      - workspace:/workspace
      - ${PWD}:/workspace:z
    working_dir: /workspace/frontend
    command: ["/usr/local/bin/setup-workspace.sh", "npm", "run", "dev"]
    ports:
      - "5173:5173"

  claude:
    build: .devcontainer
    depends_on:
      - backend
      - frontend
      - postgres
    environment:
      - CLAUDE_PROJECT_REPO=${CLAUDE_PROJECT_REPO:-}
      - BRAVE_API_KEY=${BRAVE_API_KEY}
      - HOMEASSISTANT_API_KEY=${HOMEASSISTANT_API_KEY}
      - GOOGLE_MAPS_API_KEY=${GOOGLE_MAPS_API_KEY}
      - DATABASE_URL=postgresql+asyncpg://test:test@postgres:5432/test
      - TEST_DATABASE_URL=postgresql+asyncpg://test:test@postgres:5432/test
    volumes:
      - workspace:/workspace
      - ${PWD}:/workspace:z
      - ~/.claude:/home/claude/.claude:ro
    stdin_open: true
    tty: true
    command: ["/usr/local/bin/setup-workspace.sh", "claude"]
```

### Claude Configuration

**CLAUDE.local.md** provides container-specific context:

- Service URLs and ports
- Available commands
- Container-specific notes

**settings.local.json** adapted from project settings:

- Removes notification hooks (incompatible with container)
- Adds container-specific allowed commands
- Maintains all existing permissions

### MCP Server Support

All MCP servers from mcp_config.json are supported with adjusted paths:

- Python servers use `/workspace/.venv/bin/python`
- Node servers use deno with npm: imports
- uvx tools installed in container

## Implementation Plan

### Phase 1: Infrastructure Preparation

#### 1.1 Test Infrastructure Updates

- Modify `tests/conftest.py` to support external PostgreSQL
- Add environment variable check for `TEST_DATABASE_URL`
- Create bypass mechanism for testcontainers
- Ensure backward compatibility (testcontainers still work locally)

#### 1.2 Project Configuration

- Add `.devcontainer/` to `.gitignore`
- Create placeholder for container-specific configurations
- Document environment variables needed

### Phase 2: Basic Container Implementation

#### 2.1 Dockerfile Creation

- Create `.devcontainer/Dockerfile`
- Install system dependencies
- Add language runtimes (Node.js, Python/uv, Deno)
- Install Claude Code globally

#### 2.2 Workspace Setup Script

- Create `scripts/setup-workspace.sh`
- Handle git cloning logic
- Implement dependency installation
- Add Playwright browser setup

#### 2.3 Basic Docker Compose

- Single Claude container initially
- Volume mounts for code and auth
- Basic environment variables

### Phase 3: Multi-Service Architecture

#### 3.1 PostgreSQL Sidecar

- Add postgres service to compose
- Configure health checks
- Update environment variables
- Test database connectivity

#### 3.2 Backend Service

- Add backend container definition
- Configure auto-reload
- Set up proper networking
- Verify API accessibility

#### 3.3 Frontend Service

- Add frontend container
- Configure Vite for container environment
- Set up proxy to backend
- Test hot module replacement

### Phase 4: Claude Integration

#### 4.1 Claude Configuration Files

- Create CLAUDE.local.md template
- Adapt settings.local.json
- Set up proper file copying in setup script

#### 4.2 MCP Server Configuration

- Update mcp_config.json paths
- Test each MCP server
- Document any limitations

#### 4.3 Volume Optimization

- Implement proper volume strategy
- Optimize for performance
- Handle node_modules separately if needed

### Phase 5: Advanced Features

#### 5.1 Multiple Instance Support

- Document COMPOSE_PROJECT_NAME usage
- Create example configurations
- Test parallel execution

#### 5.2 Podman Compatibility

- Test with podman-compose
- Document any differences
- Create podman-specific instructions

#### 5.3 Kubernetes Manifests (Optional)

- Create Kubernetes deployment examples
- Use emptyDir for shared storage
- Document kubectl usage

### Phase 6: Documentation and Testing

#### 6.1 User Documentation

- Create README in .devcontainer/
- Add usage examples
- Document common issues

#### 6.2 Integration Testing

- Test all poe commands
- Verify MCP servers work
- Test parallel instances

#### 6.3 Performance Optimization

- Measure startup times
- Optimize image layers
- Cache dependency installation

## Usage Examples

### Basic Usage

```bash
# Start all services
docker compose up -d

# Connect to Claude
docker compose exec claude claude

# View logs
docker compose logs -f
```

### With Git Repository

```bash
# Clone and set up from repository
CLAUDE_PROJECT_REPO=https://github.com/user/repo docker compose up -d
```

### Multiple Instances

```bash
# Developer 1
COMPOSE_PROJECT_NAME=dev1 docker compose up -d

# Developer 2  
COMPOSE_PROJECT_NAME=dev2 docker compose up -d
```

## Benefits

1. **Fast Testing**: PostgreSQL always ready, no testcontainer startup
2. **Consistency**: Identical environments across all instances
3. **Isolation**: Each instance completely separated
4. **Flexibility**: Works with Docker, Podman, or Kubernetes
5. **Developer Experience**: Everything works out of the box

## Future Enhancements

1. **Resource Limits**: Add CPU/memory constraints
2. **GPU Support**: For ML workloads
3. **Cloud Integration**: Deploy to cloud providers
4. **CI/CD Integration**: Use in GitHub Actions
5. **Extension System**: Allow custom tool installation

## Security Considerations

1. **Authentication**: Mount Claude auth read-only
2. **Network Isolation**: Containers on isolated network
3. **Secret Management**: Use Docker secrets for API keys
4. **File Permissions**: Run as non-root user (future)
5. **Resource Limits**: Prevent resource exhaustion

## Conclusion

This development container system provides a robust, scalable solution for running Claude Code in
isolated environments. The multi-container architecture with shared volumes offers the best balance
of isolation, performance, and developer experience.

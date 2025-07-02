# Claude Code Development Container

This directory contains the Docker configuration for running Claude Code in isolated development
containers.

## Overview

The development container system provides:

- Isolated environments for parallel Claude Code instances
- Multi-container architecture (claude, backend, frontend, postgres)
- Shared volumes for file synchronization
- PostgreSQL sidecar replacing testcontainers for faster tests

## Quick Start

```bash
# Start all services
docker compose up -d

# Connect to Claude
docker compose exec claude claude

# View logs
docker compose logs -f
```

## Environment Variables

- `TEST_DATABASE_URL`: External PostgreSQL URL (bypass testcontainers)
- `CLAUDE_PROJECT_REPO`: Git repository to clone on startup
- `BRAVE_API_KEY`: API key for Brave search MCP server
- `HOMEASSISTANT_API_KEY`: API key for Home Assistant MCP server
- `GOOGLE_MAPS_API_KEY`: API key for Google Maps MCP server

## Implementation Status

See [docs/design/claude-dev-container.md](../docs/design/claude-dev-container.md) for the full
design and implementation plan.

### Completed

- [x] Phase 1.1: Test infrastructure updates (TEST_DATABASE_URL support)
- [x] Phase 1.2: Project configuration

### In Progress

- [ ] Phase 2.1: Dockerfile creation
- [ ] Phase 2.2: Workspace setup script
- [ ] Phase 2.3: Basic Docker Compose

### Pending

- [ ] Phase 3: Multi-service architecture
- [ ] Phase 4: Claude integration
- [ ] Phase 5: Advanced features
- [ ] Phase 6: Documentation and testing

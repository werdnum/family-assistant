# CLAUDE.devcontainer.md

This file documents specific information for Claude when running in the devcontainer environment.

## Development Server Setup

The devcontainer runs a Kubernetes StatefulSet with three containers:

### Container Architecture

1. **postgres** - PostgreSQL database with pgvector extension

   - Port: 5432
   - Credentials: user=test, password=test, database=test
   - Data stored in ephemeral volume (lost on pod restart)

2. **backend** - Application server

   - Ports: 8000 (backend API), 5173 (frontend dev server)
   - Runs `poe dev` which starts both backend and Vite frontend
   - PostgreSQL connection: `postgresql+asyncpg://test:test@localhost:5432/test`
   - Development mode enabled (`DEV_MODE=true`)

3. **claude** - Claude Code Web UI

   - Port: 8080
   - Runs `claude-code-webui` with MCP servers configured
   - Read-only access to development server logs

### Access Points

- **Main Application**: http://localhost:5173 (frontend dev server with HMR)
- **Backend API**: http://localhost:8000 (FastAPI with auto-reload)
- **Claude Code Web UI**: http://localhost:8080
- **Health Check**: http://localhost:8000/health

## Development Server Logs

When running in the Kubernetes devcontainer pod, the development server logs from the backend
container are available to the claude container at:

```
/var/log/family-assistant/dev-server.log
```

This log file contains the combined output from both the backend API server (uvicorn) and the
frontend dev server (vite) that are started by `poe dev`.

### Accessing Logs

To view the logs in real-time:

```bash
tail -f /var/log/family-assistant/dev-server.log
```

To search for specific patterns in the logs:

```bash
grep "error" /var/log/family-assistant/dev-server.log
grep -i "exception" /var/log/family-assistant/dev-server.log
```

To see the last 100 lines:

```bash
tail -n 100 /var/log/family-assistant/dev-server.log
```

### Log Contents

The log file includes:

- Backend API server startup messages
- HTTP request logs from the backend
- Frontend dev server startup messages
- Frontend build and hot-reload messages
- Any errors or exceptions from either server
- Database connection messages
- WebSocket connection logs

Note: The logs are stored in an emptyDir volume, so they will be lost when the pod is restarted.

## Container Details

### Volume Mounts

- **workspace**: `/workspace` (20Gi persistent volume) - Project code and build artifacts
- **claude-home**: `/home/claude` (persistent host path) - Claude settings and cache
- **postgres-data**: `/var/lib/postgresql/data` (ephemeral) - Database files, lost on restart
- **dev-logs**: `/var/log/family-assistant` (ephemeral) - Shared log directory

### Environment Variables

Backend container:

- `DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test`
- `TEST_DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test`
- `DEV_MODE=true`
- `HOME=/home/claude`

### Process Details

- **Backend container** runs: `poe dev 2>&1 | tee /var/log/family-assistant/dev-server.log`
  - This starts both uvicorn (port 8000) and vite dev server (port 5173)
  - Output is both logged to file and displayed on stdout
- **Claude container** runs: `claude-code-webui --port 8080 --host 0.0.0.0`

### Testing with Playwright MCP Server

The Playwright MCP server can test the running application at `http://localhost:5173`. This is
useful for:

- Verifying redirects (e.g., root path redirects to `/chat`)
- Testing UI functionality and forms
- End-to-end workflow validation
- Screenshot/snapshot capture for debugging

Example:

```python
# Test the redirect implemented in this branch
await mcp__playwright__browser_navigate("http://localhost:5173/")
# Should see redirect to /chat interface
```

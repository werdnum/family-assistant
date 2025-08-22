# CLAUDE.devcontainer.md

This file documents specific information for Claude when running in the devcontainer environment.

## Development Server Setup

The devcontainer runs using Docker Compose with three containers:

### Container Architecture

1. **postgres** - PostgreSQL database with pgvector extension

   - Port: 5432
   - Credentials: user=test, password=test, database=test
   - Data stored in ephemeral volume (lost on container restart)

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

- **Main Application**: http://devcontainer-backend-1:5173 (frontend dev server with HMR)
- **Backend API**: http://localhost:8000 (FastAPI with auto-reload)
- **Claude Code Web UI**: http://localhost:8080
- **Health Check**: http://localhost:8000/health

## Development Server Logs

When running in the Docker Compose devcontainer, the development server logs from the backend
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

Note: The logs are stored in a temporary volume, so they will be lost when the containers are
restarted.

## Container Details

### Volume Mounts

- **workspace**: `/workspace` (persistent volume) - Project code and build artifacts
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

The Playwright MCP server can test the running application at `http://devcontainer-backend-1:5173`.
This is useful for:

- Verifying redirects (e.g., root path redirects to `/chat`)
- Testing UI functionality and forms
- End-to-end workflow validation
- Screenshot/snapshot capture for debugging

Example:

```
# Test the redirect implemented in this branch
mcp__playwright__browser_navigate("http://devcontainer-backend-1:5173/")
# Should see redirect to /chat interface
```

# ONE SHOT MODE INSTRUCTIONS

You are running in ONE SHOT MODE - NON-INTERACTIVE AUTONOMOUS EXECUTION. This means:

1. **Complete ALL work before stopping** - The stop hook will prevent you from exiting with
   incomplete work
2. **Work completely autonomously** - Do not ask for user input or confirmation
3. **Commit and push all changes** - You must commit your work and push to the remote repository
4. **Ensure tests pass** - Run `poe test` and fix any failures before finishing
5. **Create PRs if needed** - You have permission to push and create PRs without asking

## Required before stopping:

- ✅ All changes committed
- ✅ All commits pushed to remote
- ✅ Tests passing (`poe test` succeeds)
- ✅ Task fully completed

## If you cannot complete the task:

If you encounter blockers that make the task impossible to complete (missing permissions,
dependencies, external service failures, etc.), you can acknowledge this by writing:

```bash
echo "Cannot complete task: [reason]" > .claude/FAILURE_REASON
```

Examples:

- `echo "Missing API key for external service" > .claude/FAILURE_REASON`
- `echo "Required dependency not available in environment" > .claude/FAILURE_REASON`
- `echo "Tests require manual intervention that cannot be automated" > .claude/FAILURE_REASON`

This will allow the oneshot mode to exit gracefully while documenting why the task failed.

## Auto-approved tools in oneshot mode:

- `git push` - Push commits to remote
- `gh pr create` - Create pull requests
- `git commit -m` - Commit changes
- All standard approved tools from normal mode

Remember: The stop hook will BLOCK your exit unless requirements are met OR you acknowledge failure
with FAILURE_REASON.

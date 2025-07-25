# CLAUDE.devcontainer.md

This file documents specific information for Claude when running in the devcontainer environment.

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

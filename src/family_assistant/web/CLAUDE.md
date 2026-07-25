# Web API Development Guide

This file provides guidance for working with the FastAPI web application layer for the Family
Assistant.

## Architecture

- **`app_creator.py`**: sets up the FastAPI app with middleware, routers, static files, and the
  Jinja2 template environment
- **`auth.py`**: OIDC session auth and API-token auth, plus the `AuthMiddleware` and its
  `PUBLIC_PATHS` regex list of endpoints that bypass authentication
- **`dependencies.py`**: FastAPI dependency injection for database context, services, and the
  current user
- **`models.py`**: Pydantic models for API requests and responses
- **`utils.py`**: shared utility functions

Routers live in `routers/`, one per feature area. `api.py` is the aggregator that mounts the other
`*_api.py` routers under the `/api` prefix.

## Adding New Web API Endpoints

1. Create the router in `routers/`, following existing patterns for dependency injection.
2. Define request/response Pydantic models in `models.py` if needed.
3. Register the router in `app_creator.py` with the correct prefix and tags.
4. If the endpoint should bypass authentication, add it to the public-path configuration in
   `auth.py`; otherwise use dependency injection for auth checks and document the requirement in the
   endpoint docstring.
5. Add the endpoint to the appropriate test files in `tests/functional/web/`.

## Notable Behaviours

- **Auth flow**: requests hit `AuthMiddleware` for path-based checking; OIDC users get session-based
  auth, API clients present Bearer tokens validated against the database, and public paths bypass
  authentication entirely.
- **Chat API**: routes requests to different processing service profiles, keys context off
  conversation IDs, and supports response streaming.
- **Vector search**: hybrid search combining vector similarity with full-text search via RRF
  (Reciprocal Rank Fusion), across multiple embedding types.
- **Tools**: MCP integration for external tools, plus a confirmation mechanism for destructive
  operations.

## Error Reports vs. Telemetry Breadcrumbs

Frontend clients POST to `POST /api/errors/`. The report's optional `severity` selects the lane:

- Absent or `"error"` → **error lane**: logged at `ERROR` and persisted to `error_logs` (the table
  the engineer profile reads via `read_error_logs` and a human reads via `GET /api/errors/`). The
  web frontend never sets `severity`, so its reports — including React error-boundary catches that
  use `error_type: "component_error"` — stay here.
- `"info"` / `"warning"` / `"debug"` → **telemetry lane**: recorded in an in-memory ring buffer and
  logged below the `error_logs` threshold, so high-frequency breadcrumbs never drown genuine errors.
  The iOS app sends its sync breadcrumbs (stream restarts/disconnects, resync phases, transport
  events) here. Read them via `GET /api/errors/telemetry` (same diagnostics-reader gate) or the
  engineer-profile `read_frontend_telemetry` tool. The buffer is dropped on restart.

Routing is decided by `severity` alone — `error_type` has no effect on it. Both clients send
`error_type: "component_error"`, but the web frontend never sets `severity` (so those land in the
error lane) while the iOS client maps it to `"info"` (so those land in telemetry). Do not infer the
lane from `error_type`.

See
[docs/design/ios-frontend-telemetry-lane.md](../../../docs/design/ios-frontend-telemetry-lane.md)
and [ios/CLAUDE.md](../../../ios/CLAUDE.md), where the split originated.

## Dependency Injection

Use FastAPI dependencies for shared resources:

```python
from family_assistant.web.dependencies import get_db, get_processing_service


@router.get("/my-endpoint")
async def my_endpoint(
    db: DatabaseContext = Depends(get_db),
    processing_service: ProcessingService = Depends(get_processing_service),
):
    pass
```

## Testing Web Endpoints

Full request/response cycles go in `tests/functional/web/`; see
[tests/functional/web/CLAUDE.md](../../../tests/functional/web/CLAUDE.md) for detailed guidance.

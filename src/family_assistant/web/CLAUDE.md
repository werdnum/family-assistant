# Web API Development Guide

This file provides guidance for working with the FastAPI web application layer for the Family
Assistant.

## Architecture

- **`app_creator.py`**: sets up the FastAPI app with middleware, routers, static files, and the
  Jinja2 template environment
- **`auth.py`**: OIDC session auth and API-token auth, plus the fail-closed `AuthMiddleware`
- **`route_auth.py`**: the shared classification for `/api` routes that do not use the default
  middleware authentication
- **`dependencies.py`**: FastAPI dependency injection for database context, services, and the
  current user
- **`models.py`**: Pydantic models for API requests and responses
- **`utils.py`**: shared utility functions

Routers live in `routers/`, one per feature area. `api.py` is the aggregator that mounts the other
`*_api.py` routers under the `/api` prefix.

## Adding New Web API Endpoints

1. Create the router in `routers/`, following existing patterns for dependency injection.
2. Define request/response Pydantic models in `models.py` if needed.
3. Register the router. An ordinary `*_api.py` router is registered on `api_router` in
   `routers/api.py` with its prefix and tags, so it lands under `/api`; register directly in
   `app_creator.py` only for a router that is deliberately mounted outside `/api`.
4. `/api` routes require `AuthMiddleware` authentication by default. A route that must use a scoped
   dependency or remain public must be classified in `route_auth.NO_DEFAULT_AUTH_ROUTES`; that
   registry also drives the published edge-gateway contract and bootstrap body limits. Scoped routes
   still declare their route-specific dependency. `PUBLIC_PATHS` is only for non-API page routes.
5. Add the endpoint to the appropriate test files in `tests/functional/web/`.

## Notable Behaviours

- **Auth flow**: requests hit `AuthMiddleware` for path-based checking; OIDC users get session-based
  auth, API clients present Bearer tokens validated against the database, and explicitly classified
  page/API routes use their narrower authentication policy.
- **Chat API**: routes requests to different processing service profiles, keys context off
  conversation IDs, and supports response streaming.
- **Vector search**: hybrid search combining vector similarity with full-text search via RRF
  (Reciprocal Rank Fusion), across multiple embedding types.
- **Tools**: MCP integration for external tools, plus a confirmation mechanism for destructive
  operations.

## Client Sync

`GET /api/v1/sync` (`routers/sync_api.py`) returns everything that changed since a client's cursor:
conversation rows, messages of the conversations it has open, pending confirmations and delegations,
and the progress of running turns. `wait` turns it into a long poll and `/api/v1/sync/stream`
repeats it over one connection. See
[docs/design/mobile-state-sync.md](../../../docs/design/mobile-state-sync.md).

Change tracking is not something an endpoint or repository does by hand. Every row of a table listed
in `storage/sync_tracking.py`'s `SYNCED_TABLES` carries a `sync_cursor`, and `DatabaseTransaction`
stamps it on every symbolic insert, update and delete (a delete leaves a tombstone). Two
consequences:

- **A raw SQL write to a synced table is invisible to clients.** Write symbolically, as the project
  already requires.
- **Giving clients a new collection** means adding its table to `SYNCED_TABLES`, giving the table
  `sync_cursor_column()` and a migration, and teaching `build_sync_response` to report it. A poll a
  client still runs for that data is removed only once the collection is synced.

The reference client in `tests/sync/reference_client.py` is the protocol's executable specification;
the convergence test checks it against a client that bootstrapped from nothing.

## Error Reports vs. Telemetry Breadcrumbs

Frontend clients POST to `POST /api/errors/`. The report's optional `severity` selects the lane:

- Absent or `"error"` → **error lane**: logged at `ERROR` and persisted to `error_logs` (the table
  the engineer profile reads via `read_error_logs` and a human reads via `GET /api/errors/`). The
  web frontend's general error reporter never sets `severity`, so its reports — including React
  error-boundary catches that use `error_type: "component_error"` — stay here.
- `"info"` / `"warning"` / `"debug"` → **telemetry lane**: recorded in an in-memory ring buffer and
  logged below the `error_logs` threshold, so high-frequency breadcrumbs never drown genuine errors.
  The iOS app sends its sync breadcrumbs (stream restarts/disconnects, resync phases, transport
  events) here. Web voice tool-stage breadcrumbs also use this lane. Read them via
  `GET /api/errors/telemetry` (same diagnostics-reader gate) or the engineer-profile
  `read_frontend_telemetry` tool. The buffer is dropped on restart.

Routing is decided by `severity` alone — `error_type` has no effect on it. Both clients send
`error_type: "component_error"`. The web error client omits `severity` (error lane), while web voice
tool events explicitly set `"info"` and the iOS client maps component breadcrumbs to `"info"`
(telemetry lane). Do not infer the lane from `error_type`.

See
[docs/design/ios-frontend-telemetry-lane.md](../../../docs/design/ios-frontend-telemetry-lane.md)
and [ios/CLAUDE.md](../../../ios/CLAUDE.md), where the split originated.

## Chat Telemetry in the Backend Log

The hub lives in memory and a client's reports can arrive minutes late, so the backend log is the
durable record of a turn. Stdout reaches the log store with no level filter, so these are at INFO.
They never carry message content. `web/chat_telemetry.py` writes them.

`chat_turn` lines share a `turn_id`. Their `event` is one of:

- `request_received`, `retry_matched`, `user_message_saved`: `POST /turns` progress. `retry_matched`
  means a client resent a turn the server already had, and `source` says how it was recognised.
- `started`, `first_output`, `ended`, `acked`: written by the hub, so they cover every kind of turn.
  `acked` can come after the push decision, which is how to measure a notification beating the app.
- `delivery_wait_finished`, `push_sent`, `push_skipped`, `push_failed`: the disconnect push
  decision.

`chat_stream` lines record `opened` and `closed` for each client stream. `closed` carries `reason`
(`finished`, `client_gone`, `error`, or the reason from a `stream_dropped` frame); `finished`
includes a sync stream that noticed by itself that its client had left, the duration, how many event
and heartbeat frames were written, and how long the stream had been quiet. A new long-lived stream
must go through `observed_stream`.

`frontend.telemetry` lines hold each breadcrumb as JSON, which outlasts the ring buffer. Order them
by the client's own `client_seq`, because the line's timestamp is arrival time.

## Dependency Injection

Use FastAPI dependencies for shared resources:

```python
from family_assistant.web.dependencies import get_db, get_processing_service


@router.get("/my-endpoint")
async def my_endpoint(
    db: Database = Depends(get_db),
    processing_service: ProcessingService = Depends(get_processing_service),
):
    pass
```

## Testing Web Endpoints

Full request/response cycles go in `tests/functional/web/`; see
[tests/functional/web/CLAUDE.md](../../../tests/functional/web/CLAUDE.md) for detailed guidance.

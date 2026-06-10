# Family Assistant API Documentation

This document provides guidance for integrating with the Family Assistant REST API.

## Overview

The Family Assistant API provides programmatic access to the assistant's features including:

- **Chat Interactions** - Send messages and receive AI-powered responses with optional tool
  execution
- **Notes Management** - Create, read, update, and delete notes stored by the assistant
- **Tasks** - View and manage background tasks
- **Events** - Access event history and monitoring
- **Documents** - Upload and search indexed documents
- **Vector Search** - Semantic search across stored content

## Getting Started

### Base URL

The API is served from the same host as the web interface. For example:

- Development: `http://localhost:8000/api`
- Production: `https://your-domain.com/api`

### Interactive API Documentation

Family Assistant uses FastAPI which provides automatic OpenAPI documentation:

- **Swagger UI**: Available at `/api/docs` - Interactive API explorer with try-it-out functionality
- **ReDoc**: Available at `/api/redoc` - Alternative documentation format with better navigation

These endpoints provide complete schema definitions, example requests/responses, and the ability to
test endpoints directly in your browser.

### Content Type

All API endpoints accept and return JSON unless otherwise specified. Include the appropriate
headers:

```http
Content-Type: application/json
Accept: application/json
```

For file uploads, use `multipart/form-data` instead.

## Authentication

The API supports two authentication methods:

### 1. OIDC Session Authentication (Web UI)

For web browser sessions, authentication is handled via OpenID Connect. Users authenticate through
the `/login` endpoint which redirects to the configured OIDC provider. After successful
authentication, a session cookie maintains the authentication state.

### 2. API Token Authentication (Programmatic Access)

For programmatic access, use Bearer token authentication:

```http
Authorization: Bearer <your-api-token>
```

**Check the resolved application user:**

```bash
curl "https://your-domain.com/api/me" \
  -H "Authorization: Bearer <your-api-token>"
```

`/api/auth/me` returns the same payload for clients that are already using the auth API namespace.
The response includes `user_identifier`, the canonical application user id after OIDC/API-token
identity mapping.

#### Obtaining API Tokens

API tokens can be created through:

1. **Web UI**: Navigate to token management in the web interface (when logged in via OIDC)
2. **API**: Use the token management endpoints (requires existing authentication)

**Create a new token:**

```bash
curl -X POST "https://your-domain.com/api/me/tokens" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <existing-token-or-session>" \
  -d '{
    "name": "My Integration Token",
    "expires_at": "2025-12-31T23:59:59Z"
  }'
```

**Response:**

```json
{
  "id": 1,
  "name": "My Integration Token",
  "full_token": "abc12345_secret_token_value",
  "prefix": "abc12345",
  "user_identifier": "user@example.com",
  "created_at": "2024-01-15T10:30:00Z",
  "expires_at": "2025-12-31T23:59:59Z",
  "is_revoked": false,
  "last_used_at": null
}
```

**Important**: The `full_token` is only returned once at creation time. Store it securely.

**List tokens:**

```bash
curl "https://your-domain.com/api/me/tokens" \
  -H "Authorization: Bearer <your-token>"
```

**Revoke a token:**

```bash
curl -X DELETE "https://your-domain.com/api/me/tokens/{token_id}" \
  -H "Authorization: Bearer <your-token>"
```

### Public Endpoints

Some endpoints are accessible without authentication:

- `/health` - Health check endpoint
- `/webhook/*` - Webhook endpoints for external integrations (e.g., email webhooks)

## API Endpoints

### Chat API

The chat API allows you to send messages and receive AI-powered responses.

#### Send Message

**Non-streaming request:**

```
POST /api/v1/chat/send_message
```

**Request body:**

```json
{
  "prompt": "What's on my calendar today?",
  "conversation_id": "optional-conversation-uuid",
  "profile_id": "default_assistant",
  "interface_type": "api",
  "attachments": []
}
```

| Field             | Type            | Required | Description                                           |
| ----------------- | --------------- | -------- | ----------------------------------------------------- |
| `prompt`          | string          | Yes      | The user message to process                           |
| `conversation_id` | string          | No       | UUID to continue an existing conversation             |
| `profile_id`      | string          | No       | Processing profile to use (e.g., "default_assistant") |
| `interface_type`  | string          | No       | Interface identifier (default: "api")                 |
| `attachments`     | array of object | No       | Image attachments (base64 encoded or attachment URLs) |
| `turn_id`         | string          | No       | Client UUIDv4 for idempotency (see below)             |

`turn_id` makes `/send_message` idempotent: retrying the same request with the same `turn_id`
returns the already-persisted reply instead of re-driving the LLM and persisting a second copy. The
retry's response carries `already_complete: true`. Omit `turn_id` to always generate a fresh reply.

**Response:**

```json
{
  "reply": "You have 3 meetings today...",
  "conversation_id": "uuid-of-conversation",
  "turn_id": "uuid-of-this-turn",
  "attachments": null,
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "calendar_get_events",
        "arguments": "{\"days\": 1}"
      }
    }
  ],
  "already_complete": false
}
```

| Field              | Type    | Description                                           |
| ------------------ | ------- | ----------------------------------------------------- |
| `reply`            | string  | Assistant's textual reply                             |
| `conversation_id`  | string  | Conversation the reply belongs to                     |
| `turn_id`          | string  | Turn identifier (echoes the request, or a fresh UUID) |
| `attachments`      | array   | Attachment metadata processed for this turn, if any   |
| `tool_calls`       | array   | Tool calls the assistant made, if any                 |
| `already_complete` | boolean | `true` when served from an idempotent `turn_id` retry |

This endpoint blocks until the assistant finishes and returns the full reply in one response. For
incremental token rendering or to resume a reply after a disconnect, use the resumable streaming
endpoints below.

#### Resumable Streaming (Server-Sent Events)

Streaming is a two-step, resumable flow built on a per-conversation event hub rather than a single
request/response stream. A background **turn producer** drives the LLM and publishes typed events
into a per-conversation ring buffer in the hub; one or more SSE subscribers replay buffered events
and then tail live ones. Because the producer runs independently of any connection, a client can
disconnect mid-turn and reconnect (from a saved sequence cursor) without losing the reply, and a
second device watching the same conversation sees the same event stream.

Each event's `data` payload carries a monotonic `seq` (and `turn_id` when it belongs to a turn).
Clients persist the highest `seq` they have applied and use it to resume.

##### Start a Turn

```
POST /api/v1/chat/turns
```

Kicks off the background producer for one turn. The client supplies a `turn_id`, making the call
idempotent: retrying the same `turn_id` returns the existing turn instead of starting a second
producer.

**Request body:**

```json
{
  "turn_id": "client-generated-uuidv4",
  "conversation_id": "optional-conversation-uuid",
  "prompt": "What's on my calendar today?",
  "profile_id": "default_assistant",
  "interface_type": "api",
  "attachments": []
}
```

| Field             | Type            | Required | Description                                                 |
| ----------------- | --------------- | -------- | ----------------------------------------------------------- |
| `turn_id`         | string          | Yes      | Client UUIDv4 identifying this turn; retries idempotent     |
| `prompt`          | string          | Yes      | The user message to process                                 |
| `conversation_id` | string          | No       | UUID to continue an existing conversation (auto if omitted) |
| `profile_id`      | string          | No       | Processing profile to use                                   |
| `interface_type`  | string          | No       | Interface identifier (default: "api")                       |
| `attachments`     | array of object | No       | Attachments (base64 encoded or attachment URLs)             |

**Response:**

```json
{
  "turn_id": "client-generated-uuidv4",
  "conversation_id": "uuid-of-conversation",
  "first_seq": 0,
  "already_complete": false
}
```

| Field              | Type    | Description                                                            |
| ------------------ | ------- | ---------------------------------------------------------------------- |
| `turn_id`          | string  | Turn identifier                                                        |
| `conversation_id`  | string  | Conversation the turn belongs to                                       |
| `first_seq`        | integer | `seq` of the `turn_started` event; pass as `from_seq` when subscribing |
| `already_complete` | boolean | See below                                                              |

When `already_complete` is `true`, the turn was resolved from the durable database record (the
`turn_id` was found in the DB but not in the in-memory hub — e.g. after a backend restart, or after
the completed turn was pruned/evicted). The turn already finished and is **not** replayable from the
hub, so the client must **not** open `/stream`; it should reload conversation history to show the
persisted reply instead.

##### Subscribe to the Conversation Stream

```
GET /api/v1/chat/conversations/{conversation_id}/stream
```

Replays buffered events with `seq >= from_seq`, then tails live events.

**Query parameters:**

| Parameter     | Type   | Default | Description                                                 |
| ------------- | ------ | ------- | ----------------------------------------------------------- |
| `from_seq`    | int    | `0`     | Replay cursor: first `seq` to (re)play before tailing live  |
| `ack_seq`     | int    | `-1`    | Highest `seq` the client has already applied (delivery ack) |
| `follow`      | bool   | `false` | Stream lifetime (see below)                                 |
| `event_types` | string | unset   | Comma-separated allow-list of event types to emit           |

- `follow=false` ("watch my reply / resume"): drains the buffer and streams any in-flight turn
  through its `turn_ended`, then closes. This is the dominant flow (send-then-watch,
  refresh-mid-turn resume).
- `follow=true` ("always-on live updates"): stays open with periodic heartbeats so the connection
  also surfaces turns that start later (e.g. a reply triggered from another device).
- `ack_seq` lets a reconnecting client tell the server it already received events up to that `seq`,
  suppressing the redundant disconnect-push fallback.
- `event_types` (e.g. `event_types=text,turn_ended`) restricts which event types are emitted on both
  replayed and live events. The lifecycle/control frames `turn_ended`, `heartbeat` and
  `stream_dropped` are **always** emitted regardless of the filter, since they tell the client when
  to stop, reload, or reconnect. Follow clients that only reload history use the filter to skip the
  token firehose.

**Event types:**

- `turn_started` - A new turn began.
- `text` - Text content chunk: `{"content": "partial response..."}`
- `tool_call` - Tool invocation: `{"tool_call": {...}}`
- `tool_result` - Tool execution result: `{"tool_call_id": "...", "result": "..."}`
- `attachment` - Attachment event:
  `{"type": "attachment", "source": "trigger" | "response", "attachment_id": "...", "content_url": "...", ...}`.
  `source: "trigger"` is the user's own upload republished at turn start; `source: "response"` is an
  attachment the assistant produced.
- `tool_confirmation_request` - A tool requires user approval (see Tool Confirmation).
- `tool_confirmation_result` - Resolution of a confirmation:
  `{"request_id": "...", "approved": true | false}`.
- `error` - Error occurred: `{"error": "message", "error_id": "..."}`
- `message` - Content-free "new messages were persisted" nudge published outside the token stream
  (e.g. by the non-streaming `/send_message` path or a reply delivered from another interface):
  `{"new_messages": true}`. A follow client reacts by reloading conversation history rather than
  rendering tokens.
- `turn_ended` - The turn finished: `{"status": "complete" | "failed", "reasoning_info": {...}}`.
  After handling this, a client should `POST /ack` with its `seq`.
- `heartbeat` - Keep-alive on `follow=true` (and idle non-follow) streams: `{}`.
- `stream_dropped` - The server is closing this subscription; reconnect or reload history.
  `{"reason": "queue_overflow" | "server_shutdown"}`.

A `410 Gone` response means `from_seq` is below the oldest buffered `seq` (buffer underrun). The
body includes `active_turns` so the client can render a "still thinking" placeholder while falling
back to a history reload.

##### Acknowledge Delivery

```
POST /api/v1/chat/ack
```

Records that the client has received events up to `ack_seq`, so the server's disconnect-push
fallback suppresses a redundant push for an already-delivered reply. Send this after handling a
`turn_ended` event (or after handling a push notification, without keeping a stream open).

**Request body:**

```json
{
  "conversation_id": "uuid-of-conversation",
  "ack_seq": 42
}
```

**Response:**

```json
{ "ok": true }
```

**Example using curl:**

```bash
# 1. Start the turn (client supplies a UUID turn_id; idempotent on retry).
curl -X POST "https://your-domain.com/api/v1/chat/turns" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-token>" \
  -d '{"turn_id": "'"$(uuidgen)"'", "conversation_id": "demo", "prompt": "Hello!"}'

# 2. Subscribe to the conversation event stream (replays from from_seq, then
#    tails live; follow=false closes once the turn ends).
curl -N "https://your-domain.com/api/v1/chat/conversations/demo/stream?from_seq=0" \
  -H "Authorization: Bearer <your-token>"
```

#### List Conversations

```
GET /api/v1/chat/conversations
```

**Query parameters:**

| Parameter         | Type   | Description                          |
| ----------------- | ------ | ------------------------------------ |
| `limit`           | int    | Max results to return (default: 20)  |
| `offset`          | int    | Pagination offset (default: 0)       |
| `interface_type`  | string | Filter by interface (web, api, etc.) |
| `conversation_id` | string | Filter by specific conversation ID   |
| `date_from`       | string | Filter by date (YYYY-MM-DD)          |
| `date_to`         | string | Filter by date (YYYY-MM-DD)          |

**Response:**

```json
{
  "conversations": [
    {
      "conversation_id": "uuid",
      "last_message": "Preview of the last message...",
      "last_timestamp": "2024-01-15T10:30:00Z",
      "message_count": 5
    }
  ],
  "count": 42
}
```

#### Get Conversation Messages

```
GET /api/v1/chat/conversations/{conversation_id}/messages
```

**Query parameters:**

| Parameter | Type   | Description                                     |
| --------- | ------ | ----------------------------------------------- |
| `limit`   | int    | Max messages (default: 50, use 0 for all)       |
| `before`  | string | Get messages before this timestamp (ISO format) |
| `after`   | string | Get messages after this timestamp (ISO format)  |

**Response:**

```json
{
  "conversation_id": "uuid",
  "messages": [
    {
      "internal_id": 123,
      "role": "user",
      "content": "What's the weather?",
      "timestamp": "2024-01-15T10:30:00Z",
      "tool_calls": null,
      "tool_call_id": null,
      "attachments": null
    },
    {
      "internal_id": 124,
      "role": "assistant",
      "content": "The weather in your area is...",
      "timestamp": "2024-01-15T10:30:05Z",
      "tool_calls": [...],
      "tool_call_id": null,
      "attachments": null
    }
  ],
  "count": 2,
  "total_messages": 50,
  "has_more_before": true,
  "has_more_after": false
}
```

#### Get Available Profiles

```
GET /api/v1/profiles
```

Returns available processing profiles with their capabilities.

**Response:**

```json
{
  "profiles": [
    {
      "id": "default_assistant",
      "description": "General-purpose AI assistant with access to your notes, calendar, and tools",
      "llm_model": "gpt-4",
      "available_tools": ["calendar_get_events", "notes_search", ...],
      "enabled_mcp_servers": []
    }
  ],
  "default_profile_id": "default_assistant"
}
```

#### Tool Confirmation

For tools that require user approval:

```
POST /api/v1/chat/confirm_tool
```

**Request body:**

```json
{
  "request_id": "confirmation-request-uuid",
  "approved": true,
  "conversation_id": "optional-conversation-uuid"
}
```

#### Live Message Updates

There is no separate "events" SSE endpoint. Always-on live updates use the same resumable stream
described above with `follow=true`:

```
GET /api/v1/chat/conversations/{conversation_id}/stream?follow=true&event_types=message,turn_ended
```

A client that only wants to know when to reload history (rather than render the token firehose)
passes `event_types=message,turn_ended`; it then reloads conversation messages whenever it receives
a `message` or `turn_ended` event. The lifecycle frames `turn_ended`, `heartbeat` and
`stream_dropped` are always emitted regardless of the filter.

### Notes API

Manage notes stored by the assistant.

#### List All Notes

```
GET /api/notes/
```

**Response:**

```json
[
  {
    "title": "Shopping List",
    "content": "- Milk\n- Eggs\n- Bread",
    "include_in_prompt": true,
    "attachment_ids": null
  }
]
```

#### Get a Note

```
GET /api/notes/{title}
```

**Response:**

```json
{
  "title": "Shopping List",
  "content": "- Milk\n- Eggs\n- Bread",
  "include_in_prompt": true,
  "attachment_ids": null
}
```

#### Create or Update a Note

```
POST /api/notes/
```

**Request body:**

```json
{
  "title": "Shopping List",
  "content": "Updated content here",
  "include_in_prompt": true,
  "original_title": "Old Title"
}
```

The `original_title` field enables renaming: if provided and different from `title`, the note is
renamed while preserving its identity.

**Response:**

```json
{
  "message": "Note saved"
}
```

#### Delete a Note

```
DELETE /api/notes/{title}
```

**Response:**

```json
{
  "message": "Note deleted"
}
```

### Tasks API

View and manage background tasks.

#### List Tasks

```
GET /api/tasks/
```

**Query parameters:**

| Parameter   | Type     | Description                  |
| ----------- | -------- | ---------------------------- |
| `status`    | string   | Filter by status             |
| `task_type` | string   | Filter by task type          |
| `date_from` | datetime | Filter by date               |
| `date_to`   | datetime | Filter by date               |
| `sort`      | string   | Sort order ("asc" or "desc") |
| `limit`     | int      | Max results (default: 100)   |

**Response:**

```json
{
  "tasks": [
    {
      "id": 1,
      "task_id": "unique-task-id",
      "task_type": "send_notification",
      "payload": {},
      "status": "completed",
      "created_at": "2024-01-15T10:30:00Z",
      "scheduled_at": null,
      "retry_count": 0,
      "max_retries": 3,
      "recurrence_rule": null,
      "error_message": null,
      "locked_by": null,
      "locked_at": null
    }
  ]
}
```

#### Retry a Task

```
POST /api/tasks/{internal_task_id}/retry
```

#### Cancel a Task

```
POST /api/tasks/{internal_task_id}/cancel
```

### Events API

Access event history.

#### List Events

```
GET /api/events/
```

**Query parameters:**

| Parameter        | Type   | Description                             |
| ---------------- | ------ | --------------------------------------- |
| `source_id`      | string | Filter by event source                  |
| `hours`          | int    | Time window in hours (default: 24)      |
| `only_triggered` | bool   | Only show events that triggered actions |
| `limit`          | int    | Max results (default: 50)               |
| `offset`         | int    | Pagination offset                       |

**Response:**

```json
{
  "events": [
    {
      "event_id": "evt_123",
      "source_id": "calendar",
      "event_data": {},
      "triggered_listener_ids": [1, 2],
      "timestamp": "2024-01-15T10:30:00Z"
    }
  ],
  "total": 100
}
```

#### Get Event Details

```
GET /api/events/{event_id}
```

### Documents API

Upload and manage indexed documents.

#### List Documents

```
GET /api/documents/
```

**Query parameters:**

| Parameter     | Type   | Description                |
| ------------- | ------ | -------------------------- |
| `limit`       | int    | Max results (default: 100) |
| `offset`      | int    | Pagination offset          |
| `source_type` | string | Filter by source type      |

**Response:**

```json
{
  "documents": [
    {
      "id": 1,
      "source_type": "manual_upload",
      "source_id": "doc123",
      "title": "Meeting Notes",
      "source_uri": "file://path/to/doc.pdf",
      "created_at": "2024-01-15T10:30:00Z",
      "added_at": "2024-01-15T11:00:00Z",
      "doc_metadata": {}
    }
  ],
  "total": 50
}
```

#### Get Document Details

```
GET /api/documents/{document_id}
```

Returns detailed document information including embeddings and full text content.

#### Upload a Document

```
POST /api/documents/upload
```

**Content-Type:** `multipart/form-data`

**Form fields:**

| Field           | Type   | Required | Description                         |
| --------------- | ------ | -------- | ----------------------------------- |
| `source_type`   | string | Yes      | Type (e.g., "manual_upload")        |
| `source_id`     | string | Yes      | Unique identifier                   |
| `source_uri`    | string | Yes      | Canonical URI                       |
| `title`         | string | Yes      | Document title                      |
| `uploaded_file` | file   | Cond.    | The file to upload (PDF, TXT, etc.) |
| `content_parts` | string | Cond.    | JSON of content parts if no file    |
| `url`           | string | Cond.    | URL to scrape if no file or content |
| `created_at`    | string | No       | Original creation date (ISO format) |
| `metadata`      | string | No       | Additional metadata as JSON         |

One of `uploaded_file`, `content_parts`, or `url` is required.

**Example with curl:**

```bash
curl -X POST "https://your-domain.com/api/documents/upload" \
  -H "Authorization: Bearer <your-token>" \
  -F "source_type=manual_upload" \
  -F "source_id=doc123" \
  -F "source_uri=file://meeting-notes.pdf" \
  -F "title=Q1 Meeting Notes" \
  -F "uploaded_file=@meeting-notes.pdf"
```

**Response:**

```json
{
  "message": "Document stored and embedding task enqueued.",
  "document_id": 42,
  "task_enqueued": true
}
```

#### Re-index a Document

```
POST /api/documents/{document_id}/reindex
```

Enqueues a background task to re-process and re-index the document.

### Vector Search API

Semantic search across indexed documents.

#### Search Documents

```
POST /api/vector-search/
```

**Request body:**

```json
{
  "query_text": "meeting notes from last quarter",
  "limit": 5,
  "filters": {
    "source_types": ["manual_upload"],
    "embedding_types": [],
    "created_after": "2024-01-01T00:00:00Z",
    "created_before": null,
    "title_like": null,
    "metadata_filters": {}
  }
}
```

**Response:**

```json
[
  {
    "document": {
      "id": 1,
      "title": "Q1 Planning Meeting",
      "source_type": "manual_upload",
      "source_id": "doc123",
      "source_uri": "file://...",
      "created_at": "2024-01-15T10:30:00Z",
      "metadata": {}
    },
    "score": 0.85
  }
]
```

#### Get Document Detail

```
GET /api/vector-search/document/{document_id}
```

### Tools API

Execute tools programmatically and inspect definitions.

#### Execute a Tool

```
POST /api/tools/execute/{tool_name}
```

**Request body:**

```json
{
  "arguments": {
    "arg1": "value1",
    "arg2": "value2"
  }
}
```

**Response:**

```json
{
  "success": true,
  "result": {
    "text": "Tool execution result...",
    "data": {}
  }
}
```

#### Get Tool Definitions

```
GET /api/tools/definitions
```

Returns all available tools with their schemas.

**Response:**

```json
{
  "tools": [
    {
      "name": "calendar_get_events",
      "description": "Get calendar events",
      "parameters": {
        "type": "object",
        "properties": {
          "days": {
            "type": "integer",
            "description": "Number of days to look ahead"
          }
        }
      }
    }
  ],
  "count": 25
}
```

### Health Check

```
GET /health
```

Returns service health status.

**Response:**

```json
{
  "status": "ok",
  "reason": "Telegram polling active"
}
```

Possible status values:

- `ok` / `healthy` - Service is running normally
- `initializing` - Service is starting up
- `unhealthy` - Service has issues (HTTP 503)

## Common Patterns

### Error Responses

API errors return standard HTTP status codes with JSON error details:

```json
{
  "detail": "Error message describing what went wrong"
}
```

Common status codes:

| Code | Meaning                                        |
| ---- | ---------------------------------------------- |
| 400  | Bad Request - Invalid parameters               |
| 401  | Unauthorized - Authentication required         |
| 403  | Forbidden - Insufficient permissions           |
| 404  | Not Found - Resource doesn't exist             |
| 409  | Conflict - Resource conflict (e.g., duplicate) |
| 422  | Unprocessable Entity - Validation failed       |
| 500  | Internal Server Error                          |
| 503  | Service Unavailable                            |

### Pagination

List endpoints support pagination via `limit` and `offset` parameters:

```bash
# Get first 20 items
curl ".../api/documents/?limit=20&offset=0"

# Get next 20 items
curl ".../api/documents/?limit=20&offset=20"
```

Response includes total count for calculating pages.

### Date/Time Formats

- All timestamps use ISO 8601 format
- Timezone is UTC unless otherwise specified
- Examples:
  - Full datetime: `2024-01-15T10:30:00Z`
  - Date only: `2024-01-15`

## Code Examples

### Python

```python
import requests

BASE_URL = "https://your-domain.com/api"
TOKEN = "your-api-token"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

# Send a chat message
response = requests.post(
    f"{BASE_URL}/v1/chat/send_message",
    headers=headers,
    json={
        "prompt": "What's on my calendar today?",
        "profile_id": "default_assistant"
    }
)
response.raise_for_status()  # Raises exception for 4xx/5xx errors
result = response.json()
print(result["reply"])

# List notes
response = requests.get(f"{BASE_URL}/notes/", headers=headers)
response.raise_for_status()
notes = response.json()
for note in notes:
    print(f"- {note['title']}")

# Search documents
response = requests.post(
    f"{BASE_URL}/vector-search/",
    headers=headers,
    json={
        "query_text": "meeting notes",
        "limit": 5
    }
)
response.raise_for_status()
results = response.json()
for item in results:
    print(f"{item['document']['title']}: {item['score']:.2f}")
```

> **Note**: Use `response.raise_for_status()` to check for HTTP errors. See
> [Error Responses](#error-responses) for the error format.

### JavaScript/Fetch

```javascript
const BASE_URL = "https://your-domain.com/api";
const TOKEN = "your-api-token";

const headers = {
  Authorization: `Bearer ${TOKEN}`,
  "Content-Type": "application/json",
};

// Send a chat message
async function sendMessage(prompt) {
  const response = await fetch(`${BASE_URL}/v1/chat/send_message`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      prompt,
      profile_id: "default_assistant",
    }),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(`API error ${response.status}: ${error.detail}`);
  }
  const data = await response.json();
  return data.reply;
}

// Resumable streaming chat (two-step: start a turn, then watch the stream).
// crypto.randomUUID() supplies the idempotency turn_id.
async function streamChat(prompt, onChunk) {
  const turnId = crypto.randomUUID();
  const startResp = await fetch(`${BASE_URL}/v1/chat/turns`, {
    method: "POST",
    headers,
    body: JSON.stringify({ turn_id: turnId, prompt }),
  });
  if (!startResp.ok) {
    const error = await startResp.json();
    throw new Error(`API error ${startResp.status}: ${error.detail}`);
  }
  const { conversation_id, first_seq, already_complete } = await startResp.json();

  // The turn finished durably (e.g. after a restart) and is not replayable from
  // the hub — reload history instead of opening the stream.
  if (already_complete) return { conversation_id, alreadyComplete: true };

  // follow=false: drain the buffer and stream the in-flight turn through its
  // turn_ended frame, then close. Resume after a disconnect by re-requesting
  // with from_seq set to the highest seq already applied.
  const response = await fetch(
    `${BASE_URL}/v1/chat/conversations/${conversation_id}/stream?from_seq=${first_seq}`,
    { headers },
  );
  if (!response.ok) {
    const error = await response.json();
    throw new Error(`API error ${response.status}: ${error.detail}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const text = decoder.decode(value);
    const lines = text.split("\n");

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const data = JSON.parse(line.slice(6));
        onChunk(data);
      }
    }
  }

  // Mark the turn delivered so the server suppresses the disconnect-push.
  // Use the highest seq applied from the turn_ended frame as ack_seq.
  return { conversation_id };
}

// List notes
async function listNotes() {
  const response = await fetch(`${BASE_URL}/notes/`, { headers });
  return response.json();
}
```

### cURL

```bash
# Set common variables
export API_URL="https://your-domain.com/api"
export TOKEN="your-api-token"

# Send a chat message
curl -X POST "$API_URL/v1/chat/send_message" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello, what can you help me with?"}'

# Create a note
curl -X POST "$API_URL/notes/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "API Test", "content": "Created via API", "include_in_prompt": true}'

# Search documents
curl -X POST "$API_URL/vector-search/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query_text": "important documents", "limit": 10}'

# Upload a document
curl -X POST "$API_URL/documents/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "source_type=manual_upload" \
  -F "source_id=my-doc-001" \
  -F "source_uri=file://notes.pdf" \
  -F "title=My Notes" \
  -F "uploaded_file=@notes.pdf"
```

## Related Resources

- **Interactive API Docs**: `/api/docs` (Swagger UI)
- **Alternative API Docs**: `/api/redoc` (ReDoc)
- [Web Development Guide](../../src/family_assistant/web/CLAUDE.md) - Internal web layer
  documentation
- [Tool Development Guide](../../src/family_assistant/tools/CLAUDE.md) - Creating custom tools

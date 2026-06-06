# iOS Push Notifications (APNs) — Server Side

## Overview

Family Assistant already supports browser Web Push (PWA) notifications via VAPID. This document
describes the server-side integration for **native iOS push notifications** delivered through Apple
Push Notification service (APNs).

The two channels are unified behind a single `NotificationDispatcher` so callers send one logical
notification that fans out to every channel a user is subscribed to (Web Push and/or iOS).

## Components

### Storage: `ios_push_tokens`

A new table stores APNs device tokens registered by authenticated clients.

| Column            | Type        | Notes                                           |
| ----------------- | ----------- | ----------------------------------------------- |
| `id`              | Integer PK  |                                                 |
| `device_token`    | String(255) | Hex APNs device token, globally unique          |
| `user_identifier` | String(255) | Canonical user id (indexed)                     |
| `environment`     | String(20)  | `production` or `sandbox` (APNs host selection) |
| `bundle_id`       | String(255) | Optional app bundle id reported by the client   |
| `created_at`      | DateTime    | server default `now()`                          |
| `updated_at`      | DateTime    | refreshed on re-registration                    |

A device token is unique to a device/app install, so registration is an **upsert** keyed on
`device_token`: re-registering moves the token to the current user and refreshes `environment` and
`bundle_id`.

### APNs sender: `APNsService`

- Authenticates with an Apple `.p8` APNs auth key using **provider token (JWT)** auth.
- Signs ES256 JWTs with the configured **Team ID** (`iss`) and **Key ID** (`kid` header). Tokens are
  cached and refreshed well within Apple's 20–60 minute validity window.
- Sends HTTP/2 requests to APNs (`api.push.apple.com` for production, `api.sandbox.push.apple.com`
  for sandbox) using `httpx` with `http2=True`.
- Sets `apns-topic` to the app bundle id and `apns-push-type: alert`.
- Payload is a standard alert: `{"aps": {"alert": {"title", "body"}, "sound": "default"}}`.

#### Error handling

- **`410` / reason `Unregistered` / `ExpiredToken`** → delete the stored token.
- **`400` reason `BadDeviceToken`** → likely a sandbox/production mismatch. Retry once against the
  other APNs environment; on success, persist the corrected `environment`. The token is only deleted
  if the retry is *also* conclusively invalid (`BadDeviceToken`/`Unregistered`/410); a transient
  retry failure (5xx, 429, provider-token error, local `httpx` error) leaves the token in place for
  the next send.
- **`403` reason `ExpiredProviderToken` / `InvalidProviderToken`** → invalidate the cached JWT and
  retry once.
- All non-success responses log the APNs `reason` for debugging.

### `Notifier` protocol and `NotificationDispatcher`

All notification channels implement an explicit `Notifier` protocol (`enabled` +
`send_notification(user_identifier, title, body, db_context)`): the Web Push service, the APNs
service, and the `NotificationDispatcher`. Consumers (`WebChatInterface`, `ConfirmationService`,
`TaskWorker`, the worker webhook) depend on `Notifier` rather than a concrete service, so delivery
is a type-checked contract rather than structural duck typing.

`NotificationDispatcher` is a thin facade holding the optional Web Push and APNs services. It fans
out to every enabled channel concurrently and isolates per-channel failures. In production it is the
`Notifier` injected at every send point; the bare services are injected directly only in narrower
contexts.

The shared
`notify_conversation(notifier, db_context, *, interface_type, conversation_id, title, body)` helper
resolves the owning user from conversation history and dispatches, returning whether a notification
was sent. It is the public seam exercised by tests.

## Endpoints

Both require authentication (`get_current_user`), matching the existing Web Push endpoints.

- `POST /api/ios/push-tokens` — register/refresh a device token. Body:
  `{ "device_token": str, "environment": "production"|"sandbox", "bundle_id": str | null }`.
- `DELETE /api/ios/push-tokens/{device_token}` — unregister a device token for the current user.

## Send points

A notification is dispatched at four points. For points that only carry a conversation context, the
owning user is resolved from the most recent user message in that conversation (the same approach
`WebChatInterface` already uses), extracted into a shared `resolve_conversation_user()` helper.

1. **Assistant reply saved to a web conversation** — existing `WebChatInterface.send_message`, now
   driven by the dispatcher.
2. **Pending confirmation created** — `ConfirmationService.create_request` notifies the
   `target_user_id` (already a canonical user id).
3. **Automation / task failure** — `TaskWorker` notifies the conversation owner when a task is
   marked failed after exhausting retries.
4. **Worker task complete** — the worker completion webhook notifies the conversation owner when a
   spawned worker finishes.

## Configuration

`apns` config section (all optional; service is disabled unless fully configured):

| Field           | Env var              | Notes                                |
| --------------- | -------------------- | ------------------------------------ |
| `team_id`       | `APNS_TEAM_ID`       | Apple Developer Team ID (JWT `iss`)  |
| `key_id`        | `APNS_KEY_ID`        | APNs auth key id (JWT `kid`)         |
| `auth_key`      | `APNS_AUTH_KEY`      | `.p8` private key PEM contents       |
| `auth_key_path` | `APNS_AUTH_KEY_PATH` | Path to a `.p8` file (alternative)   |
| `bundle_id`     | `APNS_BUNDLE_ID`     | App bundle id used as `apns-topic`   |
| `use_sandbox`   | `APNS_USE_SANDBOX`   | Default environment when unspecified |

`auth_key` (and any loaded `.p8` contents) are treated as secrets and redacted from logged config.

## Testing strategy

- Storage and config: real test database + config loader round-trips.
- `APNsService`: an injected `httpx` client backed by `httpx.MockTransport` simulates APNs
  responses, allowing deterministic tests of success, `Unregistered` deletion, sandbox/production
  retry, and JWT refresh without contacting Apple.
- Endpoints: functional tests through the FastAPI app, mirroring `test_push_api.py`.

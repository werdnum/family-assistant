# Configuration Reference

This document provides a comprehensive reference for all environment variables and configuration
options in Family Assistant.

## Configuration Hierarchy

Configuration is loaded in the following order (later sources override earlier ones):

1. **Code Defaults** - Built-in defaults in `__main__.py`
2. **config.yaml** - Main configuration file
3. **Environment Variables** - Runtime overrides (highest priority)
4. **CLI Arguments** - Command-line overrides (highest priority for supported options)

Environment variables can be set directly or loaded from a `.env` file.

______________________________________________________________________

## Core Configuration

### DATABASE_URL

Database connection string for the application.

| Property  | Value                                                            |
| --------- | ---------------------------------------------------------------- |
| Required  | No                                                               |
| Default   | `sqlite+aiosqlite:///family_assistant.db`                        |
| Sensitive | Yes (may contain credentials)                                    |
| Example   | `postgresql+asyncpg://user:pass@localhost:5432/family_assistant` |

Supports SQLite (for development) and PostgreSQL (for production). PostgreSQL is recommended for
production use with pgvector extension for vector search.

______________________________________________________________________

### SERVER_URL

Base URL of the running server, used for generating links and webhooks.

| Property  | Value                           |
| --------- | ------------------------------- |
| Required  | No                              |
| Default   | `http://localhost:8000`         |
| Sensitive | No                              |
| Example   | `https://assistant.example.com` |

______________________________________________________________________

### TIMEZONE

Default timezone for date/time operations.

| Property  | Value                                     |
| --------- | ----------------------------------------- |
| Required  | No                                        |
| Default   | `UTC` (or as configured in `config.yaml`) |
| Sensitive | No                                        |
| Example   | `Australia/Sydney`                        |

Uses IANA timezone database identifiers.

______________________________________________________________________

### DEV_MODE

Enable development mode features.

| Property  | Value   |
| --------- | ------- |
| Required  | No      |
| Default   | `false` |
| Sensitive | No      |
| Example   | `true`  |

Enables development-specific features like hot reloading and debug endpoints.

______________________________________________________________________

## Storage Paths

### DOCUMENT_STORAGE_PATH

Directory for storing uploaded documents.

| Property  | Value                                 |
| --------- | ------------------------------------- |
| Required  | No                                    |
| Default   | `/mnt/data/files`                     |
| Sensitive | No                                    |
| Example   | `/var/lib/family-assistant/documents` |

______________________________________________________________________

### ATTACHMENT_STORAGE_PATH

Directory for storing email attachments.

| Property  | Value                                   |
| --------- | --------------------------------------- |
| Required  | No                                      |
| Default   | `/mnt/data/mailbox/attachments`         |
| Sensitive | No                                      |
| Example   | `/var/lib/family-assistant/attachments` |

______________________________________________________________________

### CHAT_ATTACHMENT_STORAGE_PATH

Directory for storing chat message attachments.

| Property  | Value                                        |
| --------- | -------------------------------------------- |
| Required  | Yes (for production)                         |
| Default   | `/tmp/chat_attachments`                      |
| Sensitive | No                                           |
| Example   | `/var/lib/family-assistant/chat-attachments` |

> **⚠️ WARNING**: The default `/tmp/chat_attachments` is for development only. Files in `/tmp` may
> be deleted on reboot. Configure a persistent path for production.

______________________________________________________________________

### DOCS_USER_DIR

Directory containing user documentation files.

| Property  | Value                                       |
| --------- | ------------------------------------------- |
| Required  | No                                          |
| Default   | `docs/user` (or `/app/docs/user` in Docker) |
| Sensitive | No                                          |
| Example   | `/app/docs/user`                            |

______________________________________________________________________

## User Identities

Use top-level `users` to map each human to a canonical application user id across web/OIDC,
Telegram, and email intake. This is the id stored on durable confirmations and other user-scoped
records.

```yaml
users:
  - id: "alice@example.com"
    label: "Alice"
    oidc:
      emails:
        - "alice@example.com"
      subjects: []
    telegram:
      user_ids:
        - 123456789
      developer: true
    email_intake:
      sender_addresses:
        - "alice@gmail.com"
      recipient_addresses:
        - "assistant+alice@mg.example.com"
```

Use the stable Keycloak/OIDC email as `id` unless you have a stronger local convention. Telegram
`user_ids` are Telegram user IDs, not chat IDs.

The optional `label` is the human-friendly display name the assistant uses to address the user (for
example in the web chat). When a web/OIDC or API-token user has a `label`, the assistant uses it
instead of a generic placeholder; if it is omitted the assistant falls back to the OIDC display name
and then the canonical `id`.

When `users` is configured, unknown OIDC users, Telegram users, and required email mappings are
rejected at the interface boundary. When it is empty, the app keeps the legacy behavior:
`allowed_user_ids`, `developer_chat_id`, and `email_intake.user_mappings`.

If you do not want Telegram identifiers in `config.yaml`, keep the non-sensitive user shape in YAML
and inject sensitive identity details with `USER_IDENTITIES_FILE`:

```yaml
users:
  - id: "alice@example.com"
    oidc:
      emails:
        - "alice@example.com"
    email_intake:
      sender_addresses:
        - "alice@gmail.com"
```

```yaml
# /run/secrets/family-assistant-users.yaml
users:
  - id: "alice@example.com"
    telegram:
      user_ids:
        - 123456789
      developer: true
```

```bash
USER_IDENTITIES_FILE=/run/secrets/family-assistant-users.yaml
CHAT_ID_TO_NAME_MAP='123456789:Alice'
```

The file can contain either a top-level `users:` list or a bare user list. Entries are merged into
`config.yaml` by `id`, so secret-backed files can add only the sensitive fields. This is useful for
GitOps deployments that keep numeric Telegram IDs and display names in a Kubernetes Secret or
SealedSecret while leaving email/OIDC mappings editable in the normal config.

### Migration From Legacy Identity Settings

| Legacy field                                       | New field                                  |
| -------------------------------------------------- | ------------------------------------------ |
| `allowed_user_ids`                                 | `users[].telegram.user_ids`                |
| `developer_chat_id`                                | `users[].telegram.developer: true`         |
| `email_intake.user_mappings[].user_id`             | `users[].id`                               |
| `email_intake.user_mappings[].sender_addresses`    | `users[].email_intake.sender_addresses`    |
| `email_intake.user_mappings[].recipient_addresses` | `users[].email_intake.recipient_addresses` |

`email_intake.allowed_sender_addresses` and `allowed_recipient_addresses` are still security
allowlists. They answer "which inbound addresses are accepted at all"; `users[].email_intake`
answers "which canonical user owns this accepted email".

______________________________________________________________________

## Email Intake Security

The `/webhook/mail` endpoint accepts Mailgun inbound email webhooks. Use this configuration when
Mailgun routes are enabled, especially before allowing any assistant action from forwarded email.

```yaml
email_intake:
  mailgun_webhook_signing_key: "your-mailgun-http-webhook-signing-key"
  mailgun_signature_max_age_seconds: 300

  allowed_sender_addresses:
    - "alice@gmail.com"
    - "bob@gmail.com"

  allowed_recipient_addresses:
    - "assistant-intake@mg.example.com"
    - "assistant+alice@mg.example.com"
    - "assistant+bob@mg.example.com"

  require_authenticated_sender: true

  require_user_mapping: true

  enable_actions: true
  action_profile_id: "email_intake"

  outbound_mailgun_domain: "mg.example.com"
  outbound_from_address: "Family Assistant <assistant@mg.example.com>"

  max_raw_request_bytes: 26214400
  max_attachment_bytes: 10485760
  max_total_attachment_bytes: 26214400
```

Set secret values with environment variables where possible:

```bash
MAILGUN_WEBHOOK_SIGNING_KEY=your-mailgun-http-webhook-signing-key
MAILGUN_OUTBOUND_API_KEY=your-mailgun-messages-api-key
EMAIL_INTAKE_ENABLE_ACTIONS=true
```

### mailgun_webhook_signing_key

Mailgun HTTP webhook signing key for the receiving domain. Set it from the
`MAILGUN_WEBHOOK_SIGNING_KEY` environment variable; find the value in the Mailgun dashboard under
Sending → Webhooks.

| Property  | Value                                   |
| --------- | --------------------------------------- |
| Required  | Yes when any email intake policy is set |
| Default   | `null`                                  |
| Sensitive | **Yes**                                 |
| Example   | `0123456789abcdef0123456789abcdef`      |

If this key is unset, the app skips Mailgun signature verification only for permissive indexing-only
deployments. If sender allowlists, recipient allowlists, sender authentication, or user mapping are
enabled, the webhook fails closed until the signing key is configured. This prevents direct HTTP
clients from forging Mailgun form fields such as `sender`, `recipient`, `dmarc`, `spf`, and `dkim`.

### allowed_sender_addresses

Authorized email accounts that may forward messages into the assistant.

These are the SMTP envelope senders reported by Mailgun in the `sender` form field. In the common
CUJ where Alice forwards an order confirmation from Gmail, this should contain Alice's Gmail
address, not the merchant's address from the original forwarded message.

| Property  | Value                        |
| --------- | ---------------------------- |
| Required  | Recommended for email intake |
| Default   | `[]` (unrestricted)          |
| Sensitive | No                           |
| Example   | `["alice@gmail.com"]`        |

Use concrete lowercase addresses. If this list is non-empty, the Mailgun signing key must also be
set.

### allowed_recipient_addresses

Mailgun inbound addresses or aliases that this app should accept.

These are the recipient addresses Mailgun reports in the `recipient` form field. They are addresses
at your Mailgun receiving domain, not the order vendor's email address and not the human user's
normal Gmail address unless Gmail is also the Mailgun recipient. Examples:

- Shared intake mailbox: `assistant-intake@mg.example.com`
- Per-user aliases: `assistant+alice@mg.example.com`, `assistant+bob@mg.example.com`
- Per-user local parts: `alice@mg.example.com`, `bob@mg.example.com`

Mailgun can route a catch-all or regex recipient pattern to the same `/webhook/mail` endpoint, while
the app uses `allowed_recipient_addresses` to reject unexpected recipients. Per-user recipient
aliases add friction because operators need to provision or communicate the aliases, but they give a
clean way to distinguish which user intended the email intake path.

| Property  | Value                                     |
| --------- | ----------------------------------------- |
| Required  | Recommended for production Mailgun routes |
| Default   | `[]` (unrestricted)                       |
| Sensitive | No                                        |
| Example   | `["assistant+alice@mg.example.com"]`      |

If this list is non-empty, the Mailgun signing key must also be set.

### User Mapping

`users[].email_intake` resolves accepted inbound email to a canonical application user id. The
resolved value is stored on the received email as `target_user_id`, so later action-processing and
confirmation flows can deterministically choose the right user context.

Mappings can match by authorized forwarding sender, by Mailgun recipient alias, or by both:

```yaml
users:
  - id: "alice@example.com"
    oidc:
      emails: ["alice@example.com"]
    email_intake:
      sender_addresses: ["alice@gmail.com"]
      recipient_addresses: ["assistant+alice@mg.example.com"]

email_intake:
  require_user_mapping: true
```

When `require_user_mapping` is true, accepted email must map to exactly one configured user. If no
mapping matches, the webhook returns `401`. If sender and recipient mappings point to different
users, the webhook also returns `401` rather than guessing.

For the least operational friction, map each user's stable forwarding address in `sender_addresses`.
For stronger routing and clearer confirmation behavior, also give each user a Mailgun inbound alias
and include it in `recipient_addresses`.

| Option                 | Default | Recommended for actions |
| ---------------------- | ------- | ----------------------- |
| `require_user_mapping` | `false` | `true`                  |
| `users[].email_intake` | `[]`    | One entry per user      |

If `require_user_mapping` is true or `users[].email_intake` is non-empty, the Mailgun signing key
must also be set.

### Action Processing

`enable_actions` controls whether accepted, mapped inbound emails become assistant turns. When it is
false, the webhook stores and indexes email only. When it is true, the webhook enqueues an
`email_intake_action` task for the restricted `action_profile_id` profile.

Action-capable email intake should use all of these controls together:

| Option                         | Recommended    |
| ------------------------------ | -------------- |
| `mailgun_webhook_signing_key`  | Set            |
| `allowed_sender_addresses`     | Set            |
| `allowed_recipient_addresses`  | Set            |
| `require_authenticated_sender` | `true`         |
| `require_user_mapping`         | `true`         |
| `enable_actions`               | `true`         |
| `action_profile_id`            | `email_intake` |

The built-in `email_intake` profile treats email body text, forwarded content, HTML, links, and
attachments as untrusted evidence. Its policy allows bounded reads, blocks destructive/code/browser/
delegation/worker/automation tools, and requires durable confirmation for calendar writes, note
writes, reminders, callbacks, and messages to known users. Confirmation is surfaced through trusted
interfaces such as Telegram or Web, not by trusting the email body.

### Outbound Email Replies

Outbound email support is reply-only. The email interface resolves `email:{received_email_id}` back
to the stored inbound email and sends text only to the envelope sender address from that row, and
only when that sender address is explicitly mapped to the resolved user in
`users[].email_intake.sender_addresses`. Recipient-only mappings can route an inbound email to a
user for storage and confirmation creation, but they do not authorize emailing assistant responses
back to arbitrary external senders. This is intentional: the email intake profile may use bounded
read tools, so outbound replies require a per-user sender mapping to avoid data exfiltration. It is
not a general-purpose arbitrary-recipient email tool.

| Option                     | Default        | Environment variable             | Sensitive |
| -------------------------- | -------------- | -------------------------------- | --------- |
| `outbound_mailgun_api_key` | `null`         | `MAILGUN_OUTBOUND_API_KEY`       | Yes       |
| `outbound_mailgun_domain`  | `null`         | `MAILGUN_OUTBOUND_DOMAIN`        | No        |
| `outbound_from_address`    | `null`         | `EMAIL_OUTBOUND_FROM_ADDRESS`    | No        |
| `outbound_timeout_seconds` | `10.0`         | None                             | No        |
| `enable_actions`           | `false`        | `EMAIL_INTAKE_ENABLE_ACTIONS`    | No        |
| `action_profile_id`        | `email_intake` | `EMAIL_INTAKE_ACTION_PROFILE_ID` | No        |

If outbound Mailgun settings are absent, or the inbound sender is not mapped in
`users[].email_intake.sender_addresses` for the resolved user, email action processing can still
create confirmations and store conversation history, but final email replies are not delivered.

### Sender Authentication Policy

`require_authenticated_sender` makes the webhook require a DMARC `pass` result that the app computes
locally from the raw MIME message. The app verifies DKIM signatures and evaluates DMARC alignment
with [`authheaders`](https://pypi.org/project/authheaders/) and
[`dkimpy`](https://pypi.org/project/dkimpy/); it no longer trusts Mailgun's `dmarc`, `SPF`, or
`Dkim` form fields or the embedded `Authentication-Results` header.

Because local DKIM verification needs the byte-exact raw message, Mailgun must be configured to
forward the MIME payload rather than the parsed representation. Mailgun only includes the raw
message in the `body-mime` form field when the route's Destination URL path ends in `mime` or
`raw-mime`; otherwise it sends parsed `body-plain`/`body-html` fields instead. The app therefore
exposes the same webhook at both `/webhook/mail` and `/webhook/mail/mime`. Point the Mailgun route
at the `/webhook/mail/mime` URL so Mailgun triggers raw-MIME forwarding. When
`require_authenticated_sender` is true, requests without `body-mime` fail closed with `401`. DMARC
failures always fail closed.

| Option                         | Default | Recommended |
| ------------------------------ | ------- | ----------- |
| `require_authenticated_sender` | `false` | `true`      |

If `require_authenticated_sender` is true, the Mailgun signing key must also be set.

### Size Limits

Application-level payload limits for inbound email.

| Option                       | Default    | Meaning                                 |
| ---------------------------- | ---------- | --------------------------------------- |
| `max_raw_request_bytes`      | `26214400` | Full webhook request body limit         |
| `max_attachment_bytes`       | `10485760` | Per-attachment limit                    |
| `max_total_attachment_bytes` | `26214400` | Total attachments for one inbound email |

The webhook checks `Content-Length` before buffering the request body when the header is present,
and also checks the actual buffered body length.

______________________________________________________________________

## Authentication - Telegram

### TELEGRAM_BOT_TOKEN

Telegram Bot API token from BotFather.

| Property  | Value                                           |
| --------- | ----------------------------------------------- |
| Required  | Yes (for Telegram integration)                  |
| Default   | None                                            |
| Sensitive | **Yes**                                         |
| Example   | `123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789` |

Obtain from [@BotFather](https://t.me/botfather) on Telegram.

______________________________________________________________________

### ALLOWED_USER_IDS

Comma-separated list of Telegram user IDs allowed to interact with the bot.

| Property  | Value                  |
| --------- | ---------------------- |
| Required  | **Yes** (for security) |
| Default   | Empty                  |
| Sensitive | No                     |
| Example   | `100000001,123456789`  |

> **⚠️ SECURITY WARNING**: If this is empty or unset, the bot will accept messages from **any
> Telegram user**. Always set this in production to restrict access to authorized users only.

Also accepts `ALLOWED_CHAT_IDS` as an alias.

______________________________________________________________________

### DEVELOPER_CHAT_ID

Telegram chat ID for receiving error notifications and system alerts.

| Property  | Value       |
| --------- | ----------- |
| Required  | No          |
| Default   | None        |
| Sensitive | No          |
| Example   | `100000001` |

______________________________________________________________________

### CHAT_ID_TO_NAME_MAP

Mapping of Telegram chat IDs to display names.

| Property  | Value               |
| --------- | ------------------- |
| Required  | No                  |
| Default   | Empty               |
| Sensitive | No                  |
| Example   | `123:Alice,456:Bob` |

Format: comma-separated `chat_id:name` pairs.

______________________________________________________________________

### USER_IDENTITIES_FILE

Path to a YAML file containing user identity entries to merge into top-level `users`.

| Property  | Value                                                    |
| --------- | -------------------------------------------------------- |
| Required  | No                                                       |
| Default   | Unset                                                    |
| Sensitive | Yes, if the file contains sensitive identity identifiers |
| Example   | `/run/secrets/family-assistant-users.yaml`               |

The file can contain either:

```yaml
users:
  - id: "alice@example.com"
    telegram:
      user_ids: [123]
```

or:

```yaml
- id: "alice@example.com"
  telegram:
    user_ids: [123]
```

Entries are merged with configured users by `id`. Nested objects are deep-merged; lists follow
normal YAML config semantics and are replaced by the overlay value.

______________________________________________________________________

## AI/LLM Services

### GEMINI_API_KEY

Google Gemini API key for Google AI models.

| Property  | Value                     |
| --------- | ------------------------- |
| Required  | Yes (for Google provider) |
| Default   | None                      |
| Sensitive | **Yes**                   |
| Example   | `AIzaSy...`               |

Required when using `provider: "google"` in service profiles or for video generation.

______________________________________________________________________

### OPENAI_API_KEY

OpenAI API key for GPT models.

| Property  | Value                     |
| --------- | ------------------------- |
| Required  | Yes (for OpenAI provider) |
| Default   | None                      |
| Sensitive | **Yes**                   |
| Example   | `sk-...`                  |

Required when using `provider: "openai"` in service profiles.

______________________________________________________________________

### OPENROUTER_API_KEY

OpenRouter API key for accessing multiple LLM providers.

| Property  | Value                       |
| --------- | --------------------------- |
| Required  | Yes (for OpenRouter models) |
| Default   | None                        |
| Sensitive | **Yes**                     |
| Example   | `sk-or-v1-...`              |

Used when model names start with `openrouter/`.

______________________________________________________________________

### ANTHROPIC_API_KEY

Anthropic API key for Claude models.

| Property  | Value                        |
| --------- | ---------------------------- |
| Required  | Yes (for Anthropic provider) |
| Default   | None                         |
| Sensitive | **Yes**                      |
| Example   | `sk-ant-...`                 |

Required when using `provider: "anthropic"` in service profiles.

______________________________________________________________________

### LLM_MODEL

Default LLM model identifier.

| Property  | Value                               |
| --------- | ----------------------------------- |
| Required  | No                                  |
| Default   | `gemini/gemini-2.5-pro`             |
| Sensitive | No                                  |
| Example   | `gpt-4o`, `anthropic/claude-3-opus` |

______________________________________________________________________

### EMBEDDING_MODEL

Embedding model for vector search.

| Property  | Value                         |
| --------- | ----------------------------- |
| Required  | No                            |
| Default   | `gemini/gemini-embedding-001` |
| Sensitive | No                            |
| Example   | `text-embedding-3-large`      |

When `EMBEDDING_PROVIDER` is `openai`, this value is sent to the API verbatim.

______________________________________________________________________

### EMBEDDING_PROVIDER

Selects the embedding generator implementation.

| Property  | Value                           |
| --------- | ------------------------------- |
| Required  | No                              |
| Default   | Inferred from `EMBEDDING_MODEL` |
| Sensitive | No                              |
| Example   | `openai`                        |

Leave it unset to infer the provider from the model name: a `gemini/<model>` prefix selects Google
Gemini, a model name starting with `/` is treated as a path to a local sentence-transformer model,
and `mock-deterministic-embedder` selects the deterministic test embedder.

Set it to `openai` to use any OpenAI-compatible embeddings endpoint — the official OpenAI API,
OpenRouter, or a self-hosted inference server.

`openai` is currently the only value that actually changes the selection. The field also accepts
`gemini` and `sentence_transformer`, but nothing branches on them, so those fall through to the
model-name inference above — setting `sentence_transformer` with a plain Hugging Face model name
fails as an unsupported model rather than loading a local one. Use the model name to select those.

______________________________________________________________________

### EMBEDDING_BASE_URL

Base URL of the OpenAI-compatible embeddings endpoint.

| Property  | Value                          |
| --------- | ------------------------------ |
| Required  | No                             |
| Default   | None (official OpenAI API)     |
| Sensitive | No                             |
| Example   | `https://openrouter.ai/api/v1` |

Only used when `EMBEDDING_PROVIDER` is `openai`. Leave it unset to talk to the official OpenAI API.
For OpenRouter, use `https://openrouter.ai/api/v1`.

______________________________________________________________________

### EMBEDDING_API_KEY

API key for the OpenAI-compatible embeddings endpoint.

| Property  | Value                                             |
| --------- | ------------------------------------------------- |
| Required  | No                                                |
| Default   | Falls back to `openai_api_key` / `OPENAI_API_KEY` |
| Sensitive | **Yes**                                           |
| Example   | `sk-or-v1-...`                                    |

Redacted from logged configuration.

______________________________________________________________________

### EMBEDDING_DIMENSIONS

Dimensionality of embedding vectors.

| Property  | Value         |
| --------- | ------------- |
| Required  | No            |
| Default   | `1536`        |
| Sensitive | No            |
| Example   | `768`, `3072` |

Must match the dimensions produced by the configured embedding model, and — when set — it must also
match the dimensionality of the vector storage column.

This value is only forwarded as the `dimensions` request parameter when it has been explicitly
configured, so the model must actually support that output size (for example OpenAI's
`text-embedding-3-*`). Leave it unset for models that reject the field (for example
`text-embedding-ada-002`) so the model's native size is used.

______________________________________________________________________

### DEBUG_LLM_MESSAGES

Enable detailed logging of LLM message exchanges.

| Property  | Value   |
| --------- | ------- |
| Required  | No      |
| Default   | `false` |
| Sensitive | No      |
| Example   | `true`  |

Useful for debugging prompts and responses.

______________________________________________________________________

## Calendar Integration

### CALDAV_USERNAME

Username for CalDAV authentication.

| Property  | Value                      |
| --------- | -------------------------- |
| Required  | Yes (for CalDAV calendars) |
| Default   | None                       |
| Sensitive | No                         |
| Example   | `user@example.com`         |

______________________________________________________________________

### CALDAV_PASSWORD

Password for CalDAV authentication.

| Property  | Value                      |
| --------- | -------------------------- |
| Required  | Yes (for CalDAV calendars) |
| Default   | None                       |
| Sensitive | **Yes**                    |
| Example   | `app-specific-password`    |

For iCloud, use an app-specific password.

______________________________________________________________________

### CALDAV_CALENDAR_URLS

Comma-separated list of CalDAV calendar URLs.

| Property  | Value                                                |
| --------- | ---------------------------------------------------- |
| Required  | Yes (for CalDAV calendars)                           |
| Default   | None                                                 |
| Sensitive | No                                                   |
| Example   | `https://caldav.icloud.com/1234567/calendars/abc123` |

Direct URLs to individual calendars, not the CalDAV server root.

______________________________________________________________________

### ICAL_URLS

Comma-separated list of public iCalendar (.ics) URLs.

| Property  | Value                                                              |
| --------- | ------------------------------------------------------------------ |
| Required  | No                                                                 |
| Default   | None                                                               |
| Sensitive | No                                                                 |
| Example   | `https://example.com/calendar.ics,https://another.site/events.ics` |

For read-only access to public calendar feeds.

______________________________________________________________________

## Smart Home - Home Assistant

### HOMEASSISTANT_URL

URL to your Home Assistant instance.

| Property  | Value                                |
| --------- | ------------------------------------ |
| Required  | Yes (for Home Assistant integration) |
| Default   | None                                 |
| Sensitive | No                                   |
| Example   | `https://homeassistant.local:8123`   |

______________________________________________________________________

### HOMEASSISTANT_API_KEY

Long-lived access token for Home Assistant API.

| Property  | Value                                     |
| --------- | ----------------------------------------- |
| Required  | Yes (for Home Assistant integration)      |
| Default   | None                                      |
| Sensitive | **Yes**                                   |
| Example   | `eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...` |

Generate from Home Assistant: Profile -> Long-Lived Access Tokens.

______________________________________________________________________

## Push Notifications (VAPID)

### VAPID_PRIVATE_KEY

VAPID private key for signing push notifications.

| Property  | Value                                 |
| --------- | ------------------------------------- |
| Required  | Yes (for push notifications)          |
| Default   | None                                  |
| Sensitive | **Yes**                               |
| Example   | `a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6...` |

Format: Raw key bytes encoded with URL-safe base64, no padding. Generate using:
`python scripts/generate_vapid_keys.py`

______________________________________________________________________

### VAPID_PUBLIC_KEY

VAPID public key for push notification subscriptions.

| Property  | Value             |
| --------- | ----------------- |
| Required  | Yes, for web push |
| Default   | None              |
| Sensitive | No                |
| Example   | `BG1l7...`        |

Same URL-safe base64 encoding as the private key. Nothing derives this from `VAPID_PRIVATE_KEY` at
runtime: `GET /api/client_config` returns the configured value as-is, and the frontend hides its
subscription control when it is absent. Setting only the private key therefore leaves clients unable
to subscribe.

______________________________________________________________________

### VAPID_CONTACT_EMAIL

Admin contact address used as the VAPID `sub` claim.

| Property  | Value                        |
| --------- | ---------------------------- |
| Required  | Yes (for push notifications) |
| Default   | None                         |
| Sensitive | No                           |
| Example   | `mailto:admin@example.com`   |

Push services use this address to contact the deployment administrator, for example when
subscriptions fail.

______________________________________________________________________

## Push Notifications (iOS APNs)

Native iOS push is delivered through Apple Push Notification service using provider-token
authentication with a `.p8` auth key. All of these variables map to the `apns` config section.

The APNs sender is enabled **only** when `APNS_TEAM_ID`, `APNS_KEY_ID`, `APNS_BUNDLE_ID` and a
private key (either `APNS_AUTH_KEY` or `APNS_AUTH_KEY_PATH`) are all configured.

Clients register device tokens via `POST /api/ios/push-tokens` and unregister via
`DELETE /api/ios/push-tokens/{device_token}` (both authenticated). See
[docs/design/ios_push_notifications.md](../design/ios_push_notifications.md) for the full design.

### APNS_TEAM_ID

Apple Developer Team ID, sent as the provider JWT `iss` claim.

| Property  | Value              |
| --------- | ------------------ |
| Required  | Yes (for iOS push) |
| Default   | None               |
| Sensitive | No                 |
| Example   | `A1B2C3D4E5`       |

______________________________________________________________________

### APNS_KEY_ID

APNs auth key id, sent as the provider JWT `kid` header.

| Property  | Value              |
| --------- | ------------------ |
| Required  | Yes (for iOS push) |
| Default   | None               |
| Sensitive | No                 |
| Example   | `ABC123DEFG`       |

______________________________________________________________________

### APNS_AUTH_KEY

Contents of the `.p8` private key, in PEM form.

| Property  | Value                                   |
| --------- | --------------------------------------- |
| Required  | Yes, unless `APNS_AUTH_KEY_PATH` is set |
| Default   | None                                    |
| Sensitive | **Yes**                                 |
| Example   | `-----BEGIN PRIVATE KEY-----\nMIGT...`  |

Redacted from logged configuration.

______________________________________________________________________

### APNS_AUTH_KEY_PATH

Path to a `.p8` auth key file, as an alternative to inlining it in `APNS_AUTH_KEY`.

| Property  | Value                                |
| --------- | ------------------------------------ |
| Required  | Yes, unless `APNS_AUTH_KEY` is set   |
| Default   | None                                 |
| Sensitive | No (the file it points at is)        |
| Example   | `/run/secrets/AuthKey_ABC123DEFG.p8` |

______________________________________________________________________

### APNS_BUNDLE_ID

App bundle id, sent as the `apns-topic` header.

| Property  | Value                         |
| --------- | ----------------------------- |
| Required  | Yes (for iOS push)            |
| Default   | None                          |
| Sensitive | No                            |
| Example   | `com.example.familyassistant` |

______________________________________________________________________

### APNS_USE_SANDBOX

Default APNs environment to use when a registered device token does not specify one.

| Property  | Value                |
| --------- | -------------------- |
| Required  | No                   |
| Default   | `false` (production) |
| Sensitive | No                   |
| Example   | `true`               |

Each device token carries its own `environment`, so this is only the fallback. A sandbox/production
mismatch surfaces as a `BadDeviceToken` rejection and is auto-corrected: the send is retried against
the other environment and, on success, the token's stored `environment` is updated so subsequent
sends go to the right host.

______________________________________________________________________

## Google Integration (Gmail & Drive)

Per-user Gmail and Drive access needs an OAuth client from Google Cloud Console plus a Fernet key
for encrypting refresh tokens at rest. All three secrets must be present; if any is missing the
integration is disabled at startup with an error naming the unmet condition.

See [docs/design/user-scoped-google-data-access.md](../design/user-scoped-google-data-access.md) and
the user-facing [Gmail and Google Drive guide](../user/google-workspace.md).

**OAuth client setup.** Create an OAuth 2.0 client in Google Cloud Console with the redirect URI
pointing at `<your-server>/api/integrations/google/callback`, and add every household member who
will use the feature as a test user on the consent screen. A client in "testing" mode is sufficient
and needs no Google verification for these sensitive scopes — but refresh tokens issued to a
test-mode client expire after **7 days**, which triggers re-authorization notifications for every
user on that cycle. Publishing the client to production mode avoids this; for the Gmail and Drive
scopes that may involve Google's verification process. The design intentionally does not work around
this in code — the right fix is production mode, not silence.

```yaml
google_integration:
  oauth_client_id: ""
  oauth_client_secret: ""
  credential_encryption_key: ""
  scopes:
    - "https://www.googleapis.com/auth/gmail.readonly"
    - "https://www.googleapis.com/auth/gmail.compose"
    - "https://www.googleapis.com/auth/drive.readonly"
    - "https://www.googleapis.com/auth/drive.file"
  require_taint_enforcement: true
```

### GOOGLE_OAUTH_CLIENT_ID

OAuth client id from Google Cloud Console. Maps to `google_integration.oauth_client_id`.

| Property  | Value                                       |
| --------- | ------------------------------------------- |
| Required  | Yes (for the Google integration)            |
| Default   | `""`                                        |
| Sensitive | No                                          |
| Example   | `1234567890-abc.apps.googleusercontent.com` |

______________________________________________________________________

### GOOGLE_OAUTH_CLIENT_SECRET

OAuth client secret from Google Cloud Console. Maps to `google_integration.oauth_client_secret`.

| Property  | Value                            |
| --------- | -------------------------------- |
| Required  | Yes (for the Google integration) |
| Default   | `""`                             |
| Sensitive | **Yes**                          |
| Example   | `GOCSPX-...`                     |

Redacted from logged configuration and from the diagnostics export.

______________________________________________________________________

### CREDENTIAL_ENCRYPTION_KEY

URL-safe base64-encoded Fernet key used to encrypt stored refresh tokens at rest. Maps to
`google_integration.credential_encryption_key`.

| Property  | Value                              |
| --------- | ---------------------------------- |
| Required  | Yes (for the Google integration)   |
| Default   | `""`                               |
| Sensitive | **Yes**                            |
| Example   | `dGhpcy1pcy1ub3QtYS1yZWFsLWtleQ==` |

Redacted from logged configuration and from the diagnostics export. Generate a key with:

```bash
python -c "from family_assistant.services.credential_encryption import generate_key; print(generate_key())"
```

Keep this key stable across deployments. A decryption failure (wrong or changed key) is treated as a
configuration error: the stored connection row is left untouched, so restoring the correct key
restores access without any user re-authorization.

______________________________________________________________________

### google_integration.scopes

Allowlist of Google OAuth data scopes requested at consent.

| Property  | Value                                                             |
| --------- | ----------------------------------------------------------------- |
| Required  | No                                                                |
| Default   | `gmail.readonly`, `gmail.compose`, `drive.readonly`, `drive.file` |
| Sensitive | No                                                                |
| Example   | `["https://www.googleapis.com/auth/gmail.readonly"]`              |

The `scopes` list *narrows* the grant — remove the Drive scopes to get a Gmail-only integration, for
example. Only scopes used by shipped deterministic tools are allowed; adding an unsupported scope
(`gmail.send`, `gmail.modify`, full `drive`, etc.) disables the integration with a startup error.
The identity scopes `openid` and `email` are always appended in code and are not configurable.

Tool registration follows the configured scopes:

| Tool                                                        | Required scope                                |
| ----------------------------------------------------------- | --------------------------------------------- |
| `gmail_search`, `gmail_get_message`, `gmail_get_attachment` | `gmail.readonly`                              |
| `gmail_create_draft`                                        | `gmail.compose`                               |
| `drive_search`                                              | `drive.readonly` or `drive.metadata.readonly` |
| `drive_get_file`                                            | `drive.readonly`                              |
| `drive_write_file`                                          | `drive.file`                                  |

The draft tool never sends email, although Google's `gmail.compose` scope itself also authorizes
sending. Drive writes are deterministically confined to the app-created Family Assistant folder and
app-marked files within it.

**Enablement conditions** (validated at startup):

1. The three secrets above are set.
2. The `scopes` list passes allowlist validation.
3. Real web authentication is active (OIDC configured, `users` block resolution in effect) — the
   development `test_user` mode shares one identity across all callers and therefore refuses to
   enable this feature.

______________________________________________________________________

### google_integration.require_taint_enforcement

Whether the Gmail/Drive tools require taint enforcement before they register.

| Property  | Value   |
| --------- | ------- |
| Required  | No      |
| Default   | `true`  |
| Sensitive | No      |
| Example   | `false` |

When `true`, the tools only register if `taint_policy.mode` is `enforce` **and** the effective
policy matrix floors the key exfiltration sinks — `arbitrary_external_message`,
`attacker_addressable_egress`, `sandbox_network`, and `sensitive_read_broadening` — at `confirm` for
untrusted content. If the check fails, the tools are not registered and the integration status
endpoint reports the unmet condition.

Setting it to `false` waives the check (logged at startup and surfaced on the status endpoint) and
the tools register regardless of taint mode.

Two sink classes are deliberately softer at `unknown_external` in the shipped matrix and are not
part of the floor check:

- **`home_local`** (Home Assistant actions). If the deployment drives high-consequence actuators —
  locks, garage doors, alarms — consider raising this sink to `confirm` at `unknown_external` via
  `taint_policy.matrix_overrides` or `operator_minimum`.
- **`artifact_write`** (note and calendar writes). Destructive mutations such as `delete_note` and
  `delete_calendar_event` resolve here and run unconfirmed at `unknown_external` by default.
  Provenance stamping stops injected instructions laundering themselves into trusted storage, but
  raise `artifact_write` to `confirm` via `matrix_overrides` if you want deletions confirmed once
  any external content has been read.

______________________________________________________________________

## Message History Taint Epoch

### taint_policy.history_taint_epoch

Optional timestamp granting a read-time amnesty to legacy message-history taint metadata.

| Property  | Value                         |
| --------- | ----------------------------- |
| Required  | No                            |
| Default   | `null`                        |
| Sensitive | No                            |
| Example   | `"2026-08-01T00:00:00+00:00"` |

Must be a timezone-aware ISO-8601 timestamp — quote it in YAML, since naive or unparseable values
fail startup. This is a **deployment-level** setting only; profiles cannot set it.

- Rows persisted **before** the epoch contribute taint only from explicitly attributed sources: the
  synthetic `legacy_missing_taint_metadata` fallback and anonymous escalation artifacts are ignored,
  and the row's tier is recomputed from what remains.
- Rows **at or after** the epoch are trusted as recorded. A post-epoch row missing metadata logs an
  `ERROR` as a write-path regression alarm, and anonymous post-epoch artifacts are read
  conservatively.
- One filter is timestamp-independent: sources carrying the `legacy_missing_taint_metadata` label
  *inside* persisted metadata are second-hand echoes of another row's read-time fallback, so they
  are dropped and the tier recomputed even for post-epoch rows. First-hand missing-metadata rows
  still escalate and fire the `ERROR` alarm.

> **⚠️ Set the epoch to the instant this feature is DEPLOYED (or later), never earlier** — in
> particular, not the taint-metadata migration date `2026-07-06`. Rows written before deploy may
> contain re-baked poison that the echo filter only partially neutralizes, so an earlier epoch would
> keep re-seeding it.

`GET /api/diagnostics/taint-audit` reports the configured epoch and the pre/post-epoch row splits,
so the poison collapse can be verified before switching `taint_policy.mode` to `enforce`.

See [docs/design/taint-history-epoch-amnesty.md](../design/taint-history-epoch-amnesty.md).

______________________________________________________________________

## Shopping (Universal Commerce Protocol)

`ucp_config` publishes this deployment's own UCP platform profile at `/.well-known/ucp` and holds
the signing material the shopping tools need. Checkout handoff requires signed UCP requests: set
`UCP_SIGNING_KEY_ID` plus either `UCP_SIGNING_PRIVATE_KEY` or `UCP_SIGNING_PRIVATE_KEY_PATH` with an
EC P-256 or P-384 private key. Prefer the path form with a mounted secret. Without them, discovery
and cart building still work but checkout handoff fails with an error naming the missing variables.

See `ucp_config` in `defaults.yaml` for the full key set, and
[docs/design/ucp-client-prerequisites.md](../design/ucp-client-prerequisites.md) for the client
requirements. The user-facing behaviour is described in [Shopping](../user/shopping.md).

______________________________________________________________________

## Diagnostics Access

### DIAGNOSTICS_READONLY_TOKEN

Optional shared secret granting read-only access to the diagnostics endpoints without a user session
or API token — intended for an external monitor that only pulls diagnostics.

| Property  | Value                  |
| --------- | ---------------------- |
| Required  | No                     |
| Default   | Unset (path disabled)  |
| Sensitive | **Yes**                |
| Example   | `a-long-random-string` |

Supply it as `Authorization: Bearer <token>` or `X-API-Token: <token>`; it is compared with a
constant-time check. Leave it unset to disable the read-only path entirely.

Covered endpoints:

- `GET /api/errors/`
- `GET /api/errors/{id}`
- `GET /api/errors/telemetry`
- `GET /api/diagnostics/export`
- `GET /api/diagnostics/taint-audit`
- `GET /api/debug/profiles/tools`

`GET /api/diagnostics/taint-audit` summarizes recent taint policy decisions by tool, sink, tier,
outcome, and source category, and inventories stored message-history rows by interface, role,
metadata version, and taint tier — so repeated reads of the same legacy row do not inflate the
apparent backfill size. Use `days` to set the audit window and `max_events` to bound processing; the
response says explicitly when that cap truncated the breakdown. It exposes no message content,
conversation IDs, tool arguments, or source IDs.

The token grants no access beyond those endpoints. It does not make anything public that was not
already: `/api/*` bypasses `AuthMiddleware` and individual routes enforce their own auth, so a
deliberately public route such as the `POST /api/errors/` report receiver stays reachable without
it. In particular `GET /api/debug/profiles` (the full config dump) is **not** covered, while the
tool-inventory endpoint that is covered exposes only tool names and sizes — no prompts and no policy
bodies.

______________________________________________________________________

## External Services

### BRAVE_API_KEY

Brave Search API key for web search.

| Property  | Value                      |
| --------- | -------------------------- |
| Required  | Yes (for Brave search MCP) |
| Default   | None                       |
| Sensitive | **Yes**                    |
| Example   | `BSA...`                   |

Used by the Brave Search MCP server.

______________________________________________________________________

### GOOGLE_MAPS_API_KEY

Google Maps API key for location services.

| Property  | Value                     |
| --------- | ------------------------- |
| Required  | Yes (for Google Maps MCP) |
| Default   | None                      |
| Sensitive | **Yes**                   |
| Example   | `AIzaSy...`               |

Used by the Google Maps MCP server.

______________________________________________________________________

### WILLYWEATHER_API_KEY

WillyWeather API key for Australian weather data.

| Property  | Value       |
| --------- | ----------- |
| Required  | No          |
| Default   | None        |
| Sensitive | **Yes**     |
| Example   | `abc123...` |

______________________________________________________________________

### WILLYWEATHER_LOCATION_ID

WillyWeather location ID for weather forecasts.

| Property  | Value                      |
| --------- | -------------------------- |
| Required  | No (if using WillyWeather) |
| Default   | None                       |
| Sensitive | No                         |
| Example   | `12345`                    |

Must be an integer.

______________________________________________________________________

## Camera Integration

### REOLINK_CAMERAS

JSON configuration for Reolink camera backends.

| Property  | Value                                                                                                    |
| --------- | -------------------------------------------------------------------------------------------------------- |
| Required  | No                                                                                                       |
| Default   | None                                                                                                     |
| Sensitive | **Yes** (contains passwords)                                                                             |
| Example   | `{"coop": {"host": "192.168.1.100", "username": "admin", "password": "secret", "name": "Chicken Coop"}}` |

Alternative to configuring cameras in `config.yaml`.

______________________________________________________________________

## Telephony (Asterisk)

### ASTERISK_SECRET_TOKEN

Secret token for authenticating Asterisk WebSocket connections.

| Property  | Value                          |
| --------- | ------------------------------ |
| Required  | No                             |
| Default   | None (authentication disabled) |
| Sensitive | **Yes**                        |
| Example   | `my-secure-token-123`          |

______________________________________________________________________

### ASTERISK_ALLOWED_EXTENSIONS

Comma-separated list of allowed Asterisk extensions.

| Property  | Value                          |
| --------- | ------------------------------ |
| Required  | No                             |
| Default   | Empty (all extensions allowed) |
| Sensitive | No                             |
| Example   | `100,101,102`                  |

______________________________________________________________________

## Advanced Configuration

### DEFAULT_SERVICE_PROFILE_ID

Default service profile to use when none specified.

| Property  | Value               |
| --------- | ------------------- |
| Required  | No                  |
| Default   | `default_assistant` |
| Sensitive | No                  |
| Example   | `custom_profile`    |

______________________________________________________________________

### MCP_CONFIG_PATH

Path to MCP server configuration file.

| Property  | Value                                   |
| --------- | --------------------------------------- |
| Required  | No                                      |
| Default   | `mcp_config.json`                       |
| Sensitive | No                                      |
| Example   | `/etc/family-assistant/mcp_config.json` |

______________________________________________________________________

### MCP_INITIALIZATION_TIMEOUT_SECONDS

Timeout for MCP server initialization.

| Property  | Value |
| --------- | ----- |
| Required  | No    |
| Default   | `60`  |
| Sensitive | No    |
| Example   | `120` |

______________________________________________________________________

### TOOLS_REQUIRING_CONFIRMATION

Comma-separated list of tools requiring user confirmation.

| Property  | Value                                         |
| --------- | --------------------------------------------- |
| Required  | No                                            |
| Default   | As configured in `config.yaml`                |
| Sensitive | No                                            |
| Example   | `delete_calendar_event,modify_calendar_event` |

______________________________________________________________________

### INDEXING_PIPELINE_CONFIG_JSON

JSON configuration for document indexing pipeline.

| Property  | Value                          |
| --------- | ------------------------------ |
| Required  | No                             |
| Default   | As configured in `config.yaml` |
| Sensitive | No                             |
| Example   | `{"processors": [...]}`        |

Overrides `indexing_pipeline_config` from config.yaml.

______________________________________________________________________

### LOGGING_CONFIG

Path to Python logging configuration file.

| Property  | Value                                |
| --------- | ------------------------------------ |
| Required  | No                                   |
| Default   | `logging.conf`                       |
| Sensitive | No                                   |
| Example   | `/etc/family-assistant/logging.conf` |

______________________________________________________________________

### ALEMBIC_CONFIG

Path to Alembic configuration file for database migrations.

| Property  | Value              |
| --------- | ------------------ |
| Required  | No                 |
| Default   | Auto-detected      |
| Sensitive | No                 |
| Example   | `/app/alembic.ini` |

______________________________________________________________________

### ASSISTANT_DEBUG_MODE

Enable assistant debug mode.

| Property  | Value   |
| --------- | ------- |
| Required  | No      |
| Default   | `false` |
| Sensitive | No      |
| Example   | `true`  |

______________________________________________________________________

## Global Tool Policy

### global_tools_policy

Top-level config section whose tool-policy rules are injected into **every** profile's tool-policy
engine.

| Property  | Value                                            |
| --------- | ------------------------------------------------ |
| Required  | No                                               |
| Default   | The shipped rules in `defaults.yaml` (see below) |
| Sensitive | No                                               |
| Example   | See the YAML block below                         |

The rules apply regardless of the profile's own `tools_policy`, which otherwise replaces the shipped
defaults wholesale rather than merging with them. Use it for tools that must be available in all
contexts. Operator policy still overrides global rules.

The shipped default uses it for two things:

- `report_technical_problem`, so the assistant can always report bugs — these surface through the
  error-log and diagnostics endpoints.
- `read_text_attachment` and `jq_query`, so the assistant can always read back tool results that
  exceeded the large-result threshold and were auto-converted to attachments.

```yaml
global_tools_policy:
  rules:
    - match:
        names:
          - "report_technical_problem"
      decision: "allow"
      priority: 50
    - match:
        names:
          - "read_text_attachment"
          - "jq_query"
      decision: "allow"
      priority: 50
```

______________________________________________________________________

## Configuration File Reference

### config.yaml

The main configuration file (`config.yaml`) supports:

- **llm_parameters**: Model-specific LLM parameters
- **gemini_live_config**: Gemini Live voice API configuration
- **indexing_pipeline_config**: Document processing pipeline
- **calendar_config**: Calendar sources and duplicate detection
- **default_profile_settings**: Default service profile configuration
- **service_profiles**: Multiple assistant profiles with different capabilities
- **attachment_config**: Attachment handling settings
- **event_system**: Event system configuration

Operator merge note for service profiles:

- `service_profiles` in `config.yaml` are **merged by ID** with `defaults.yaml`.
- Profiles with new IDs are added alongside the built-in defaults.
- Profiles whose ID matches a default override that default.
- You do not need to re-list all default profiles to add a new one.

Operator merge note for tool policy:

- `default_profile_settings.tools_policy.rules` in `config.yaml` are additive.
- Shipped rules from `defaults.yaml` are preserved, and operator rules are layered on top with
  higher precedence.
- `default_profile_settings.tools_policy.default_decision` still overrides the shipped default when
  explicitly set.

### mcp_config.json

MCP server definitions with environment variable expansion using `$VAR` or `${VAR}` syntax:

```json
{
  "mcpServers": {
    "brave": {
      "command": "...",
      "env": {
        "BRAVE_API_KEY": "$BRAVE_API_KEY"
      }
    }
  }
}
```

Expansion applies to every string value: stdio `command`, `args`, and `env` entries, and the `url`
of a remote SSE or Streamable HTTP server. For example, `"url": "${MCP_HTTP_ORIGIN}/mcp"` keeps a
deployment-specific origin out of `config.yaml`.

#### tool_metadata (taint classification)

Configure `tool_metadata` for MCP servers whose protocol annotations do not describe their security
boundary, so the runtime taint policy can classify their tools correctly:

- `code_execution` or `worker` — network-capable sandboxes.
- `home_auto` — actions confined to the household system.
- `low_bandwidth_external` — constrained providers such as a search API that receives only a query.
- `external_comm` or `browser` — anything that sends messages, invokes webhooks, or accepts
  attacker-controlled destinations. Use these rather than `home_auto` for a Home Assistant service
  that does any of those things.

Every configured entry should also declare `output_trusted` or `output_untrusted`. An exact
`tool_metadata` entry **replaces** the tool's annotation-derived tags rather than adding to them.

### prompts.yaml

LLM prompts with template variables:

- `{current_time}` - Current timestamp
- `{user_name}` - User's display name
- `{aggregated_other_context}` - Context from providers

______________________________________________________________________

## Security Best Practices

1. **Never commit secrets** - Use environment variables or secret management
2. **Use `.env` files locally** - Add to `.gitignore`
3. **Rotate API keys regularly** - Especially after potential exposure
4. **Limit ALLOWED_USER_IDS** - Only permit known users
5. **Use HTTPS for SERVER_URL** - In production environments
6. **Secure VAPID keys** - Treat as cryptographic secrets
7. **Review MCP server access** - Limit enabled servers per profile

______________________________________________________________________

## Example .env File

```bash
# Core
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/family_assistant
SERVER_URL=https://assistant.example.com
TIMEZONE=Australia/Sydney

# Telegram
TELEGRAM_BOT_TOKEN=your-bot-token
ALLOWED_USER_IDS=123456789
DEVELOPER_CHAT_ID=123456789

# AI Services
GEMINI_API_KEY=your-gemini-key
OPENAI_API_KEY=your-openai-key

# Calendar
CALDAV_USERNAME=user@example.com
CALDAV_PASSWORD=app-specific-password
CALDAV_CALENDAR_URLS=https://caldav.example.com/calendars/home

# Home Assistant
HOMEASSISTANT_URL=https://homeassistant.local:8123
HOMEASSISTANT_API_KEY=your-long-lived-token

# Push Notifications
VAPID_PRIVATE_KEY=your-private-key
VAPID_CONTACT_EMAIL=mailto:admin@example.com

# External Services
BRAVE_API_KEY=your-brave-key
GOOGLE_MAPS_API_KEY=your-maps-key
```

______________________________________________________________________

## CLI Arguments

The following options can be passed as command-line arguments:

| Argument                    | Description                      |
| --------------------------- | -------------------------------- |
| `--telegram-token`          | Override Telegram bot token      |
| `--openrouter-api-key`      | Override OpenRouter API key      |
| `--model`                   | Override default LLM model       |
| `--embedding-model`         | Override embedding model         |
| `--embedding-dimensions`    | Override embedding dimensions    |
| `--document-storage-path`   | Override document storage path   |
| `--attachment-storage-path` | Override attachment storage path |

CLI arguments have the highest priority and override all other configuration sources.

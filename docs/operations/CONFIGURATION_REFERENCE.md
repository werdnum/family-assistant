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

Mailgun HTTP webhook signing key for the receiving domain.

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

______________________________________________________________________

### EMBEDDING_DIMENSIONS

Dimensionality of embedding vectors.

| Property  | Value         |
| --------- | ------------- |
| Required  | No            |
| Default   | `1536`        |
| Sensitive | No            |
| Example   | `768`, `3072` |

Must match the dimensions produced by the configured embedding model.

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

| Property  | Value                              |
| --------- | ---------------------------------- |
| Required  | No (auto-derived from private key) |
| Default   | Derived from VAPID_PRIVATE_KEY     |
| Sensitive | No                                 |
| Example   | `BG1l7...`                         |

Same URL-safe base64 encoding as private key.

______________________________________________________________________

### VAPID_CONTACT_EMAIL

Admin contact email for VAPID claims.

| Property  | Value                        |
| --------- | ---------------------------- |
| Required  | Yes (for push notifications) |
| Default   | None                         |
| Sensitive | No                           |
| Example   | `mailto:admin@example.com`   |

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

MCP server definitions with environment variable expansion using `${VAR}` syntax:

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

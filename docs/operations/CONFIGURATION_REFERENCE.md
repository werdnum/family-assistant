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

### POSTGRES_STATEMENT_TIMEOUT_MS

Server-side ceiling, in milliseconds, on any single PostgreSQL statement the application runs.

| Property  | Value         |
| --------- | ------------- |
| Required  | No            |
| Default   | `60000` (60s) |
| Sensitive | No            |
| Example   | `30000`       |

A backstop, not a latency target — nothing the application runs should come close to it. It exists
so that a statement whose caller has gone away cannot keep consuming database capacity indefinitely;
without it, a request cancelled after 2.9 seconds left its query running for 41 minutes. Set `0` to
disable, which is PostgreSQL's own meaning for the setting.

Startup migrations are exempt: they run on a connection from this same pool, so the migration runner
lifts the ceiling for their duration (a large index build or backfill is the one place a long
statement is legitimate). The exemption is local to the migration transaction and disappears when
that transaction commits. Ignored on SQLite.

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

## Privacy Policy Page

Every deployment serves a privacy policy at `/privacy`. The path is in `PUBLIC_PATHS`, so it renders
without authentication — App Store Connect and TestFlight require a policy URL reachable by
reviewers who have no account. Use your `SERVER_URL` with `/privacy` appended as the privacy policy
URL.

The policy text lives in `src/family_assistant/templates/privacy_policy.html.j2` and describes the
software's data handling; the two settings below identify the operator of your instance, who is the
data controller.

### PRIVACY_POLICY_OPERATOR

Name of the person or household operating this instance, shown in the policy.

| Property  | Value                                                    |
| --------- | -------------------------------------------------------- |
| Required  | No                                                       |
| Default   | `the person who operates this Family Assistant instance` |
| Sensitive | No                                                       |
| Example   | `The Garrett household`                                  |

______________________________________________________________________

### PRIVACY_POLICY_CONTACT_EMAIL

Contact address for privacy questions and data deletion requests. When unset, the policy directs
readers to the operator instead of showing an address. Set it on any instance reachable from the
internet, and especially where email intake is enabled — inbound mail can come from people who are
not users of the instance and have no other way to reach its operator.

| Property  | Value                 |
| --------- | --------------------- |
| Required  | No                    |
| Default   | Unset                 |
| Sensitive | No                    |
| Example   | `privacy@example.com` |

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

### llm_parameters (reasoning and thinking)

`llm_parameters` in `config.yaml` maps a model name or prefix to keyword arguments passed to that
provider for every matching model. It is shared by all profiles, including both halves of a
`retry_config`.

```yaml
llm_parameters:
  "claude-sonnet-4-6":
    thinking:
      type: enabled
      budget_tokens: 4096
  "some-openai-model":
    use_responses_api: false # opt a model back out; the default is on
```

#### Reasoning propagation, by provider

Reasoning state has to be replayed on each turn of a tool loop or the model restarts its reasoning
from scratch on every step. How that works differs:

- **Google (Gemini)** — thought signatures are captured and replayed automatically. No
  configuration.

- **OpenAI** — direct OpenAI models use the **Responses API by default**. Only Responses returns the
  encrypted reasoning items that can be replayed; Chat Completions has no equivalent, so anything
  left on it loses reasoning between tool-loop steps. Defaulting on means a newly configured model
  gets reasoning propagation without anyone remembering to enrol it. Set `use_responses_api: false`
  to pin a specific model back to Chat Completions. Direct OpenAI only; OpenRouter and other
  `base_url` backends implement Chat Completions, and the flag is ignored for them.

  Switching a model between the two APIs is observable beyond reasoning: parallel tool-calling
  behaviour can differ for the same prompt, and structured output (`generate_structured` /
  `generate_json`) always uses Chat Completions regardless of this setting.

- **Anthropic** — extended thinking is off by default. Once enabled, thinking blocks are captured
  and replayed automatically.

#### Anthropic thinking

The configuration shape differs by model generation and the two are **not** interchangeable — a
single `"claude-"` prefix entry cannot serve both:

| Model generation           | Shape                                                                  |
| -------------------------- | ---------------------------------------------------------------------- |
| `claude-sonnet-4-6`, `4-5` | `thinking: {type: enabled, budget_tokens: N}`                          |
| `claude-fable-5`           | `thinking: {type: adaptive}` plus `output_config: {effort: low\|high}` |

Applying the `enabled` shape to a model that wants `adaptive` is rejected by the API at request time
with a message naming the alternative.

`budget_tokens` must be less than `max_tokens` (default 8192). A budget that cannot fit is rejected
up front with both values named, rather than failing mid-conversation. Raise `max_tokens` in the
same `llm_parameters` entry if you want a larger budget.

Enabling thinking is worthwhile mainly for long tool loops — the profile running Claude (`engineer`)
is the candidate. Note that thinking is incompatible with a non-default `temperature`.

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

## iOS Universal Links

The server publishes `/.well-known/apple-app-site-association` for native app-auth callbacks and
shared-conversation links. The checked-in production defaults match the
`assistant.andrewgarrett.dev` app; set both variables for a differently signed or self-hosted app.
Setting both also makes the app-auth callback use the verified HTTPS Universal Link instead of the
`familyassistant://` fallback.

### APPLE_TEAM_ID

Apple Developer Team ID used in the AASA `appID`.

| Property  | Value        |
| --------- | ------------ |
| Required  | Self-hosted  |
| Default   | `H7NBC2S52X` |
| Sensitive | No           |

### APPLE_BUNDLE_ID

iOS application bundle identifier used in the AASA `appID`.

| Property  | Value                         |
| --------- | ----------------------------- |
| Required  | Self-hosted                   |
| Default   | `dev.andrewgarrett.assistant` |
| Sensitive | No                            |

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
`attacker_addressable_egress`, `sandbox_network`, and `sensitive_read_broadening` — at least at
`confirm` for untrusted content. An `adjudicate` cell satisfies this check only when its effective
reviewer verdict floor is `confirm` or `deny`, whether the floor comes from the cell's
`verdict_floor` or from `operator_minimum`. Bare `adjudicate` can return `allow` and therefore does
not satisfy the check.

The shipped review-era defaults deliberately use bare, unfloored `adjudicate` cells and keep
`sensitive_read_broadening` at `audit`, so merely changing the shipped `taint_policy.mode` from
`observe` to `enforce` does not make this registration requirement pass. Add explicit floors or use
the documented [migration pin](#migration-for-deployments-already-enforcing-taint-policy) to retain
the earlier deterministic gates. If the check fails, the tools are not registered and the
integration status endpoint reports the unmet condition.

Setting it to `false` waives the check (logged at startup and surfaced on the status endpoint) and
the tools register regardless of taint mode.

Two sink classes are deliberately softer at `unknown_external` in the shipped matrix and are not
part of the floor check:

- **`home_local`** (Home Assistant actions). If the deployment drives high-consequence actuators —
  locks, garage doors, alarms — consider raising this sink to `confirm` at `unknown_external` via
  `taint_policy.matrix_overrides` or `operator_minimum`.

- **`artifact_write`** (note and calendar writes). Destructive mutations resolve here and the matrix
  alone does not confirm them at `unknown_external` — `delete_note` runs unconfirmed on that path.
  Provenance stamping stops injected instructions laundering themselves into trusted storage, but
  raise `artifact_write` to `confirm` via `matrix_overrides` if you want deletions confirmed once
  any external content has been read.

  This describes the taint matrix only. Ordinary tool policy is a separate and independent gate: the
  shipped `default_profile_settings.tools_policy` carries a priority-20 `confirm` rule for
  `delete_calendar_event` and `modify_calendar_event`, so those remain confirmed whatever the matrix
  says. A soft taint outcome never removes a confirmation that tool policy imposes.

______________________________________________________________________

## Automatic Tool-Call Review

### tool_call_review

Configures the shared, non-agentic judge used by runtime-taint `adjudicate` cells and static
tool-policy `review` rules.

```yaml
tool_call_review:
  enabled: true
  provider: "google"
  model: "gemini-3.7-flash"
  timeout_seconds: 30.0
  max_reviews_per_turn: 25
  escalation:
    consecutive_denials: 3
    total_denials_per_turn: 20
  guidance: >-
    Optional deployment-wide trusted guidance about routine workflows.
```

`timeout_seconds` bounds a single review, which runs inline: the gated tool call waits on it, so the
value trades a stalled turn against a fail-closed fallback. The default of 30 s suits a reasoning
model on a long prompt — reviews on `gemini-3.7-flash` commonly take 4-10 s with a tail past 15 s,
and a budget near that range turns ordinary reviews into fallbacks rather than judgements. Lower it
only with evidence from `taint_audit_events` that the configured model returns sooner, and read it
alongside `max_reviews_per_turn`, which bounds how many such waits one turn can incur.

The reviewer gets no tools. It receives only explicitly trusted-tier conversation rows, an
audit-safe provenance digest, the matched policy context, and the complete proposed arguments fenced
as untrusted data. It returns `allow`, `confirm`, or `deny`. `confirm` uses the existing durable
confirmation path; `deny` returns a structured refusal so the calling model can continue and choose
another route. Provider errors, malformed output, timeouts, a disabled reviewer, and per-turn budget
exhaustion all use the caller-owned fallback; no such path can resolve to `allow`. The configured
reviewer provider is initialized on its first review rather than during application startup. A
deployment that has not configured that provider's credentials can still start, while any attempted
review fails closed through the same caller-owned fallback. Provider initialization failures are
cached for the process lifetime, so restart after correcting credentials or provider configuration.

Profiles can add trusted, additive instructions with `processing_config.review_guidance`. Do not put
request data, trigger payloads, browser content, or secrets in either guidance field.

Runtime-taint matrix cells accept either the short form `adjudicate` (whose fallback is derived from
the pre-review default) or an explicit cell:

```yaml
taint_policy:
  matrix_overrides:
    unknown_external:
      sandbox_network:
        outcome: "adjudicate"
        verdict_floor: "confirm" # optional: confirm or deny
        fallback: "deny"         # required for a new cell: confirm or deny
```

A `confirm` floor limits the model to `confirm`/`deny`; a `deny` floor limits it to `deny`.
`operator_minimum` is applied as a verdict floor and profiles cannot relax it. `redact` cannot be a
minimum for an adjudicated cell. In `observe` mode, adjudication still runs but its effect is
`audit`; taint-only reviews are detached from the execution path and drained during shutdown.

Static policy can delegate a matched call in the same way:

```yaml
tools_policy:
  rules:
    - match: {tags_any: ["destructive"]}
      decision: "review"
      priority: 20
      description: "Judge destructive operations against the trusted request"
```

`review` tools remain advertised even without a live confirmation channel. When static `review` and
taint `adjudicate` match the same call, one reviewer invocation receives both contexts; their
verdict spaces and fallbacks merge toward the stricter result. A `confirm` verdict with no available
confirmation path degrades to a structured denial.

The confined-profile exemption skips a taint-only disclosure review only when
`include_aggregated_context` is `false`, the turn recorded no sensitive reads or high-taint history,
no effective `confirm`/`deny` floor applies, and the reviewer message window proves it contains only
the current turn (system scaffolding followed by the current user message, with no prior user,
assistant, tool, or error rows). This taint-layer exemption also applies to browser-tagged actions;
independent static/action-review rules are unchanged. Browser tools that return page content are
tagged as sensitive reads, so a successful browser read disables the exemption for later
disclosures. A missing or ambiguous message window fails toward review. The exemption resolves to
`audit`, never `allow`. Destination-bearing local tools declare their destination argument in
trusted metadata; an exact whole-value match in the current trusted request is passed to the
reviewer as evidence, not as authorization. URL matching normalizes scheme and host case but
preserves path, query, and fragment case; non-URL destinations retain case-insensitive text
normalization.

The central executor treats every successful tool tagged both `read_only` and `sensitive_data` as a
sensitive read. A tool can record a narrower corpus scope itself; otherwise the executor records a
conservative tool-level scope by comparing state before and after successful execution. There is no
in-flight read reservation: a concurrent disclosure formed before the read returns cannot causally
contain its result, while disclosures after return see the recorded read and cannot exempt.

`GET /api/diagnostics/taint-audit` includes verdict and resolution-status counts. Individual
`tool_call_review` audit events include the verdict, reason, latency, fallback use, delegating
contexts, allowed verdicts, and destination-echo signal without storing raw tool arguments or the
reviewer's free-form rationale. The reason is fixed trusted text; trusted local-schema argument
names may appear in the argument summary, while every argument value is omitted and MCP or
unexpected mapping keys are pseudonymized. For message-originated calls, `turn_id` and
`tool_call_id` can locate the canonical stored assistant message for later reconstruction without
duplicating it in the audit table. Direct named-sink and other non-message-originated authorizations
may have no corresponding message row, so their structured event is the complete durable record.
When a blocking-path model-denial threshold is reserved, one `tool_call_review_escalation` event is
also recorded with review status `escalation_confirmation_requested` or
`escalation_turn_terminated`. Detached observe-only reviews do not update denial counters, so these
trip counts intentionally describe blocking static/enforce paths rather than shadow traffic.

#### Capture (live-capture friction set)

The `tool_call_review.capture` block controls on-source capture of reviewed inputs into a private,
per-deployment dataset for the tool-call review evaluation harness. It is the friction half of that
harness: production traffic contains no real attacks, so every captured review is a real, benign
task shape that the eval replays to measure false denials. See
[../design/tool-call-review-eval.md](../design/tool-call-review-eval.md) and the maintainer runbook
[../development/review-eval-history-extraction.md](../development/review-eval-history-extraction.md).

```yaml
tool_call_review:
  capture:
    enabled: false
    directory: ".review-eval-local/captures"
    allow_external_directory: false
```

| Property                   | Default                       | Meaning                                                                                   |
| -------------------------- | ----------------------------- | ----------------------------------------------------------------------------------------- |
| `enabled`                  | `false`                       | Ships **off**. When true, each reviewed conversation input is serialized as it is judged. |
| `directory`                | `.review-eval-local/captures` | Where captures are written. Must contain a `.review-eval-local` path component.           |
| `allow_external_directory` | `false`                       | Escape hatch to write outside the private tree (e.g. a mounted private volume).           |

Privacy is structural. Captures hold **raw** household content — the exact `ToolCallReviewInput` the
judge saw, including the message window, guidance, policy contexts, and taint state that the audit
table deliberately does not persist. Byte-level fidelity is required (the assembly-parity check and
the destination-echo signal both match literal values), so no pseudonymization happens on write; a
pseudonymized copy is generated on demand only when a capture must be quoted or shared.

Because the content is raw, `directory` must stay under the gitignored `.review-eval-local/` tree —
this is a public repository, and a typo like `captures/` would write household content to a
commit-visible path. The configuration model **rejects a `directory` without a `.review-eval-local`
path component at load** unless `allow_external_directory: true` is set to opt into an explicitly
private location elsewhere. Capture is best-effort and off the review's critical path; a capture
failure never adds latency to or breaks a review.

Captures are stored **raw and interim-unlabeled**: they carry a placeholder `label: benign` only
because the schema's label is not yet nullable, and that interim label is not a real label. Only
captures a maintainer positively labels after skimming enter the friction pool and tuning metrics;
unlabeled captures replay for observation only. A benign-by-default capture would otherwise count a
correct `deny` as friction and teach tuning to prefer `allow` on it.

#### Migration for deployments already enforcing taint policy

The shipped default matrix now replaces the old egress and sandbox gates with `adjudicate`, and
makes `unknown_external` household messaging and sensitive-read broadening auditable. A deployment
already running `taint_policy.mode: enforce` must either adopt that judged posture deliberately or
pin every previously deterministic gate before upgrading. This is the literal cell-for-cell pin:

```yaml
taint_policy:
  operator_minimum:
    known_contact:
      arbitrary_external_message: "confirm"
      attacker_addressable_egress: "confirm"
      sandbox_network: "confirm"
    recognized_machine:
      arbitrary_external_message: "confirm"
      attacker_addressable_egress: "confirm"
      sandbox_network: "confirm"
    unknown_external:
      arbitrary_external_message: "confirm"
      attacker_addressable_egress: "confirm"
      known_user_message: "confirm"
      sensitive_read_broadening: "confirm"
      sandbox_network: "deny"
```

Until automation-definition provenance is persisted, every unattended callback enters at
`unknown_external`; the `known_user_message` minimum therefore makes a reminder's
`send_message_to_user` call create a deferred confirmation instead of delivering. A deployment that
requires automatic reminder delivery may deliberately omit that one entry while retaining the other
minima. That is a reminder-compatible exception to the old posture, not a cell-for-cell pin. Remove
the entry from `operator_minimum` to choose the shipped `audit` behavior; a weaker matrix override
cannot relax an operator minimum.

Keep production in `observe` until the audit data shows near-zero false allows on adversarial
replays, acceptable projected confirmation volume, and acceptable p95 reviewer latency.

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

## Confined Diagnostics Profile (`ops_automation`)

`ops_automation` exists so an unattended scheduled job can crawl recent error logs, triage them, and
record a summary without holding broad access. Delegation into it requires user confirmation.

The profile is deliberately narrow: it reads bounded diagnostics, writes **only** notes labelled
`ops_diagnostics` (enforced in the repository layer, so it cannot write elsewhere even when
instructed to), and can create only script automations — never ones that wake the full assistant.
Its notes are quarantined, so log text carrying injected content cannot reach a trusted
conversation.

To let a profile read those reports, grant it the `ops_diagnostics` visibility label. Without that
grant the reports are readable only through the web interface. See
[docs/design/profile-confined-note-writes-and-automation-approvals.md](../design/profile-confined-note-writes-and-automation-approvals.md).

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

The diagnostics token grants no access beyond the endpoints above. `/api/*` requires
`AuthMiddleware` authentication by default; the smaller set in `route_auth.NO_DEFAULT_AUTH_ROUTES`
instead uses its declared scoped policy or, for a deliberately public receiver such as
`POST /api/errors/`, bounded public handling. In particular `GET /api/debug/profiles` (the full
config dump) is **not** covered, while the tool-inventory endpoint that is covered exposes only tool
names and sizes — no prompts and no policy bodies.

______________________________________________________________________

## JWT Access Tokens (Edge Gateway Authentication)

### JWT_SIGNING_KEY

Optional PEM-encoded EC private key enabling short-lived signed access tokens (ES256). When set,
`POST /api/auth/exchange`, `POST /api/auth/refresh`, and `POST /api/auth/token` return a signed JWT
instead of an opaque secret; the JWT is also what the browser session bridge
(`GET /api/auth/browser-token`) sets as an HttpOnly cookie scoped to `/api`. A deployment's edge
gateway can then verify API requests statelessly against the published JWKS
(`GET /.well-known/jwks.json`) — see docs/design/jwt-edge-auth.md.

| Property  | Value                                                               |
| --------- | ------------------------------------------------------------------- |
| Required  | No                                                                  |
| Default   | Unset (opaque token behaviour)                                      |
| Sensitive | **Yes**                                                             |
| Example   | PEM text (generate with `openssl ecparam -name prime256v1 -genkey`) |

Unset ⇒ everything behaves as before (30-day opaque API tokens). Set but unparsable fails startup.
The public key is published at `/.well-known/jwks.json`; the route classification the gateway policy
must mirror is published at `/.well-known/auth-route-classification`.

### JWT_ACCESS_TOKEN_TTL_SECONDS

Lifetime of issued signed access tokens in seconds.

| Property | Value  |
| -------- | ------ |
| Required | No     |
| Default  | `3600` |

Requires `JWT_SIGNING_KEY`. A JWT upgraded from an expiring opaque token is capped at that token's
remaining lifetime. Revoked, expired, or deleted backing tokens are rejected immediately by the
server; edge gateways accept an already issued JWT until its own expiry by design.

### Error-intake abuse controls

`POST /api/errors/` is deliberately reachable without a session so error capture works pre-login and
with broken auth. It is bounded server-side: per-authenticated-user rate limiting (falling back to
client address before authentication) of 60 reports per 60 seconds (HTTP 429 beyond), a 64 KiB body
cap, hard field-length limits on the report model, and reports arriving without a valid session/API
credential are clamped into the in-memory telemetry ring. They are never persisted to `error_logs`,
whatever severity they claim, and are lost on process restart. Authenticated reporters keep the full
behaviour described above.

______________________________________________________________________

## External Services

### Keychute brokered script HTTP

Family Assistant can expose `keychute_http_request()` to Monty scripts while leaving credentials
inside [Keychute](https://github.com/werdnum/keychute). Family Assistant calls Keychute's HTTP API
directly to create and await an access request, validate the resulting grant, and proxy the call.

```yaml
keychute_config:
  enabled: true
  url: "https://keychute.example.com"
  token_file: "/var/run/secrets/keychute/token"
  ca_bundle: "/etc/ssl/keychute/ca.crt"
  max_response_bytes: 26214400
```

| Setting              | Default    | Description                                         |
| -------------------- | ---------- | --------------------------------------------------- |
| `enabled`            | `false`    | Exposes the brokered HTTP function to scripts.      |
| `url`                | `null`     | Keychute API origin.                                |
| `token`              | `null`     | Static client bearer token.                         |
| `token_file`         | `null`     | Rotating client bearer-token file.                  |
| `ca_bundle`          | `null`     | Optional PEM CA bundle for the Keychute connection. |
| `max_response_bytes` | `26214400` | Maximum upstream response body retained in RAM.     |

The URL, token, token file, and CA bundle may instead be supplied through `KEYCHUTE_URL`,
`KEYCHUTE_TOKEN`, `KEYCHUTE_TOKEN_FILE`, and `KEYCHUTE_CA_BUNDLE`. Production deployments normally
use the rotating service-account token file. The file is reread for every Keychute API request so
token rotation continues during an approval wait. Enabling the integration without a working
configuration fails individual script calls explicitly; it does not fall back to direct
unauthenticated HTTP.

______________________________________________________________________

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
| Required  | When `JWT_SIGNING_KEY` is set  |
| Default   | None (authentication disabled) |
| Sensitive | **Yes**                        |
| Example   | `my-secure-token-123`          |

The Asterisk WebSocket is exempt from gateway JWT enforcement because the client cannot attach a
browser cookie or authorization header. When signed JWT authentication is enabled, the backend
rejects all Asterisk connections unless this separate transport secret is configured.

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

**A profile's own `tools_policy` cannot opt out.** Global rules are injected at the `profile` policy
layer, which outranks the `defaults` layer a profile's own `tools_policy` occupies, so a deny rule
written there does not override a global allow at any priority — layer beats priority.

Use `excluded_global_tools` on the profile to withhold one. It denies in the same layer as the
global rules at the maximum priority, which is what makes it effective. The shipped `media_analyst`
profile withholds all three, because none is safe in a fully untrusted context:
`read_text_attachment` and `jq_query` resolve any attachment the acting user owns rather than only
the current turn's artifacts, and `report_technical_problem` persists model-supplied text.

```yaml
service_profiles:
  - id: "media_analyst"
    excluded_global_tools:
      - "read_text_attachment"
      - "jq_query"
      - "report_technical_problem"
```

Keep this section to broadly safe tools, and treat anything that reads user-owned data by id, or
writes, as needing an exclusion in every profile that processes untrusted input.

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

### include_aggregated_context

Per-profile `processing_config` flag deciding whether the profile receives the context providers'
output at all.

| Property  | Value            |
| --------- | ---------------- |
| Required  | No               |
| Default   | `false`          |
| Sensitive | No               |
| Values    | `true` / `false` |

Context providers gather the household's notes, calendar, known users, weather and Home Assistant
state. When this is `true`, that material reaches the model in the trailing `<turn_context>` block
appended to the end of every request. When it is `false` — the default — the profile receives none
of it.

The default is off so that a profile nobody has explicitly considered is denied the household's
private data rather than granted it — a profile that reads untrusted input should not also hold
sensitive data. Turning it on for such a profile is the combination the Rule of Two exists to
prevent.

`excluded_context_providers` is the finer-grained control underneath it — it drops individual
providers from a profile that has this flag on, and has no effect on a profile that does not.

The current time is injected for **every** profile regardless of this flag; it is not sensitive.

Shipped `defaults.yaml` sets it on six profiles: `default_assistant`, `data_visualization`,
`camera_analyst`, `event_handler`, `complex_tasks` and `engineer`. Every other profile — including
`telephone_external`, which serves external callers, and `media_analyst`, which reads
attacker-controlled media — gets no aggregated context.

```yaml
service_profiles:
  - id: "my_profile"
    processing_config:
      include_aggregated_context: true
```

______________________________________________________________________

### excluded_context_providers

Per-profile `processing_config` list naming context providers to drop for that profile.

| Property  | Value                                                           |
| --------- | --------------------------------------------------------------- |
| Required  | No                                                              |
| Default   | `[]` (every applicable provider is attached)                    |
| Sensitive | No                                                              |
| Values    | `notes`, `calendar`, `known_users`, `weather`, `home_assistant` |

Context providers inject the user's own data into the trailing `<turn_context>` block. They apply
only to profiles that set `include_aggregated_context: true`; within such a profile every applicable
provider is attached by default (`weather` and `home_assistant` only when configured). An
unrecognised name is a startup error rather than a no-op, since a silently-ignored entry would leave
a profile holding data the config says it doesn't.

Use it to keep private data out of a profile that needs some context but not all of it. The shipped
`media_analyst` profile excludes all five as a second layer on top of leaving
`include_aggregated_context` at its default: it exists to transcribe attacker-controlled media, so
pairing the user's notes with that input is precisely the combination the Rule of Two is meant to
prevent.

```yaml
service_profiles:
  - id: "media_analyst"
    processing_config:
      excluded_context_providers:
        - "notes"
        - "calendar"
        - "known_users"
        - "weather"
        - "home_assistant"
```

______________________________________________________________________

### antigravity_config

Per-profile `processing_config` block tuning the Google Antigravity managed agent. Read only when
`llm_model` names the agent (`antigravity-preview-05-2026` or a later `antigravity-*` revision);
setting it on any other profile is a startup error rather than a silently discarded block.

| Property  | Value                                                                   |
| --------- | ----------------------------------------------------------------------- |
| Required  | No                                                                      |
| Default   | `model: gemini-3.7-flash`, no token cap, no environment                 |
| Sensitive | No (credentials are named by env var, never written here)               |
| Values    | `model` (string), `max_total_tokens` (int > 0), `environment` (mapping) |

`model` is the model the agent reasons with — the Gemini 3.x Flash family, `gemini-3.7-flash` being
the current default. It is pinned in `defaults.yaml` rather than left to the API, so an upstream
default change shows up as a config change. `max_total_tokens` caps what a single run may spend;
unset leaves the API's own default, which together with `max_async_seconds` is the only bound on how
long an autonomous run iterates.

#### `environment`: sandbox egress and injected credentials

`environment` describes the sandbox a run gets. Today that is its egress policy: which domains the
sandbox may reach, and which credentials the API's egress proxy attaches on the way out. **The
sandbox never receives a credential** — the agent issues an unauthenticated request and the proxy
adds the header, so nothing the agent can print, log or write to a file contains the token.

| Key         | Values                                                                  |
| ----------- | ----------------------------------------------------------------------- |
| `network`   | `default` (send no policy), `disabled` (no network at all), `allowlist` |
| `allowlist` | list of rules; required by, and only valid with, `allowlist`            |

Each allowlist rule takes `domain` (wildcards allowed; `*` matches everything), an optional
`headers` mapping of **non-secret** static headers, and an optional `credential`:

| Key           | Values                                                                   |
| ------------- | ------------------------------------------------------------------------ |
| `type`        | `github_app` (mints a token) or `bearer` (reads `token_env`)             |
| `header_name` | Header to inject; defaults to `Authorization`                            |
| `scheme`      | `bearer` (default) or `basic`                                            |
| `token_env`   | Env var holding the token; required by `bearer`, rejected on other types |

`scheme` matters because GitHub authenticates its REST API and git-over-HTTPS differently: `bearer`
renders `Authorization: Bearer <token>` (the REST API), and `basic` renders
`Authorization: Basic base64("x-access-token:<token>")` (git). Applying one to both fails as a 401
in the middle of an agent run rather than as a config error, so each domain names its own.

`type: "github_app"` reads the same environment variables the k8s-agent and ai-worker deployments
already use, so a cluster that runs a GitHub App needs no new secret plumbing — mount the existing
key and set:

| Variable                      | Purpose                                                         |
| ----------------------------- | --------------------------------------------------------------- |
| `GITHUB_APP_ID`               | The App's numeric id (JWT `iss`). Required.                     |
| `GITHUB_APP_INSTALLATION_ID`  | The installation to mint a token for. Required.                 |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to the App's PEM private key (a mounted secret).           |
| `GITHUB_APP_PRIVATE_KEY`      | The PEM contents inline, used in preference to the path if set. |

The App private key never leaves the process: it signs a short-lived JWT which is exchanged for an
installation access token, and only that ~1-hour token is handed to the proxy. A credential that
cannot be resolved — a missing variable, an unreadable key, a revoked installation — fails the run
rather than submitting it unauthenticated, which would otherwise surface as a 404 on a private
repository from inside the agent.

**Runs longer than the token cannot keep GitHub access.** The proxy is given one fixed header at
submit and there is no way to refresh it mid-run, so a token is minted fresh per run to give each
one the longest possible window — but a run that outlasts it (~1 hour) starts failing GitHub calls,
possibly on a final push. `max_async_seconds` for the shipped `coder` profile is `7200`. Set it
below an hour on a credentialed profile if GitHub must hold for the whole of every run; leave it
high if long runs matter more and late-run GitHub failures are acceptable.

**Injecting a credential widens the profile's Rule of Two class**, and how far is mostly set outside
this file. The shipped `coder` is `[C]` only; GitHub App access adds `[B]`, and the agent already
reads the open web (`[A]`). Three layers bound that, in the order they take effect:

1. **The credential's scope.** An App installation token reaches only the repositories the App is
   installed on, only with the permissions granted, and expires in about an hour. Scoping the
   installation is the primary control — it sets the blast radius before any runtime gate applies,
   so widening the App's installation or permissions is a security change in its own right.
2. **The taint gate.** `taint_sink_class: "sandbox_network"` with `taint_policy.mode: "enforce"`
   (see below) stops content the assistant derived from an email from directing the agent at all.
3. **The egress policy here.** A closed allowlist also stops the agent reading an
   attacker-controlled page mid-run while the proxy attaches a credential; a `{domain: "*"}` rule
   does not. Allow-all is the shape the API documents for "restrict nothing, inject on some" and
   what a coding agent installing packages from arbitrary indexes needs — a risk/benefit call
   against the scope set in (1), not a question with one right answer.

See
[antigravity-environment-and-credentials.md](../design/antigravity-environment-and-credentials.md).

The agent runs server-side on the Interactions API, so the profile must use `provider: "google"` and
must not set `retry_config` — a fallback would be an ordinary chat completion answering from the
model's own knowledge instead of running the task, which is why that combination is rejected at
startup. It needs `GEMINI_API_KEY` like any other Google profile.

The shipped profile using it is `coder` (slash command `/coder`), which holds no tools at all: the
agent works in a Google-hosted sandbox with no access to household data. See
[gemini-antigravity-managed-agent.md](../design/gemini-antigravity-managed-agent.md).

```yaml
service_profiles:
  - id: "coder"
    processing_config:
      provider: "google"
      llm_model: "antigravity-preview-05-2026"
      antigravity_config:
        model: "gemini-3.7-flash"
        max_total_tokens: 250000
        environment:
          network: "allowlist"
          allowlist:
            # Everything else the agent needs (package indexes, docs, the web).
            - domain: "*"
            # git clone/push authenticates as HTTP Basic...
            - domain: "github.com"
              credential:
                type: "github_app"
                scheme: "basic"
            # ...while the REST API takes a bearer token.
            - domain: "api.github.com"
              credential:
                type: "github_app"
```

______________________________________________________________________

### taint_sink_class

Per-profile `processing_config` value declaring the runtime-taint sink class that a **whole turn**
on this profile counts as.

| Property  | Value                                              |
| --------- | -------------------------------------------------- |
| Required  | No                                                 |
| Default   | unset (the profile is not a sink in its own right) |
| Sensitive | No                                                 |
| Values    | any `SinkClass` name, e.g. `sandbox_network`       |

Runtime taint normally gates individual **tools**: `spawn_worker` is classified `sandbox_network`,
and the shipped `unknown_external × sandbox_network` cell is bare `adjudicate`. Its reviewer may
return `allow`, `confirm`, or `deny`; `deny` is the fail-closed fallback when review is unavailable,
not a deterministic verdict floor. Deployments that need the earlier hard denial must set the
`operator_minimum` shown in the
[enforcement migration pin](#migration-for-deployments-already-enforcing-taint-policy). A profile
whose entire turn is the privileged operation — an agent that runs code in a sandbox — has no such
tool to gate, and delegating to it would otherwise be classified as an ordinary delegation.

Declaring a sink here changes two things. `delegate_to_service` calls naming this profile as
`target_service_id` are evaluated as that sink rather than as a generic delegation, so the caller is
refused (or asked to confirm) before a delegation run is created. And the profile itself evaluates
the sink against the turn's taint before every model call, covering the entry points a tool gate
does not see: slash commands, A2A requests and `wake_llm` automations. On a profile that declares a
sink and also holds tools, that per-call evaluation is what stops a tool result from raising the
turn's tier and then being fed to the model anyway. An `adjudicate` outcome invokes the shared
reviewer against the complete current state; a `confirm` verdict uses the turn's confirmation
channel when one exists and otherwise fails closed.

Attachments routed into such a profile contribute their own recorded provenance to that evaluation,
so an untrusted file raises the turn's tier even when the request text is trusted.

**A declared sink takes effect when `taint_policy.mode` does.** The deployment-wide mode defaults to
`observe`, which downgrades every gating outcome to `audit`, so a declared sink records rather than
decides until the deployment switches to `enforce`. Do not reach for a profile-level
`taint_policy.mode: enforce` to get there sooner. A profile may tighten the deployment policy, but
doing it here applies the shipped matrix to one profile ahead of the friction measurement that keeps
the rollout in `observe` — and it refuses more than the untrusted input it is aimed at, because the
tiers the matrix only wants *confirmed* have no confirmation path while the calling profile's tool
gate is still observing and therefore never asks. See
[runtime-taint-enforcement-operational-findings.md](../design/runtime-taint-enforcement-operational-findings.md).

The shipped `coder` profile declares the sink and leaves the mode to the deployment. See
[interactions-agent-taint-and-attachments.md](../design/interactions-agent-taint-and-attachments.md).

```yaml
service_profiles:
  - id: "coder"
    processing_config:
      taint_sink_class: "sandbox_network"
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

### llm_parameters

Per-model keyword arguments, passed through to whichever provider SDK serves that model. Keys are
matched as **substrings** of the model name, so `"gpt-5.6-"` covers every variant in that line while
`"gpt-5.6-terra"` targets one. Every matching entry is merged, and the result is applied **over**
the provider client's own defaults — so this is the supported way to override a hard-coded default
such as the Anthropic client's `max_tokens`.

Settings currently shipped in `defaults.yaml`:

| Key               | Setting                                                                            | Why                                                                                                                                                                               |
| ----------------- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude-sonnet-5` | `thinking: {type: adaptive}`, `output_config: {effort: high}`, `max_tokens: 16000` | Thinking on for `engineer`, whose long tool loops benefit most. `max_tokens` is raised because thinking shares that budget with the response. See the comment in `defaults.yaml`. |
| `gpt-5.6-sol`     | `reasoning_effort: high`                                                           | `complex_tasks` is reached by delegation, so it can afford to think longer.                                                                                                       |
| `gpt-5.6-terra`   | `reasoning_effort: medium`                                                         | The fallback for `default_assistant` and `camera_analyst`, both of which answer interactively, where time-to-first-token is felt directly.                                        |

`reasoning_effort` accepts `none`, `low`, `medium`, `high`, `xhigh` or `max` on GPT-5.6 models and
defaults to `medium` when unset. Raising it trades latency and tokens for capability; it is the
first dial to turn when a profile's agentic performance falls short, ahead of changing the model.

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

#### The environment a stdio server actually receives

A stdio server is spawned with a whitelisted environment — `HOME`, `LOGNAME`, `PATH`, `SHELL`,
`TERM`, `USER` — plus whatever the entry's own `env` block declares. Nothing else from the
application's environment reaches the child process.

This rules out launchers that rely on ambient configuration. `uvx <package>` cannot see
`UV_TOOL_DIR`, so it will not find a pre-installed tool environment and re-resolves the package from
PyPI on every connection, inside the initialization timeout and onto whatever versions resolve that
day. Install the server and invoke its entry point by name instead, or pass what it needs through
`env`.

A server that fails to start does not make the application unhealthy — it is logged, marked
`failed`, and its tools are simply absent. Run `poe check-mcp` (or
`python scripts/check_mcp_servers.py <server-id>`) to get a verdict per server.

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

LLM prompts with template variables. A profile's `system_prompt` may reference:

- `{user_name}` - User's display name
- `{server_url}` - Public base URL of the deployment (see [SERVER_URL](#server_url))
- `{profile_id}` - ID of the profile the prompt is being rendered for

That is the complete list. Any other placeholder is an error, and every profile's prompt is rendered
once at startup, so a template naming an unknown one fails the boot rather than the first
conversation with that profile. Escape literal braces as `{{` and `}}`.

> **⚠️ BREAKING CHANGE for custom prompts**: `{current_time}` and `{aggregated_other_context}` are
> no longer template variables. The current time and the context providers' output are delivered in
> the trailing `<turn_context>` block appended to each request instead of being interpolated into
> the system prompt, so that the system prompt and conversation history stay byte-stable and can be
> cached by the provider. A `system_prompt` still referencing either placeholder fails at startup.
> Delete the reference; if the profile needs the providers' output, set
> [include_aggregated_context](#include_aggregated_context) on it. The current time needs no opt-in.
> See [docs/design/prompt-cache-turn-context.md](../design/prompt-cache-turn-context.md).

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

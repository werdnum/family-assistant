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
| Example   | `109472877,123456789`  |

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
| Example   | `109472877` |

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

| Property  | Value                                   |
| --------- | --------------------------------------- |
| Required  | Yes (for OpenRouter models via LiteLLM) |
| Default   | None                                    |
| Sensitive | **Yes**                                 |
| Example   | `sk-or-v1-...`                          |

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

### LITELLM_DEBUG

Enable LiteLLM debug logging.

| Property  | Value   |
| --------- | ------- |
| Required  | No      |
| Default   | `false` |
| Sensitive | No      |
| Example   | `true`  |

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

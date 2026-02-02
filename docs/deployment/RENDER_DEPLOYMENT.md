# Render Deployment Guide

This guide explains how to deploy Family Assistant to [Render](https://render.com) using their
one-click deployment feature.

## One-Click Deployment

The easiest way to deploy Family Assistant is using the "Deploy to Render" button:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/werdnum/family-assistant)

This will:

1. Create a PostgreSQL database with pgvector extension
2. Create a web service running the Family Assistant application
3. Set up a persistent disk for document and attachment storage
4. Prompt you to enter required environment variables

## Prerequisites

Before deploying, you'll need:

1. **A Render account**: Sign up at [render.com](https://render.com)
2. **Telegram Bot Token**: Create a bot via [@BotFather](https://t.me/botfather)
3. **LLM API Key**: At least one of:
   - [Google Gemini](https://aistudio.google.com/) (recommended)
   - [OpenRouter](https://openrouter.ai/)
   - [OpenAI](https://platform.openai.com/)
4. **Your Telegram User ID**: Message [@userinfobot](https://t.me/userinfobot) to get your ID

## Required Environment Variables

During deployment, you'll be prompted to enter these values:

| Variable             | Required | Description                                      |
| -------------------- | -------- | ------------------------------------------------ |
| `TELEGRAM_BOT_TOKEN` | Yes      | Your Telegram bot token from @BotFather          |
| `ALLOWED_USER_IDS`   | Yes      | Comma-separated Telegram user IDs allowed access |
| `GEMINI_API_KEY`     | \*       | Google Gemini API key                            |
| `OPENROUTER_API_KEY` | \*       | OpenRouter API key                               |
| `OPENAI_API_KEY`     | \*       | OpenAI API key                                   |
| `DEVELOPER_CHAT_ID`  | No       | Telegram user ID for error notifications         |

\* At least one LLM API key is required.

## Optional Configuration

### Web UI Authentication (OIDC)

To secure the web interface with OpenID Connect:

| Variable              | Description                        |
| --------------------- | ---------------------------------- |
| `OIDC_CLIENT_ID`      | OIDC provider client ID            |
| `OIDC_CLIENT_SECRET`  | OIDC provider client secret        |
| `OIDC_DISCOVERY_URL`  | OIDC discovery endpoint URL        |
| `ALLOWED_OIDC_EMAILS` | Comma-separated allowed email list |

### Calendar Integration

| Variable               | Description                                    |
| ---------------------- | ---------------------------------------------- |
| `CALDAV_URL`           | CalDAV server URL (e.g., caldav.icloud.com)    |
| `CALDAV_USERNAME`      | CalDAV username                                |
| `CALDAV_PASSWORD`      | CalDAV app-specific password                   |
| `CALDAV_CALENDAR_URLS` | Direct calendar URLs                           |
| `ICAL_URLS`            | Comma-separated iCalendar URLs for read access |

### Push Notifications

| Variable              | Description                                    |
| --------------------- | ---------------------------------------------- |
| `VAPID_PRIVATE_KEY`   | VAPID private key (URL-safe base64)            |
| `VAPID_CONTACT_EMAIL` | Admin contact email (mailto:admin@example.com) |

Generate VAPID keys locally:

```bash
python scripts/generate_vapid_keys.py
```

### Additional Integrations

| Variable                | Description            |
| ----------------------- | ---------------------- |
| `BRAVE_API_KEY`         | Brave Search API key   |
| `HOMEASSISTANT_API_KEY` | Home Assistant API key |
| `GOOGLE_MAPS_API_KEY`   | Google Maps API key    |

## Post-Deployment Setup

### 1. Enable pgvector Extension

After deployment, connect to your database and enable the vector extension:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

You can do this via Render's database shell or using `psql` with the external connection string.

### 2. Access Your Application

Once deployed, your application will be available at:

- **Web UI**: `https://family-assistant-xxxx.onrender.com`
- **Telegram**: Message your bot to start chatting

### 3. Configure Your Domain (Optional)

To use a custom domain:

1. Go to your service's Settings in Render
2. Add your custom domain under "Custom Domains"
3. Configure DNS as instructed by Render

## Scaling Considerations

The default blueprint uses Render's "Starter" plan for both the web service and database. For
production use, consider upgrading:

### Web Service

- **Starter**: Good for testing and light use
- **Standard**: Recommended for production (includes zero-downtime deploys)
- **Pro**: For higher traffic and performance needs

### Database

- **Starter**: 1GB storage, good for testing
- **Standard**: 10GB storage, daily backups
- **Pro**: Higher performance and storage

To upgrade, edit the `plan` field in `render.yaml` or change it in the Render dashboard.

## Limitations

1. **Cold Starts**: Starter plan services may experience cold starts after periods of inactivity.
   Upgrade to Standard plan for always-on behavior.

2. **Disk Size**: The default persistent disk is 10GB. Increase `sizeGB` in `render.yaml` if you
   need more storage for documents and attachments.

3. **Region**: The default region is Oregon (`oregon`). Change the `region` field in `render.yaml`
   to deploy closer to your users.

## Troubleshooting

### Service Not Starting

Check the logs in the Render dashboard:

1. Go to your service → "Logs"
2. Look for error messages during startup
3. Common issues:
   - Missing required environment variables
   - Database connection errors (wait for database to be ready)

### Database Connection Issues

1. Verify the `DATABASE_URL` is correctly set (should be automatic from the blueprint)
2. Check that the database is in the same region as the web service
3. Ensure the pgvector extension is enabled

### Telegram Bot Not Responding

1. Verify `TELEGRAM_BOT_TOKEN` is correct
2. Check that `ALLOWED_USER_IDS` includes your Telegram user ID
3. Review logs for Telegram-related errors

## Updating Your Deployment

When you push changes to your repository:

1. Render will automatically rebuild and deploy (if auto-deploy is enabled)
2. Database migrations run automatically on startup
3. Zero-downtime deploys are available on Standard plan and above

## Cost Estimation

Approximate monthly costs on Render (as of 2024):

| Component              | Starter | Standard |
| ---------------------- | ------- | -------- |
| Web Service            | $7      | $25      |
| PostgreSQL Database    | $7      | $20      |
| Persistent Disk (10GB) | $2.50   | $2.50    |
| **Total**              | ~$17    | ~$48     |

Check [Render's pricing page](https://render.com/pricing) for current rates.

## Further Reading

- [Render Blueprint Specification](https://render.com/docs/blueprint-spec)
- [Render PostgreSQL Documentation](https://render.com/docs/databases)
- [Family Assistant Configuration Reference](../operations/CONFIGURATION_REFERENCE.md)

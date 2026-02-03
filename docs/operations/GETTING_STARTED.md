# Operator Getting Started Guide

This guide is intended for operators and administrators who want to deploy Family Assistant. It
covers different deployment methods, from simple Docker setups to full Kubernetes deployments.

## Prerequisites

Before deploying Family Assistant, you will need:

1. **Telegram Bot Token**: Create a bot via [@BotFather](https://t.me/botfather) and get the API
   token.
2. **LLM API Key**: At least one API key from the following providers:
   - [Google Gemini](https://aistudio.google.com/) (Recommended for native features)
   - [OpenRouter](https://openrouter.ai/)
   - [OpenAI](https://platform.openai.com/)
   - [Anthropic](https://console.anthropic.com/)
3. **Docker**: Ensure Docker and Docker Compose (v2+) are installed on your host.

______________________________________________________________________

## Deployment Options

### Option 1: Simple Deployment (Docker Compose)

This is the recommended method for most users. It sets up the application along with a PostgreSQL
database (including the required `pgvector` extension) for persistent storage and document indexing.

1. **Download the Docker Compose file**: You can find the production-ready `docker-compose.yaml` in
   the `deploy/` directory of the repository.

2. **Create a `.env` file**: Create a `.env` file in the **root directory** of the repository (not
   the `deploy/` directory) with your configuration:

   ```bash
   TELEGRAM_BOT_TOKEN=your_bot_token
   GEMINI_API_KEY=your_gemini_api_key
   ALLOWED_USER_IDS=your_telegram_user_id
   TIMEZONE=Australia/Sydney
   POSTGRES_PASSWORD=choose_a_strong_password
   ```

3. **Start the services**:

   ```bash
   docker compose -f deploy/docker-compose.yaml up -d
   ```

4. **Verify the deployment**: Check the logs to ensure everything started correctly:

   ```bash
   docker compose -f deploy/docker-compose.yaml logs -f
   ```

### ⚠️ Security Warning: Web UI Authentication

By default, the Web UI is **unsecured** in this configuration. Port 8000 is bound to `localhost`
(`127.0.0.1`) in the `docker-compose.yaml` to prevent accidental public exposure.

To enable secure authentication for the Web UI, you must configure OpenID Connect (OIDC). See the
[Configuration Reference](CONFIGURATION_REFERENCE.md) for details on setting up:

- `OIDC_CLIENT_ID`
- `OIDC_CLIENT_SECRET`
- `OIDC_DISCOVERY_URL`

______________________________________________________________________

### Option 2: Native Deployment (Kubernetes)

Family Assistant is natively designed to run on Kubernetes. This is the most robust deployment
method, suitable for high availability and advanced scaling.

For a streamlined "one-shot" deployment on Google Cloud, see the
[GKE Deployment Guide](../deployment/GKE_DEPLOYMENT.md).

For general instructions on Kubernetes deployment, see the
[Production Deployment Guide](../deployment/PRODUCTION_DEPLOYMENT.md).

The Kubernetes manifests are located in the `deploy/` directory:

- `deployment.yaml`: Main application deployment.
- `service.yaml`: Kubernetes service definition.
- `ingress-web.yaml`: Ingress for the Web UI.
- `secrets.yaml`: Template for application secrets.

______________________________________________________________________

### Option 3: Development/Testing (SQLite)

For quick testing or development, you can run the application with SQLite.

**⚠️ Note**: Vector search (document indexing and semantic search) is **not supported** with SQLite.
PostgreSQL with `pgvector` is required for these features.

```bash
docker run -d \
  --name family-assistant \
  -p 8000:8000 \
  -e TELEGRAM_BOT_TOKEN=your_token \
  -e GEMINI_API_KEY=your_key \
  -e ALLOWED_USER_IDS=your_id \
  -v assistant_data:/data \
  ghcr.io/werdnum/family-assistant:latest
```

______________________________________________________________________

## Configuration

Family Assistant is highly configurable. Key configuration areas include:

- **Environment Variables**: Managed via `.env` or container environment settings. See the
  [Configuration Reference](CONFIGURATION_REFERENCE.md) for a full list.
- **`config.yaml`**: Advanced configuration for service profiles, tool behaviors, and indexing
  pipelines.
- **`prompts.yaml`**: Customize the assistant's personality and system prompts.

## Monitoring & Operations

- **Health Checks**: The application exposes a `/health` endpoint on port 8000.
- **Logs**: Standard output (stdout) is used for all logging.
- **Backups**: If using Docker Compose, ensure you back up the `db_data` volume (PostgreSQL data)
  and `app_data` volume (uploaded documents).
- **Updates**: Pull the latest image and restart your containers to update:
  ```bash
  docker compose -f deploy/docker-compose.yaml pull
  docker compose -f deploy/docker-compose.yaml up -d
  ```

## Next Steps

- [User Guide](../user/USER_GUIDE.md): Learn how to use the features.
- [Troubleshooting](RUNBOOKS.md): Operational procedures and common issues.

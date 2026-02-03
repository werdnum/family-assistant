# GKE Deployment Guide

This guide describes how to deploy Family Assistant to Google Kubernetes Engine (GKE) using the
automated deployment script.

## Overview

The `scripts/deploy-gke.sh` script provides a "one-shot" interactive experience to set up a
production-ready Family Assistant instance on GKE. It automates the following:

1. **GKE Cluster**: Creates a GKE Autopilot cluster (recommended for most users).
2. **PostgreSQL**:
   - **Cloud SQL (Recommended)**: Provisions a managed Google Cloud SQL for PostgreSQL instance.
     Includes automated backups, high availability, and secure access via Workload Identity.
   - **In-Cluster**: Deploys a StatefulSet running PostgreSQL 16 with the `pgvector` extension.
3. **Application**: Deploys the Family Assistant container with all necessary environment variables
   and secrets.
4. **Networking**:
   - **With Domain**: Sets up a Static IP, Managed SSL Certificate (Google-managed), and GCE
     Ingress.
   - **Without Domain**: Sets up a standard LoadBalancer for quick access (unsecured).
5. **Authentication**: Optionally configures OpenID Connect (OIDC) with an email allowlist.

## Prerequisites

- **Google Cloud Project**: You must have an active GCP project with billing enabled.
- **Google Cloud SDK (`gcloud`)**: Installed and authenticated (`gcloud auth login`).
- **Kubernetes CLI (`kubectl`)**: Installed.
- **Telegram Bot Token**: From [@BotFather](https://t.me/botfather).
- **LLM API Key**: Either a Gemini API Key or an OpenRouter API Key.

## Quick Start

The easiest way to deploy is to run the script and follow the interactive prompts:

```bash
./scripts/deploy-gke.sh
```

The script will ask for:

- **Project ID**: Your GCP project ID.
- **Telegram Token**: Your bot's API token.
- **LLM Key**: Your Gemini or OpenRouter key.
- **Allowed Users**: Comma-separated list of Telegram user IDs authorized to use the bot.
- **Database Choice**: Prompts to use managed Cloud SQL instead of in-cluster Postgres.
- **OIDC Configuration**: (Optional) Prompts to enable OIDC and provide client details.

## Managed Database (Cloud SQL)

For a production-grade setup with "all the trimmings," we recommend using **Cloud SQL**.

- **Benefits**: Automatic patching, backups, 99.95% availability, and vertical scaling.
- **Security**: The script configures **Workload Identity**, allowing the application to connect
  securely to the database without managing raw credentials for the proxy.
- **Cost**: Cloud SQL has a minimum monthly cost (approx. $10-15/mo for a micro instance).
- **Setup Time**: Creating a Cloud SQL instance can take **10 to 15 minutes**.

To enable Cloud SQL via command line:

```bash
./scripts/deploy-gke.sh --use-cloud-sql --sql-tier db-f1-micro ...
```

## OpenID Connect (OIDC) Authentication

You can enable OIDC to secure the Web UI. Google is the recommended provider.

1. **Create OIDC Credentials**: In the Google Cloud Console, go to **APIs & Services > Credentials**
   and create an **OAuth client ID** for a "Web application".
2. **Authorized Redirect URIs**: If you have a domain, use `https://your-domain.com/auth`.
3. **Email Allowlist**: When prompted by the script, you can provide a comma-separated list of
   authorized emails (e.g., `user1@gmail.com, user2@example.com`). Only these users will be able to
   log in.

## Command Line Options

You can also provide all parameters via command line flags to skip the interactive prompts:

```bash
./scripts/deploy-gke.sh \
  --project my-project-id \
  --telegram-token "123456:ABC-DEF" \
  --gemini-key "your-gemini-key" \
  --allowed-users "12345678,87654321" \
  --domain "assistant.example.com" \
  --use-cloud-sql \
  --oidc-client-id "your-client-id" \
  --oidc-client-secret "your-client-secret" \
  --oidc-discovery "https://accounts.google.com/.well-known/openid-configuration" \
  --allowed-emails "your-email@gmail.com"
```

### All Available Options

| Option                 | Description                                        | Default                    |
| ---------------------- | -------------------------------------------------- | -------------------------- |
| `--project`            | GCP Project ID (required)                          | Current gcloud project     |
| `--region`             | GCP Region for the cluster and resources           | `us-central1`              |
| `--cluster`            | Name of the GKE cluster                            | `family-assistant-cluster` |
| `--namespace`          | Kubernetes namespace for deployment                | `family-assistant`         |
| `--image`              | Container image to deploy                          | GHCR latest                |
| `--domain`             | Domain name for HTTPS access (requires DNS config) | None (uses LoadBalancer)   |
| `--telegram-token`     | Telegram Bot API Token                             | Prompted                   |
| `--gemini-key`         | Google Gemini API Key                              | Prompted                   |
| `--openrouter-key`     | OpenRouter API Key                                 | Prompted                   |
| `--allowed-users`      | Comma-separated list of authorized Telegram IDs    | Prompted                   |
| `--dev-chat-id`        | Telegram ID for receiving system error logs        | Prompted                   |
| `--timezone`           | Timezone for date/time operations                  | `UTC` (or prompted)        |
| `--db-password`        | Password for the PostgreSQL database               | Randomly generated         |
| `--use-cloud-sql`      | Use managed Google Cloud SQL instead of in-cluster | `false`                    |
| `--sql-instance`       | Name for the Cloud SQL instance                    | `family-assistant-db`      |
| `--sql-tier`           | Machine type for Cloud SQL                         | `db-f1-micro`              |
| `--oidc-client-id`     | OIDC Client ID                                     | Prompted                   |
| `--oidc-client-secret` | OIDC Client Secret                                 | Prompted                   |
| `--oidc-discovery`     | OIDC Discovery URL                                 | Prompted                   |
| `--allowed-emails`     | Comma-separated allowed OIDC Emails                | Prompted                   |
| `--session-secret`     | Secret key for sessions                            | Randomly generated         |

## Post-Deployment

### If using a Domain

1. **Configure DNS**: The script will output a static IP address. Create an **A record** in your DNS
   provider pointing your domain to this IP.
2. **Wait for SSL**: Google-managed certificates can take **30 to 60 minutes** to become active
   after the DNS is configured.
3. **Authorized Redirect URI**: Ensure your OIDC provider has `https://<domain>/auth` in its allowed
   callback list.

### If NOT using a Domain

The script will set up a LoadBalancer. Access the application via the provided IP:
`http://<EXTERNAL_IP>`.

**Note**: OIDC typically requires HTTPS. Many providers (including Google) will not allow callbacks
to an `http` URL except for `localhost`.

## Maintenance & Operations

### Viewing Logs

```bash
kubectl logs -f deployment/family-assistant -c family-assistant -n family-assistant
```

### Updating the Application

To update to a new image:

```bash
kubectl set image deployment/family-assistant family-assistant=ghcr.io/werdnum/family-assistant:new-tag -n family-assistant
```

### Accessing the Database

- **Cloud SQL**: Use the Google Cloud Console or `gcloud sql connect`.
- **In-Cluster**:
  `kubectl exec -it statefulset/postgres -n family-assistant -- psql -U postgres -d family_assistant`

## Troubleshooting

- **Cluster Creation Fails**: Ensure you have sufficient quota in your GCP project.
- **Cloud SQL Creation Fails**: Check the Cloud SQL Admin API and billing status.
- **403 Access Denied (OIDC)**: Ensure your email is in the `ALLOWED_OIDC_EMAILS` list.
- **SSL Certificate Pending**: Ensure your A record is correctly pointed to the Static IP.

# Production Deployment Guide

This guide covers deploying Family Assistant to a production Kubernetes environment.

## Prerequisites

### Required Infrastructure

- **Kubernetes Cluster**: Version 1.24 or later recommended
- **PostgreSQL Database**: With the pgvector extension for vector search functionality
  - The application uses `postgresql+asyncpg://` connection strings
  - pgvector is required for semantic search and document indexing
- **Container Registry Access**: Images are available from GitHub Container Registry (ghcr.io)
- **Ingress Controller**: nginx-ingress recommended (used in example manifests)
- **TLS Certificate Management**: cert-manager with Let's Encrypt or similar (optional but
  recommended)

### Required Tools

- `kubectl` - Kubernetes command-line tool
- `git` - For cloning the repository and accessing manifests

### External Service Accounts

The following external services may be required depending on enabled features:

| Service              | Purpose                  | Required | Environment Variable   |
| -------------------- | ------------------------ | -------- | ---------------------- |
| OpenRouter           | LLM API access           | Yes      | `OPENROUTER_API_KEY`   |
| Telegram Bot         | Telegram interface       | Yes      | `TELEGRAM_BOT_TOKEN`   |
| CalDAV Server        | Calendar integration     | No       | `CALDAV_USERNAME`, etc |
| Brave Search         | Web search functionality | No       | `BRAVE_API_KEY`        |
| VAPID Keys           | Push notifications       | No       | `VAPID_PRIVATE_KEY`    |
| Google Maps (future) | Location services        | No       | -                      |

## Container Images

### Image Registry

Container images are published to GitHub Container Registry:

```
ghcr.io/<owner>/<repo>:latest
ghcr.io/<owner>/<repo>:<tag>
ghcr.io/<owner>/<repo>:main
```

### Image Tagging Convention

- **`latest`** - Most recent build from main branch
- **`main`** - Alias for latest main branch build
- **`YYYYMMDD_HHMMSS`** - Timestamp-based tags for specific builds
- **`<custom-tag>`** - Custom tags from workflow_dispatch builds

### Multi-Architecture Support

Images are built for multiple architectures:

- `linux/amd64` - Standard x86_64 servers
- `linux/arm64` - ARM64 servers (e.g., AWS Graviton, Apple Silicon)

Docker/Kubernetes will automatically pull the correct architecture.

### Pulling Images

For private registries, create an image pull secret:

```bash
kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=<github-username> \
  --docker-password=<github-pat> \
  --docker-email=<email> \
  -n family-assistant
```

## Step-by-Step Deployment

### 1. Create Namespace

```bash
kubectl create namespace family-assistant
```

Or apply a namespace manifest:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: family-assistant
```

### 2. Configure Secrets

Create a secrets file based on the template in `deploy/secrets.yaml`. The secrets file contains
sensitive configuration:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: family-assistant
  namespace: family-assistant
type: Opaque
stringData:
  # Required
  TELEGRAM_BOT_TOKEN: 'your-telegram-bot-token'
  OPENROUTER_API_KEY: 'your-openrouter-api-key'
  ALLOWED_CHAT_IDS: '123456789' # Comma-separated Telegram chat IDs
  DEVELOPER_CHAT_ID: '123456789' # For error notifications

  # CalDAV (optional)
  CALDAV_USERNAME: 'your-caldav-username'
  CALDAV_PASSWORD: 'your-caldav-password'
  CALDAV_CALENDAR_URLS: 'https://caldav.example.com/calendar1'

  # iCalendar feeds (optional)
  ICAL_URLS: 'https://example.com/calendar.ics'

  # Search (optional)
  BRAVE_API_KEY: 'your-brave-api-key'

  # Push notifications (optional)
  VAPID_PRIVATE_KEY: 'your-vapid-private-key'
  VAPID_CONTACT_EMAIL: 'mailto:admin@example.com'
```

Apply the secrets:

```bash
kubectl apply -f secrets.yaml -n family-assistant
```

### 3. Configure Database Connection

The deployment expects PostgreSQL credentials from a Kubernetes secret. The example in
`deploy/deployment.yaml` uses Zalando Postgres Operator secrets:

```yaml
env:
  - name: POSTGRES_USER
    valueFrom:
      secretKeyRef:
        name: your-postgres-credentials-secret
        key: username
  - name: POSTGRES_PASSWORD
    valueFrom:
      secretKeyRef:
        name: your-postgres-credentials-secret
        key: password
  - name: DATABASE_URL
    value: 'postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@postgres-host:5432/dbname'
```

Modify the deployment to match your PostgreSQL setup.

### 4. Apply Manifests

The deployment manifests are in the `deploy/` directory:

```bash
# Apply all manifests
kubectl apply -f deploy/ -n family-assistant
```

Or apply individually:

```bash
kubectl apply -f deploy/secrets.yaml -n family-assistant
kubectl apply -f deploy/deployment.yaml -n family-assistant
kubectl apply -f deploy/service.yaml -n family-assistant
kubectl apply -f deploy/ingress-web.yaml -n family-assistant  # Optional: web ingress
kubectl apply -f deploy/webhooks-ingress.yaml -n family-assistant  # Optional: webhook ingress
```

### 5. Verify Deployment

Check pod status:

```bash
kubectl get pods -n family-assistant
```

Expected output:

```
NAME                                READY   STATUS    RESTARTS   AGE
family-assistant-xxxxxxxxx-xxxxx    1/1     Running   0          1m
```

Check logs:

```bash
kubectl logs -f deployment/family-assistant -n family-assistant
```

Test connectivity:

```bash
kubectl port-forward deployment/family-assistant 8000:8000 -n family-assistant
curl http://localhost:8000/health
```

## Configuration

### Essential Environment Variables

| Variable             | Description                          | Required |
| -------------------- | ------------------------------------ | -------- |
| `DATABASE_URL`       | PostgreSQL connection string         | Yes      |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather    | Yes      |
| `OPENROUTER_API_KEY` | OpenRouter API key for LLM access    | Yes      |
| `ALLOWED_CHAT_IDS`   | Comma-separated allowed Telegram IDs | Yes      |
| `DEVELOPER_CHAT_ID`  | Chat ID for error notifications      | Yes      |
| `TIMEZONE`           | Timezone for date/time operations    | No       |

### Additional Configuration

For a complete list of configuration options, see the environment variables section in
[AGENTS.md](../../AGENTS.md).

VAPID keys for push notifications can be generated using:

```bash
python scripts/generate_vapid_keys.py
```

## Validation

### Health Check Endpoints

The application exposes a health check endpoint at `/health`:

```bash
curl https://your-domain.com/health
```

Response:

```json
{ "status": "ok", "reason": "Telegram polling active" }
```

Status codes:

- `200` - Service is healthy
- `503` - Service is unhealthy (check `reason` field for details)

### Common Health States

| Status         | Reason                        | Action                                  |
| -------------- | ----------------------------- | --------------------------------------- |
| `ok`           | Telegram polling active       | None - service is healthy               |
| `healthy`      | Web service running           | Telegram may be intentionally disabled  |
| `initializing` | Telegram service initializing | Wait for startup to complete            |
| `unhealthy`    | Telegram polling stopped      | Check logs for errors, may need restart |
| `unhealthy`    | Conflict error                | Another bot instance is running         |

### Kubernetes Probes

The deployment includes liveness probes:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 15
  periodSeconds: 20
  timeoutSeconds: 5
  failureThreshold: 3
```

### Common Issues and Solutions

| Issue                      | Possible Cause                    | Solution                                                           |
| -------------------------- | --------------------------------- | ------------------------------------------------------------------ |
| Pod in CrashLoopBackOff    | Missing secrets or invalid config | Check logs, verify all required secrets are set                    |
| Health check returns 503   | Telegram conflict error           | Ensure only one instance is running, check for other bot instances |
| Database connection errors | Wrong DATABASE_URL or credentials | Verify PostgreSQL is accessible and credentials are correct        |
| pgvector errors            | Extension not installed           | Run `CREATE EXTENSION vector;` in the database                     |
| Image pull errors          | Registry auth issues              | Create imagePullSecrets or verify registry access                  |

## Rollback Procedures

### Rolling Back a Deployment

To roll back to a previous deployment:

```bash
# View rollout history
kubectl rollout history deployment/family-assistant -n family-assistant

# Roll back to previous revision
kubectl rollout undo deployment/family-assistant -n family-assistant

# Roll back to specific revision
kubectl rollout undo deployment/family-assistant --to-revision=2 -n family-assistant
```

### Checking Rollout Status

```bash
kubectl rollout status deployment/family-assistant -n family-assistant
```

### Database Migration Considerations

Family Assistant uses Alembic for database migrations. Migrations run automatically on application
startup.

**Before rolling back:**

1. Check if the new version includes database migrations:

   ```bash
   kubectl logs deployment/family-assistant -n family-assistant | grep -i alembic
   ```

2. If migrations were applied, consider whether they are backwards-compatible

3. For non-backwards-compatible migrations, you may need to restore from a database backup

**Rolling back migrations manually:**

```bash
# Connect to a pod or run migration container
kubectl exec -it deployment/family-assistant -n family-assistant -- \
  alembic downgrade -1
```

**Best Practice:** Always take a database backup before deploying versions with migrations:

```bash
pg_dump -h <host> -U <user> -d <database> > backup-$(date +%Y%m%d_%H%M%S).sql
```

## Next Steps

After successful deployment:

1. **Monitoring**: Set up log aggregation and monitoring for the deployment

   - Monitor pod restarts and resource usage (CPU/Memory)
   - Set up alerts for unhealthy pods
   - Consider Prometheus metrics if available

2. **Backups**: Establish regular database backup procedures

   - PostgreSQL data should be backed up regularly
   - Consider automated backup solutions (e.g., Velero, pg_dump cron jobs)

3. **Updates**: The CI/CD pipeline automatically updates the kube-config repository when new images
   are built. If using GitOps (e.g., ArgoCD, Flux), deployments will auto-sync.

4. **Security Review**: Refer to the security considerations in
   [.devcontainer/k8s/PRODUCTION_CHECKLIST.md](../../.devcontainer/k8s/PRODUCTION_CHECKLIST.md):

   - NetworkPolicy restricts egress appropriately
   - Pod runs as non-root user
   - Secrets stored in Kubernetes secrets (not in images)
   - No unnecessary host network access

## Related Documentation

- [AGENTS.md](../../AGENTS.md) - Development setup and environment variables
- [.devcontainer/k8s/PRODUCTION_CHECKLIST.md](../../.devcontainer/k8s/PRODUCTION_CHECKLIST.md) -
  Production readiness checklist
- [docs/ALEMBIC_MIGRATION_GUIDE.md](../ALEMBIC_MIGRATION_GUIDE.md) - Database migration guide
- [docs/architecture-diagram.md](../architecture-diagram.md) - System architecture overview

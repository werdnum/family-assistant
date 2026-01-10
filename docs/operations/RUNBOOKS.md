# Operational Runbooks

This document provides step-by-step procedures for common operational tasks when running Family
Assistant in production.

______________________________________________________________________

## Table of Contents

1. [Service Management](#1-service-management)
2. [Database Operations](#2-database-operations)
3. [User Management](#3-user-management)
4. [Secret Management](#4-secret-management)
5. [Cache and Storage](#5-cache-and-storage)
6. [Certificate Management](#6-certificate-management)
7. [Troubleshooting Procedures](#7-troubleshooting-procedures)
8. [Emergency Procedures](#8-emergency-procedures)

______________________________________________________________________

## 1. Service Management

### 1.1 Starting the Service

#### Kubernetes

```bash
# Scale deployment to 1 replica
kubectl scale deployment family-assistant --replicas=1 -n family-assistant

# Verify pods are starting
kubectl get pods -n family-assistant -w
```

#### Docker Compose

```bash
docker-compose up -d family-assistant
```

#### Local Development

```bash
# Using poethepoet
poe dev

# Or direct Python invocation
python -m family_assistant
```

### 1.2 Stopping the Service

#### Kubernetes

```bash
# Scale down to 0 replicas (graceful shutdown)
kubectl scale deployment family-assistant --replicas=0 -n family-assistant

# Verify pods are terminated
kubectl get pods -n family-assistant
```

**Note**: Scaling to 0 is preferred over deleting the deployment for planned maintenance.

#### Docker Compose

```bash
docker-compose stop family-assistant
```

### 1.3 Restarting the Service

#### Kubernetes

```bash
# Rolling restart (zero-downtime if configured correctly)
kubectl rollout restart deployment/family-assistant -n family-assistant

# Monitor the rollout
kubectl rollout status deployment/family-assistant -n family-assistant
```

#### Docker Compose

```bash
docker-compose restart family-assistant
```

### 1.4 Checking Service Status

#### Quick Health Check

```bash
# Check the health endpoint
curl -s https://your-domain.com/health | jq .

# Expected response:
# {"status": "ok", "reason": "Telegram polling active"}
```

#### Kubernetes Status

```bash
# Check pod status
kubectl get pods -n family-assistant

# Check deployment status
kubectl describe deployment family-assistant -n family-assistant

# Check recent events
kubectl get events -n family-assistant --sort-by='.lastTimestamp' | tail -20
```

#### View Logs

```bash
# Stream logs from all pods
kubectl logs -f deployment/family-assistant -n family-assistant

# Get logs from the last hour
kubectl logs deployment/family-assistant -n family-assistant --since=1h

# Get logs from a specific pod
kubectl logs <pod-name> -n family-assistant
```

### 1.5 Updating the Service

#### Deploy New Version

```bash
# Update the image tag in deployment
kubectl set image deployment/family-assistant \
  family-assistant=ghcr.io/<owner>/family-assistant:<new-tag> \
  -n family-assistant

# Or edit the deployment directly
kubectl edit deployment family-assistant -n family-assistant

# Monitor rollout
kubectl rollout status deployment/family-assistant -n family-assistant
```

#### Verify Update

```bash
# Check the running image
kubectl get deployment family-assistant -n family-assistant -o jsonpath='{.spec.template.spec.containers[0].image}'

# Verify health after update
curl -s https://your-domain.com/health
```

______________________________________________________________________

## 2. Database Operations

### 2.1 Running Migrations

Migrations run automatically on application startup. For manual control:

#### Check Current Migration Status

```bash
# Connect to the pod
kubectl exec -it deployment/family-assistant -n family-assistant -- bash

# Check current revision
alembic current

# Check available heads
alembic heads
```

#### Apply Pending Migrations

```bash
# Apply all pending migrations
kubectl exec -it deployment/family-assistant -n family-assistant -- \
  alembic upgrade head
```

#### View Migration History

```bash
kubectl exec -it deployment/family-assistant -n family-assistant -- \
  alembic history --verbose
```

### 2.2 Rolling Back Migrations

**WARNING**: Always take a database backup before rolling back migrations.

#### Rollback One Revision

```bash
kubectl exec -it deployment/family-assistant -n family-assistant -- \
  alembic downgrade -1
```

#### Rollback to Specific Revision

```bash
# First, check history to find the target revision
kubectl exec -it deployment/family-assistant -n family-assistant -- \
  alembic history

# Rollback to specific revision
kubectl exec -it deployment/family-assistant -n family-assistant -- \
  alembic downgrade <revision-id>
```

#### Resolving Migration Conflicts

If you encounter "multiple heads" or "can't locate revision" errors:

```bash
# Stamp the database to the new base migration
alembic stamp 6daf0237b0ba

# Then upgrade to head
alembic upgrade head
```

See [ALEMBIC_MIGRATION_GUIDE.md](../ALEMBIC_MIGRATION_GUIDE.md) for detailed migration
troubleshooting.

### 2.3 Vacuum and Reindex

#### PostgreSQL Maintenance

```bash
# Connect to PostgreSQL
kubectl exec -it <postgres-pod> -n postgres -- psql -U postgres -d mlbot

# Vacuum analyze all tables
VACUUM ANALYZE;

# Vacuum specific table
VACUUM ANALYZE notes;

# Reindex database
REINDEX DATABASE mlbot;

# Reindex specific table
REINDEX TABLE notes;
```

#### Scheduled Maintenance

PostgreSQL autovacuum handles routine maintenance, but for large operations:

```bash
# Full vacuum (reclaims space, requires exclusive lock)
VACUUM FULL notes;

# Note: VACUUM FULL locks the table, use during maintenance windows only
```

### 2.4 Connection Pool Management

The application uses asyncpg with connection pooling. Monitor connections:

```bash
# Check active connections
kubectl exec -it <postgres-pod> -n postgres -- psql -U postgres -d mlbot -c \
  "SELECT count(*) as connections, state FROM pg_stat_activity WHERE datname='mlbot' GROUP BY state;"

# Kill idle connections older than 1 hour
kubectl exec -it <postgres-pod> -n postgres -- psql -U postgres -d mlbot -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='mlbot' AND state='idle' AND query_start < now() - interval '1 hour';"
```

#### Troubleshooting Connection Issues

If the application reports connection errors:

1. Check the pod is healthy: `kubectl get pods -n family-assistant`
2. Check database connectivity from pod:
   ```bash
   kubectl exec -it deployment/family-assistant -n family-assistant -- \
     python -c "import asyncio; import asyncpg; asyncio.run(asyncpg.connect('postgres://...'))"
   ```
3. Check PostgreSQL max_connections setting
4. Restart the application pod if connections are exhausted

### 2.5 Database Backup (Quick Reference)

```bash
# Full backup
pg_dump -h <hostname> -p 5432 -U <username> -d <database> \
    --format=custom \
    --compress=9 \
    --file=family_assistant_$(date +%Y%m%d_%H%M%S).dump
```

See [BACKUP_RECOVERY.md](./BACKUP_RECOVERY.md) for comprehensive backup procedures.

______________________________________________________________________

## 3. User Management

### 3.1 Adding Authorized Users

Users are authorized via Telegram chat IDs in the `ALLOWED_CHAT_IDS` environment variable.

#### Find a User's Chat ID

1. The user sends a message to the bot
2. Check the logs for the incoming message:
   ```bash
   kubectl logs deployment/family-assistant -n family-assistant | grep "chat_id"
   ```
3. Note the chat ID from the log entry

#### Update Allowed Users

##### Kubernetes Secret Update

```bash
# Get current secret
kubectl get secret family-assistant -n family-assistant -o yaml > secret-backup.yaml

# Edit secret (base64 decode/encode as needed)
kubectl edit secret family-assistant -n family-assistant

# Or patch the secret
kubectl patch secret family-assistant -n family-assistant --type='json' \
  -p='[{"op": "replace", "path": "/stringData/ALLOWED_CHAT_IDS", "value": "123456789,987654321"}]'

# Restart pod to pick up changes
kubectl rollout restart deployment/family-assistant -n family-assistant
```

##### Alternative: Update via Environment Variable

If using `ALLOWED_USER_IDS` (alias for `ALLOWED_CHAT_IDS`):

```bash
kubectl set env deployment/family-assistant \
  ALLOWED_USER_IDS="123456789,987654321" \
  -n family-assistant
```

### 3.2 Removing Users

Follow the same procedure as adding users, but remove the chat ID from the comma-separated list.

### 3.3 Checking User Access

```bash
# Check current allowed users
kubectl get secret family-assistant -n family-assistant \
  -o jsonpath='{.data.ALLOWED_CHAT_IDS}' | base64 -d

# Or check the environment in the pod
kubectl exec deployment/family-assistant -n family-assistant -- \
  printenv | grep -E "(ALLOWED_CHAT_IDS|ALLOWED_USER_IDS)"
```

### 3.4 Managing Chat ID to Name Mapping

The `CHAT_ID_TO_NAME_MAP` provides human-readable names for users:

```bash
# Format: chat_id:name,chat_id:name
kubectl patch secret family-assistant -n family-assistant --type='json' \
  -p='[{"op": "replace", "path": "/stringData/CHAT_ID_TO_NAME_MAP", "value": "123456789:Alice,987654321:Bob"}]'

kubectl rollout restart deployment/family-assistant -n family-assistant
```

______________________________________________________________________

## 4. Secret Management

### 4.1 Rotating API Keys

#### OpenRouter API Key

```bash
# Update the secret
kubectl patch secret family-assistant -n family-assistant --type='json' \
  -p='[{"op": "replace", "path": "/stringData/OPENROUTER_API_KEY", "value": "sk-or-v1-new-key-here"}]'

# Restart to apply
kubectl rollout restart deployment/family-assistant -n family-assistant

# Verify the service is working
curl -s https://your-domain.com/health
```

#### Other LLM Provider Keys

Same procedure for:

- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`

### 4.2 Updating Telegram Token

**CRITICAL**: Only one bot instance can use a token at a time.

```bash
# 1. Stop the current deployment
kubectl scale deployment family-assistant --replicas=0 -n family-assistant

# 2. Update the token
kubectl patch secret family-assistant -n family-assistant --type='json' \
  -p='[{"op": "replace", "path": "/stringData/TELEGRAM_BOT_TOKEN", "value": "NEW_TOKEN_HERE"}]'

# 3. Start the deployment
kubectl scale deployment family-assistant --replicas=1 -n family-assistant

# 4. Verify health
curl -s https://your-domain.com/health
```

**If you see "Conflict" errors**: Another instance is using the token. Ensure all old instances are
stopped.

### 4.3 Rotating VAPID Keys

VAPID keys are used for push notifications. Rotating them will invalidate all existing push
subscriptions.

#### Generate New Keys

```bash
# Run the key generation script
python scripts/generate_vapid_keys.py

# Output:
# VAPID_PRIVATE_KEY=<new-private-key>
# VAPID_PUBLIC_KEY=<new-public-key>
```

#### Apply New Keys

```bash
# Update secrets
kubectl patch secret family-assistant -n family-assistant --type='json' \
  -p='[
    {"op": "replace", "path": "/stringData/VAPID_PRIVATE_KEY", "value": "<new-private-key>"},
    {"op": "replace", "path": "/stringData/VAPID_PUBLIC_KEY", "value": "<new-public-key>"}
  ]'

# Restart
kubectl rollout restart deployment/family-assistant -n family-assistant
```

**Note**: After rotating VAPID keys, users must re-subscribe to push notifications through the web
UI.

### 4.4 Rotating CalDAV Credentials

```bash
kubectl patch secret family-assistant -n family-assistant --type='json' \
  -p='[
    {"op": "replace", "path": "/stringData/CALDAV_USERNAME", "value": "new-username"},
    {"op": "replace", "path": "/stringData/CALDAV_PASSWORD", "value": "new-password"}
  ]'

kubectl rollout restart deployment/family-assistant -n family-assistant
```

### 4.5 Viewing Current Secrets

```bash
# List all secret keys
kubectl get secret family-assistant -n family-assistant -o jsonpath='{.data}' | jq 'keys'

# View a specific secret value (base64 decode)
kubectl get secret family-assistant -n family-assistant \
  -o jsonpath='{.data.TELEGRAM_BOT_TOKEN}' | base64 -d
```

### 4.6 Backing Up Secrets

```bash
# Export secrets (WARNING: contains sensitive data)
kubectl get secret family-assistant -n family-assistant -o yaml > secrets-backup.yaml

# Encrypt the backup
gpg --symmetric --cipher-algo AES256 secrets-backup.yaml
rm secrets-backup.yaml
```

______________________________________________________________________

## 5. Cache and Storage

### 5.1 Clearing Caches

The application uses in-memory caching. Restarting the pod clears all caches:

```bash
kubectl rollout restart deployment/family-assistant -n family-assistant
```

### 5.2 Managing Document Storage

#### Check Storage Usage

```bash
# Connect to pod and check storage
kubectl exec deployment/family-assistant -n family-assistant -- \
  du -sh /mnt/data/files

# List documents
kubectl exec deployment/family-assistant -n family-assistant -- \
  ls -la /mnt/data/files
```

#### Clean Up Old Documents

```bash
# Find documents older than 90 days
kubectl exec deployment/family-assistant -n family-assistant -- \
  find /mnt/data/files -type f -mtime +90 -ls

# Delete old documents (use with caution)
kubectl exec deployment/family-assistant -n family-assistant -- \
  find /mnt/data/files -type f -mtime +90 -delete
```

### 5.3 Managing Email Attachments

```bash
# Check attachment storage
kubectl exec deployment/family-assistant -n family-assistant -- \
  du -sh /mnt/data/mailbox/attachments

# List recent attachments
kubectl exec deployment/family-assistant -n family-assistant -- \
  ls -lat /mnt/data/mailbox/attachments | head -20
```

### 5.4 Checking Storage Usage

```bash
# Get overall disk usage
kubectl exec deployment/family-assistant -n family-assistant -- df -h

# Detailed storage breakdown
kubectl exec deployment/family-assistant -n family-assistant -- \
  du -sh /mnt/data/* 2>/dev/null || echo "Storage paths may vary by configuration"
```

### 5.5 Persistent Volume Management

#### Check PV/PVC Status

```bash
# List PVCs
kubectl get pvc -n family-assistant

# Describe PVC for details
kubectl describe pvc <pvc-name> -n family-assistant
```

#### Expand Storage (if supported)

```bash
# Edit PVC to request more storage
kubectl patch pvc <pvc-name> -n family-assistant \
  -p '{"spec":{"resources":{"requests":{"storage":"20Gi"}}}}'
```

______________________________________________________________________

## 6. Certificate Management

### 6.1 Cert-Manager Renewal

Cert-manager handles automatic certificate renewal. Monitor certificate status:

```bash
# Check certificate status
kubectl get certificates -n family-assistant

# Describe certificate for details
kubectl describe certificate family-assistant-web-tls -n family-assistant

# Check cert-manager logs if issues
kubectl logs -n cert-manager deployment/cert-manager
```

#### Force Certificate Renewal

```bash
# Delete the certificate secret (cert-manager will recreate)
kubectl delete secret family-assistant-web-tls -n family-assistant

# Cert-manager will automatically request a new certificate
# Monitor the certificate resource
kubectl get certificates -n family-assistant -w
```

### 6.2 Manual Certificate Updates

If not using cert-manager:

```bash
# Create a TLS secret from certificate files
kubectl create secret tls family-assistant-web-tls \
  --cert=path/to/tls.crt \
  --key=path/to/tls.key \
  -n family-assistant

# Or update existing secret
kubectl create secret tls family-assistant-web-tls \
  --cert=path/to/tls.crt \
  --key=path/to/tls.key \
  -n family-assistant \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 6.3 Checking Certificate Expiry

```bash
# Check certificate expiry from the cluster
kubectl get secret family-assistant-web-tls -n family-assistant \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | \
  openssl x509 -noout -dates

# Check certificate from the endpoint
echo | openssl s_client -servername your-domain.com -connect your-domain.com:443 2>/dev/null | \
  openssl x509 -noout -dates
```

______________________________________________________________________

## 7. Troubleshooting Procedures

### 7.1 Investigating Slow Responses

#### Check LLM Response Times

```bash
# Look for LLM timing in logs
kubectl logs deployment/family-assistant -n family-assistant --since=1h | \
  grep -E "(LLM|latency|duration|timeout)"
```

#### Enable Debug Logging

```bash
# Temporarily enable LLM debug mode
kubectl set env deployment/family-assistant \
  DEBUG_LLM_MESSAGES=true \
  LITELLM_DEBUG=true \
  -n family-assistant

# Watch logs
kubectl logs -f deployment/family-assistant -n family-assistant

# Disable after debugging
kubectl set env deployment/family-assistant \
  DEBUG_LLM_MESSAGES- \
  LITELLM_DEBUG- \
  -n family-assistant
```

#### Check Resource Utilization

```bash
# Check pod resource usage
kubectl top pods -n family-assistant

# Check node resources
kubectl top nodes
```

### 7.2 Debugging Telegram Issues

#### Check Telegram Health

```bash
# Check health endpoint reason
curl -s https://your-domain.com/health | jq .

# Possible statuses:
# - "ok" + "Telegram polling active" = healthy
# - "unhealthy" + "Telegram polling stopped" = bot disconnected
# - "unhealthy" + "Conflict" = another instance using token
```

#### Telegram Conflict Resolution

If you see "Conflict" errors:

1. Check for duplicate deployments:
   ```bash
   kubectl get pods -A | grep family-assistant
   ```
2. Ensure only one instance is running
3. If using multiple environments, ensure each has a unique bot token

#### Check Telegram Logs

```bash
kubectl logs deployment/family-assistant -n family-assistant | \
  grep -E "(telegram|polling|Conflict|bot)"
```

### 7.3 Checking LLM Connectivity

#### Test LLM Provider Connection

```bash
# Check for LLM errors in logs
kubectl logs deployment/family-assistant -n family-assistant | \
  grep -E "(LLM|OpenRouter|Gemini|OpenAI|Anthropic|rate.limit|quota)"
```

#### Verify API Key

```bash
# Test OpenRouter connectivity (from a pod or local machine)
curl -s -X POST https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-3.5-turbo","messages":[{"role":"user","content":"test"}]}' | jq .
```

### 7.4 Database Connectivity Issues

#### Test Database Connection

```bash
# From the pod
kubectl exec deployment/family-assistant -n family-assistant -- \
  python -c "
import asyncio
import asyncpg
import os

async def test():
    url = os.environ.get('DATABASE_URL', '').replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(url)
    result = await conn.fetchval('SELECT 1')
    print(f'Connection successful: {result}')
    await conn.close()

asyncio.run(test())
"
```

#### Check Database Logs

```bash
# If using Zalando Postgres Operator
kubectl logs <postgres-pod> -n postgres

# Check for connection errors
kubectl logs deployment/family-assistant -n family-assistant | \
  grep -E "(database|connection|asyncpg|postgres)"
```

### 7.5 MCP Server Issues

#### Check MCP Initialization

```bash
kubectl logs deployment/family-assistant -n family-assistant | \
  grep -E "(MCP|mcp_server|initialization)"
```

#### Common MCP Issues

- **Timeout on initialization**: Increase `MCP_INITIALIZATION_TIMEOUT_SECONDS`
- **Environment variable expansion failed**: Check MCP config for `$VAR` references and ensure
  variables are set
- **Server failed to start**: Check if required binaries/dependencies are available

______________________________________________________________________

## 8. Emergency Procedures

### 8.1 Complete Service Restart

Use when the service is unresponsive or in an unknown state:

```bash
# 1. Scale down completely
kubectl scale deployment family-assistant --replicas=0 -n family-assistant

# 2. Wait for termination
kubectl wait --for=delete pod -l app=family-assistant -n family-assistant --timeout=60s

# 3. Scale back up
kubectl scale deployment family-assistant --replicas=1 -n family-assistant

# 4. Monitor startup
kubectl logs -f deployment/family-assistant -n family-assistant

# 5. Verify health
curl -s https://your-domain.com/health
```

### 8.2 Rollback to Previous Version

#### Quick Rollback

```bash
# Roll back to previous revision
kubectl rollout undo deployment/family-assistant -n family-assistant

# Monitor rollout
kubectl rollout status deployment/family-assistant -n family-assistant
```

#### Rollback to Specific Version

```bash
# View rollout history
kubectl rollout history deployment/family-assistant -n family-assistant

# Rollback to specific revision
kubectl rollout undo deployment/family-assistant --to-revision=<revision> -n family-assistant
```

#### Rollback with Database Considerations

If the problematic version included database migrations:

1. **Take a database backup immediately** (if not already done):
   ```bash
   pg_dump -h <host> -U <user> -d <database> --format=custom --compress=9 \
     --file=emergency_backup_$(date +%Y%m%d_%H%M%S).dump
   ```
2. Rollback the deployment
3. If needed, rollback the database migration:
   ```bash
   kubectl exec deployment/family-assistant -n family-assistant -- \
     alembic downgrade -1
   ```

### 8.3 Disaster Recovery Activation

For complete recovery from a catastrophic failure:

#### 1. Assess the Situation

```bash
# Check cluster health
kubectl get nodes
kubectl get pods -A

# Check if namespace exists
kubectl get namespace family-assistant
```

#### 2. Restore from Backup

See [BACKUP_RECOVERY.md](./BACKUP_RECOVERY.md) for detailed procedures.

**Quick database restore:**

```bash
# 1. Create database if needed
createdb -h <new-host> -U postgres family_assistant

# 2. Restore from backup
pg_restore -h <new-host> -U postgres -d family_assistant \
  --no-owner family_assistant_backup.dump

# 3. Run migrations
export DATABASE_URL="postgresql+asyncpg://user:pass@new-host:5432/family_assistant"
alembic upgrade head
```

#### 3. Redeploy Application

```bash
# Apply all manifests
kubectl apply -f deploy/ -n family-assistant

# Verify deployment
kubectl get pods -n family-assistant -w
```

#### 4. Update DNS/Ingress (if needed)

If the recovery is to a new cluster:

```bash
# Update DNS records to point to new ingress IP
# Or update load balancer configuration
```

#### 5. Verify Recovery

```bash
# Health check
curl -s https://your-domain.com/health

# Test Telegram bot
# Send a test message to the bot

# Test web UI
# Access the web interface and verify data

# Check logs for errors
kubectl logs deployment/family-assistant -n family-assistant --since=10m
```

### 8.4 Emergency Contacts and Escalation

Document your escalation path here:

| Issue Type         | First Responder | Escalation                 |
| ------------------ | --------------- | -------------------------- |
| Service down       | On-call SRE     | Platform team lead         |
| Database issues    | DBA on-call     | Database team lead         |
| Security incident  | Security team   | CISO                       |
| LLM provider issue | DevOps          | Provider support (if paid) |

### 8.5 Post-Incident Tasks

After resolving an incident:

1. **Document the incident**: What happened, when, impact, resolution
2. **Update runbooks**: Add any new procedures discovered
3. **Review monitoring**: Ensure alerts would catch similar issues
4. **Conduct post-mortem**: For significant incidents
5. **Update backups**: Ensure recent backup after recovery

______________________________________________________________________

## Related Documentation

- [CONFIGURATION_REFERENCE.md](./CONFIGURATION_REFERENCE.md) - Complete environment variable
  reference
- [MONITORING.md](./MONITORING.md) - Monitoring, logging, and alerting
- [BACKUP_RECOVERY.md](./BACKUP_RECOVERY.md) - Backup and disaster recovery procedures
- [ALEMBIC_MIGRATION_GUIDE.md](../ALEMBIC_MIGRATION_GUIDE.md) - Database migration procedures
- [PRODUCTION_DEPLOYMENT.md](../deployment/PRODUCTION_DEPLOYMENT.md) - Initial deployment guide

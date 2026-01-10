# Backup and Recovery Guide

This document provides comprehensive guidance for operators on backing up and restoring Family
Assistant data.

______________________________________________________________________

## Overview

Family Assistant stores data in several locations that require backup:

| Data Type           | Storage Location               | Backup Priority | Description                                     |
| ------------------- | ------------------------------ | --------------- | ----------------------------------------------- |
| PostgreSQL Database | PostgreSQL server              | **Critical**    | All application data (notes, tasks, messages)   |
| Document Storage    | `DOCUMENT_STORAGE_PATH`        | High            | Indexed documents and uploaded files            |
| Email Attachments   | `ATTACHMENT_STORAGE_PATH`      | High            | Email attachments received via webhooks         |
| Chat Attachments    | `CHAT_ATTACHMENT_STORAGE_PATH` | Medium          | Temporary chat attachments (can be regenerated) |
| Configuration       | `config.yaml`, `.env`          | **Critical**    | Application configuration and secrets           |

______________________________________________________________________

## Data Inventory

### PostgreSQL Database

The database contains all persistent application data:

| Table                  | Description                                        | Criticality |
| ---------------------- | -------------------------------------------------- | ----------- |
| `notes`                | User notes and knowledge base                      | Critical    |
| `tasks`                | Scheduled tasks, reminders, and callbacks          | Critical    |
| `message_history`      | Conversation history across all interfaces         | High        |
| `event_listeners`      | Event-based automation definitions                 | High        |
| `schedule_automations` | Schedule-based automation definitions              | High        |
| `received_emails`      | Stored emails received via webhooks                | High        |
| `documents`            | Document metadata for indexed content              | High        |
| `document_embeddings`  | Vector embeddings for semantic search (PostgreSQL) | Medium      |
| `api_tokens`           | API authentication tokens                          | High        |
| `push_subscriptions`   | PWA push notification subscriptions                | Medium      |
| `recent_events`        | Recent event data (short retention)                | Low         |
| `error_logs`           | Application error logs                             | Low         |
| `attachment_metadata`  | Metadata for attachments linked to messages        | Medium      |

### File Storage Paths

Default storage paths (configurable via environment variables):

| Path                           | Default Value                   | Contents                          |
| ------------------------------ | ------------------------------- | --------------------------------- |
| `DOCUMENT_STORAGE_PATH`        | `/mnt/data/files`               | Uploaded and indexed documents    |
| `ATTACHMENT_STORAGE_PATH`      | `/mnt/data/mailbox/attachments` | Email attachment files            |
| `CHAT_ATTACHMENT_STORAGE_PATH` | `/tmp/chat_attachments`         | Temporary chat uploads (volatile) |

### Configuration Files

| File              | Description                               | Contains Secrets |
| ----------------- | ----------------------------------------- | ---------------- |
| `config.yaml`     | Main application configuration            | Possibly         |
| `.env`            | Environment variables (secrets, API keys) | **Yes**          |
| `mcp_config.json` | MCP server definitions                    | Possibly         |
| `prompts.yaml`    | LLM prompt templates                      | No               |

______________________________________________________________________

## Database Backup

### Prerequisites

- PostgreSQL client tools (`pg_dump`, `psql`) installed
- Database connection credentials
- Sufficient disk space for backup files

### Full Database Backup

Use `pg_dump` to create a complete database backup:

```bash
# Basic backup with compression
pg_dump -h <hostname> -p 5432 -U <username> -d <database> \
    --format=custom \
    --compress=9 \
    --file=family_assistant_$(date +%Y%m%d_%H%M%S).dump

# Example with typical production settings
pg_dump -h storage-cluster.postgres.svc.cluster.local -p 5432 -U mlbot -d mlbot \
    --format=custom \
    --compress=9 \
    --file=family_assistant_$(date +%Y%m%d_%H%M%S).dump
```

### Backup Options Explained

| Option            | Purpose                                                |
| ----------------- | ------------------------------------------------------ |
| `--format=custom` | Enables compression and selective restore              |
| `--compress=9`    | Maximum compression (slower but smaller files)         |
| `--no-owner`      | Omit ownership commands (useful for cross-env restore) |
| `--clean`         | Include DROP statements before CREATE                  |

### Automated Backup Script

Create a backup script for scheduled execution:

```bash
#!/bin/bash
# backup_database.sh

# Configuration
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-postgres}"
DB_NAME="${DB_NAME:-family_assistant}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/family-assistant}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Generate backup filename with timestamp
BACKUP_FILE="$BACKUP_DIR/family_assistant_$(date +%Y%m%d_%H%M%S).dump"

# Create backup
echo "Creating backup: $BACKUP_FILE"
pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
    --format=custom \
    --compress=9 \
    --file="$BACKUP_FILE"

# Check backup success
if [ $? -eq 0 ]; then
    echo "Backup completed successfully: $BACKUP_FILE"
    echo "Backup size: $(du -h "$BACKUP_FILE" | cut -f1)"
else
    echo "ERROR: Backup failed!"
    exit 1
fi

# Remove old backups
echo "Removing backups older than $RETENTION_DAYS days..."
find "$BACKUP_DIR" -name "*.dump" -mtime +$RETENTION_DAYS -delete

echo "Backup complete."
```

### Scheduling Backups

#### Using cron

```bash
# Edit crontab
crontab -e

# Add daily backup at 2:00 AM
0 2 * * * /path/to/backup_database.sh >> /var/log/family-assistant-backup.log 2>&1
```

#### Using Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: family-assistant-backup
spec:
  schedule: "0 2 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: backup
            image: postgres:15
            command:
            - /bin/bash
            - -c
            - |
              pg_dump -h $DB_HOST -U $DB_USER -d $DB_NAME \
                --format=custom --compress=9 \
                --file=/backups/family_assistant_$(date +%Y%m%d_%H%M%S).dump
            env:
            - name: DB_HOST
              value: "storage-cluster.postgres.svc.cluster.local"
            - name: DB_USER
              valueFrom:
                secretKeyRef:
                  name: db-credentials
                  key: username
            - name: PGPASSWORD
              valueFrom:
                secretKeyRef:
                  name: db-credentials
                  key: password
            volumeMounts:
            - name: backup-storage
              mountPath: /backups
          restartPolicy: OnFailure
          volumes:
          - name: backup-storage
            persistentVolumeClaim:
              claimName: backup-pvc
```

### Backup Verification

Always verify backups can be restored:

```bash
# List backup contents
pg_restore --list family_assistant_backup.dump

# Test restore to a temporary database
createdb -h localhost -U postgres family_assistant_test
pg_restore -h localhost -U postgres -d family_assistant_test family_assistant_backup.dump

# Verify data integrity
psql -h localhost -U postgres -d family_assistant_test -c "SELECT COUNT(*) FROM notes;"
psql -h localhost -U postgres -d family_assistant_test -c "SELECT COUNT(*) FROM tasks;"
psql -h localhost -U postgres -d family_assistant_test -c "SELECT COUNT(*) FROM message_history;"

# Clean up test database
dropdb -h localhost -U postgres family_assistant_test
```

______________________________________________________________________

## Database Restore

### Full Restore Procedure

#### 1. Stop the Application

```bash
# Kubernetes
kubectl scale deployment family-assistant --replicas=0

# Docker Compose
docker-compose stop family-assistant

# Systemd
sudo systemctl stop family-assistant
```

#### 2. Prepare the Database

```bash
# Option A: Restore to existing database (will DROP and recreate objects)
# WARNING: This destroys existing data
pg_restore -h <hostname> -U <username> -d <database> \
    --clean --if-exists \
    family_assistant_backup.dump

# Option B: Restore to a new database
createdb -h <hostname> -U <username> family_assistant_restored
pg_restore -h <hostname> -U <username> -d family_assistant_restored \
    family_assistant_backup.dump
```

#### 3. Run Database Migrations

Ensure the schema is up to date after restore:

```bash
# Set the database URL
export DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/family_assistant"

# Run migrations
alembic upgrade head
```

If migration conflicts occur after restoring from an old backup, refer to
[ALEMBIC_MIGRATION_GUIDE.md](../ALEMBIC_MIGRATION_GUIDE.md) for resolving migration state issues.

#### 4. Verify Restoration

```bash
# Check table row counts
psql -h <hostname> -U <username> -d <database> << EOF
SELECT 'notes' as table_name, COUNT(*) as row_count FROM notes
UNION ALL
SELECT 'tasks', COUNT(*) FROM tasks
UNION ALL
SELECT 'message_history', COUNT(*) FROM message_history
UNION ALL
SELECT 'event_listeners', COUNT(*) FROM event_listeners;
EOF
```

#### 5. Start the Application

```bash
# Kubernetes
kubectl scale deployment family-assistant --replicas=1

# Docker Compose
docker-compose start family-assistant

# Systemd
sudo systemctl start family-assistant
```

#### 6. Post-Restore Verification

- Access the web UI and verify data is visible
- Check that automations are listed and enabled
- Verify notes and tasks appear correctly
- Test a simple operation (e.g., create a test note)

______________________________________________________________________

## Document Storage Backup

### File System Backup with rsync

```bash
# Backup documents to remote server
rsync -avz --delete \
    /mnt/data/files/ \
    backup-server:/backups/family-assistant/documents/

# Backup email attachments
rsync -avz --delete \
    /mnt/data/mailbox/attachments/ \
    backup-server:/backups/family-assistant/attachments/
```

### Backup Script for Files

```bash
#!/bin/bash
# backup_files.sh

SOURCE_DOCS="${DOCUMENT_STORAGE_PATH:-/mnt/data/files}"
SOURCE_ATTACHMENTS="${ATTACHMENT_STORAGE_PATH:-/mnt/data/mailbox/attachments}"
BACKUP_DEST="${BACKUP_DEST:-/var/backups/family-assistant/files}"
DATE=$(date +%Y%m%d_%H%M%S)

# Create backup with timestamp
mkdir -p "$BACKUP_DEST/$DATE"

echo "Backing up documents..."
rsync -av "$SOURCE_DOCS/" "$BACKUP_DEST/$DATE/documents/"

echo "Backing up email attachments..."
rsync -av "$SOURCE_ATTACHMENTS/" "$BACKUP_DEST/$DATE/attachments/"

# Create a tarball for archival
echo "Creating archive..."
tar -czf "$BACKUP_DEST/files_$DATE.tar.gz" -C "$BACKUP_DEST/$DATE" .
rm -rf "$BACKUP_DEST/$DATE"

echo "File backup complete: $BACKUP_DEST/files_$DATE.tar.gz"
```

### Kubernetes Persistent Volume Backup

If using Kubernetes with persistent volumes:

```bash
# Using Velero for PV backup
velero backup create family-assistant-files \
    --include-namespaces family-assistant \
    --include-resources persistentvolumeclaims,persistentvolumes
```

______________________________________________________________________

## Configuration Backup

### What to Backup

| Item                  | Location                     | Notes                          |
| --------------------- | ---------------------------- | ------------------------------ |
| `config.yaml`         | Project root                 | Main configuration             |
| `mcp_config.json`     | Project root or custom path  | MCP server definitions         |
| `prompts.yaml`        | Project root                 | LLM prompts                    |
| Environment variables | `.env` or secrets management | Contains sensitive credentials |

### Secure Backup Procedure

**Never commit secrets to version control.** Use a secure secrets manager or encrypted backup.

```bash
#!/bin/bash
# backup_config.sh

CONFIG_DIR="${CONFIG_DIR:-.}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/family-assistant/config}"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

# Backup non-sensitive configs (can be version controlled)
tar -czf "$BACKUP_DIR/config_$DATE.tar.gz" \
    "$CONFIG_DIR/config.yaml" \
    "$CONFIG_DIR/prompts.yaml" \
    "$CONFIG_DIR/mcp_config.json" \
    2>/dev/null

# Backup secrets separately with encryption
if [ -f "$CONFIG_DIR/.env" ]; then
    # Encrypt with GPG
    gpg --symmetric --cipher-algo AES256 \
        --output "$BACKUP_DIR/secrets_$DATE.env.gpg" \
        "$CONFIG_DIR/.env"
    echo "Secrets backed up and encrypted: $BACKUP_DIR/secrets_$DATE.env.gpg"
fi

echo "Configuration backup complete."
```

### Restoring Configuration

```bash
# Restore non-sensitive config
tar -xzf config_backup.tar.gz -C /path/to/app/

# Decrypt and restore secrets
gpg --decrypt secrets_backup.env.gpg > /path/to/app/.env
chmod 600 /path/to/app/.env
```

______________________________________________________________________

## Disaster Recovery

### Recovery Time Objective (RTO) and Recovery Point Objective (RPO)

| Backup Type    | Recommended Frequency | Typical RPO | Notes                                 |
| -------------- | --------------------- | ----------- | ------------------------------------- |
| Database       | Daily                 | 24 hours    | Consider more frequent for active use |
| Document files | Daily                 | 24 hours    | Sync changes incrementally            |
| Configuration  | On change             | Minutes     | Version control recommended           |

### Full Disaster Recovery Procedure

#### 1. Provision Infrastructure

- Set up a new PostgreSQL server (or restore from cloud provider snapshot)
- Provision storage volumes for documents and attachments
- Set up the application server/container

#### 2. Restore Database

```bash
# Create database
createdb -h new-server -U postgres family_assistant

# Restore from backup
pg_restore -h new-server -U postgres -d family_assistant \
    --no-owner \
    family_assistant_backup.dump

# Run migrations to ensure schema is current
export DATABASE_URL="postgresql+asyncpg://user:pass@new-server:5432/family_assistant"
alembic upgrade head
```

#### 3. Restore Files

```bash
# Restore documents
rsync -av backup-server:/backups/family-assistant/documents/ /mnt/data/files/

# Restore attachments
rsync -av backup-server:/backups/family-assistant/attachments/ /mnt/data/mailbox/attachments/
```

#### 4. Restore Configuration

```bash
# Restore config files
tar -xzf config_backup.tar.gz -C /path/to/app/

# Decrypt and restore secrets
gpg --decrypt secrets_backup.env.gpg > /path/to/app/.env
chmod 600 /path/to/app/.env
```

#### 5. Update DNS/Ingress

Point DNS records or load balancer to the new server.

#### 6. Verify Recovery

- Check application health endpoint: `curl http://server:8000/health`
- Verify Telegram bot is responding
- Check web UI access
- Test key functionality (notes, calendar, etc.)

______________________________________________________________________

## Best Practices

### Regular Testing

- **Monthly**: Perform a test restore to a separate environment
- **Quarterly**: Full disaster recovery drill
- Document and time recovery procedures

### Offsite Storage

Store backups in a different location than production:

- Cloud storage (S3, GCS, Azure Blob)
- Different data center or region
- Secure physical media for critical backups

### Encryption

- Encrypt backups at rest (especially those containing secrets)
- Use secure transport (SSH/TLS) for backup transfers
- Rotate encryption keys periodically

### Monitoring

Set up alerts for:

- Backup job failures
- Backup file size anomalies (too small may indicate problems)
- Backup age (alert if most recent backup is too old)

### Retention Policy

| Backup Type   | Daily Retention | Weekly Retention | Monthly Retention |
| ------------- | --------------- | ---------------- | ----------------- |
| Database      | 7 days          | 4 weeks          | 12 months         |
| Files         | 7 days          | 4 weeks          | 6 months          |
| Configuration | 30 days         | 12 weeks         | 24 months         |

### Documentation

- Keep this document updated with any infrastructure changes
- Document any custom backup procedures
- Maintain a runbook for common recovery scenarios

______________________________________________________________________

## Troubleshooting

### Common Issues

#### "Permission denied" during backup

```bash
# Ensure PostgreSQL user has necessary permissions
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_user;
```

#### Backup file is corrupted

```bash
# Verify backup integrity
pg_restore --list backup_file.dump

# If corrupted, restore from an older backup
```

#### Migration errors after restore

Refer to [ALEMBIC_MIGRATION_GUIDE.md](../ALEMBIC_MIGRATION_GUIDE.md) for resolving migration
conflicts.

#### Missing vector embeddings after restore

Vector embeddings in `document_embeddings` can be regenerated by re-indexing documents. This is
non-critical data that enhances search but is not required for core functionality.

______________________________________________________________________

## Related Documentation

- [Configuration Reference](./CONFIGURATION_REFERENCE.md) - Environment variables and configuration
  options
- [Alembic Migration Guide](../ALEMBIC_MIGRATION_GUIDE.md) - Database migration procedures
- [Architecture Diagram](../architecture-diagram.md) - System architecture overview

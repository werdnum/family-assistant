# Kubernetes Dev Container Production Checklist

## Prerequisites

- [ ] Container registry accessible from Kubernetes cluster
- [ ] Persistent storage configured at `/data/ssd/sync/workspace/k8s-storage/`
- [ ] GitHub personal access token for repository access

## Steps to Deploy

### 1. Build and Push Image

```bash
cd .devcontainer
./build-and-push.sh v1.0.0  # Use semantic versioning
```

### 2. Create Storage Directories

```bash
sudo mkdir -p /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/{claude-home,postgres-data}
# Set ownership for claude home directory to UID 1001
sudo chown -R 1001:1001 /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/claude-home
# Set ownership for postgres to UID 999 (postgres user)
sudo chown -R 999:999 /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/postgres-data
sudo chmod 700 /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/postgres-data
```

### 3. Set Up Claude Authentication (Optional)

```bash
# Copy existing Claude auth if available
mkdir -p /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/claude-home/.claude
cp -r ~/.claude/* /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/claude-home/.claude/
# Also copy the .claude.json if it exists
cp ~/.claude.json /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/claude-home/ 2>/dev/null || true
```

### 4. Configure Secrets

```bash
# Copy the template and add your tokens
cp .devcontainer/k8s/secrets.yaml.template .devcontainer/k8s/secrets.yaml

# Edit the file to add your GitHub token and API keys
vi .devcontainer/k8s/secrets.yaml

# Apply the secrets to Kubernetes
kubectl apply -f .devcontainer/k8s/secrets.yaml
```

Note: `secrets.yaml` is gitignored. Use `secrets.yaml.template` as reference.

### 5. Deploy to Kubernetes

```bash
cd .devcontainer/k8s
kubectl apply -f .
```

### 6. Verify Deployment

```bash
# Check pod status
kubectl get pods -n family-assistant-dev

# Check logs
kubectl logs -f family-assistant-dev -c backend -n family-assistant-dev

# Test connectivity
kubectl port-forward pod/family-assistant-dev 8000:8000 -n family-assistant-dev
```

## Security Considerations

- [ ] NetworkPolicy restricts egress to internet only
- [ ] Pod runs as non-root user (UID 1000)
- [ ] Secrets stored in Kubernetes secrets (not in images)
- [ ] No host network access

## Monitoring

- [ ] Set up log aggregation for all containers
- [ ] Monitor resource usage (CPU/Memory)
- [ ] Set up alerts for pod restarts

## Backup

- [ ] PostgreSQL data backed up regularly from hostPath
- [ ] Claude auth directory backed up

## Known Limitations

1. Single pod design - no high availability
2. HostPath volumes - tied to specific node
3. No automatic SSL/TLS termination
4. Manual image updates required

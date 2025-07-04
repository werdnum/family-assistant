# Kubernetes Development Environment

Run the family-assistant development environment in Kubernetes with all services in a single pod.

## Prerequisites

1. Set up storage directories (one-time):

   ```bash
   sudo mkdir -p /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/{claude-auth,postgres-data}
   sudo chown -R 1000:1000 /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/

   # Copy Claude auth if you have it
   cp -r ~/.claude/* /data/ssd/sync/workspace/k8s-storage/family-assistant-dev/claude-auth/
   ```

2. Update `secrets.yaml` with your GitHub token

## Deploy

```bash
kubectl apply -f .
```

## Usage

Connect to Claude:

```bash
kubectl exec -it deployment/family-assistant-dev -c claude -n family-assistant-dev -- claude
```

View logs:

```bash
# Backend and frontend (poe dev runs both)
kubectl logs -f deployment/family-assistant-dev -c backend -n family-assistant-dev
```

Access services:

```bash
# Backend API
kubectl port-forward deployment/family-assistant-dev 8000:8000 -n family-assistant-dev

# Frontend
kubectl port-forward deployment/family-assistant-dev 5173:5173 -n family-assistant-dev
```

## Architecture

- Deployment with 1 replica containing 3 containers: postgres, backend (runs poe dev), claude
- Backend container runs both API server and frontend dev server via `poe dev`
- Shared `/workspace` via emptyDir volume
- PostgreSQL data and Claude home directory persisted on hostPath
- Runs as non-root user (UID 1001)
- Network isolated: can only access internet and pods within namespace
- Cannot access other namespaces or local network resources

## Cleanup

```bash
kubectl delete namespace family-assistant-dev
```

#!/usr/bin/env bash
# scripts/deploy-gke.sh - One-shot interactive script to deploy Family Assistant on GKE
#
# This script automates the deployment of Family Assistant to Google Kubernetes Engine (GKE).
# It sets up:
# - A GKE Autopilot cluster
# - A managed PostgreSQL instance with pgvector
# - The Family Assistant application
# - Managed SSL certificates (if a domain is provided)
# - Ingress and Static IP
# - Optional OIDC Authentication

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

function log() { echo -e "${GREEN}[INFO]${NC} $1"; }
function warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
function error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# --- Initialization & Argument Parsing ---

PROJECT_ID=$(gcloud config get-value project 2>/dev/null || echo "")
REGION="us-central1"
CLUSTER_NAME="family-assistant-cluster"
NAMESPACE="family-assistant"
IMAGE="ghcr.io/werdnum/family-assistant:latest"
DOMAIN=""
TELEGRAM_TOKEN=""
GEMINI_API_KEY=""
OPENROUTER_API_KEY=""
ALLOWED_USER_IDS=""
DEVELOPER_CHAT_ID=""
DB_PASSWORD=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 24)

# OIDC Variables
OIDC_CLIENT_ID=""
OIDC_CLIENT_SECRET=""
OIDC_DISCOVERY_URL=""
ALLOWED_OIDC_EMAILS=""
SESSION_SECRET_KEY=$(openssl rand -base64 32)

# Usage information
function usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --project PROJECT_ID      GCP Project ID
  --region REGION           GCP Region (default: $REGION)
  --cluster NAME            GKE Cluster Name (default: $CLUSTER_NAME)
  --namespace NAMESPACE     Kubernetes Namespace (default: $NAMESPACE)
  --image IMAGE             Container image (default: $IMAGE)
  --domain DOMAIN           Domain name for Ingress (optional)
  --telegram-token TOKEN    Telegram Bot Token
  --gemini-key KEY          Gemini API Key
  --openrouter-key KEY      OpenRouter API Key
  --allowed-users IDS       Comma-separated Telegram User IDs
  --dev-chat-id ID          Telegram User ID for error notifications
  --db-password PWD         Password for PostgreSQL (generated if not provided)
  --oidc-client-id ID       OIDC Client ID
  --oidc-client-secret SEC  OIDC Client Secret
  --oidc-discovery URL      OIDC Discovery URL
  --allowed-emails EMAILS   Comma-separated allowed OIDC Emails
  --session-secret KEY      Secret key for sessions (generated if OIDC enabled)
  --help                    Show this help message

Examples:
  $0 --project my-gcp-project --telegram-token 123:abc --gemini-key xyz --allowed-users 12345
EOF
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --project) PROJECT_ID="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --cluster) CLUSTER_NAME="$2"; shift 2 ;;
        --namespace) NAMESPACE="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --telegram-token) TELEGRAM_TOKEN="$2"; shift 2 ;;
        --gemini-key) GEMINI_API_KEY="$2"; shift 2 ;;
        --openrouter-key) OPENROUTER_API_KEY="$2"; shift 2 ;;
        --allowed-users) ALLOWED_USER_IDS="$2"; shift 2 ;;
        --dev-chat-id) DEVELOPER_CHAT_ID="$2"; shift 2 ;;
        --db-password) DB_PASSWORD="$2"; shift 2 ;;
        --oidc-client-id) OIDC_CLIENT_ID="$2"; shift 2 ;;
        --oidc-client-secret) OIDC_CLIENT_SECRET="$2"; shift 2 ;;
        --oidc-discovery) OIDC_DISCOVERY_URL="$2"; shift 2 ;;
        --allowed-emails) ALLOWED_OIDC_EMAILS="$2"; shift 2 ;;
        --session-secret) SESSION_SECRET_KEY="$2"; shift 2 ;;
        --help) usage ;;
        *) error "Unknown option: $1" ;;
    esac
done

# --- Interactive Prompts ---

echo -e "${BLUE}========================================================${NC}"
echo -e "${BLUE}   Family Assistant GKE Deployment Tool${NC}"
echo -e "${BLUE}========================================================${NC}"
echo ""

if [[ -z "$PROJECT_ID" ]]; then
    read -p "Enter GCP Project ID: " PROJECT_ID
fi

if [[ -z "$TELEGRAM_TOKEN" ]]; then
    read -p "Enter Telegram Bot Token: " TELEGRAM_TOKEN
fi

if [[ -z "$GEMINI_API_KEY" ]] && [[ -z "$OPENROUTER_API_KEY" ]]; then
    echo "Provide at least one LLM API Key (Gemini or OpenRouter)"
    read -p "Enter Gemini API Key (leave empty for OpenRouter): " GEMINI_API_KEY
    if [[ -z "$GEMINI_API_KEY" ]]; then
        read -p "Enter OpenRouter API Key: " OPENROUTER_API_KEY
    fi
fi

if [[ -z "$ALLOWED_USER_IDS" ]]; then
    read -p "Enter Allowed Telegram User IDs (comma-separated): " ALLOWED_USER_IDS
fi

if [[ -z "$DEVELOPER_CHAT_ID" ]]; then
    read -p "Enter Developer Chat ID (for error notifications): " DEVELOPER_CHAT_ID
fi

# OIDC Interactive Prompts
if [[ -z "$OIDC_CLIENT_ID" ]]; then
    read -p "Enable OIDC Authentication? (y/N): " ENABLE_OIDC
    if [[ "$ENABLE_OIDC" =~ ^[Yy]$ ]]; then
        read -p "Enter OIDC Client ID: " OIDC_CLIENT_ID
        read -p "Enter OIDC Client Secret: " OIDC_CLIENT_SECRET
        read -p "Enter OIDC Discovery URL (e.g. https://accounts.google.com/.well-known/openid-configuration): " OIDC_DISCOVERY_URL
        read -p "Enter Allowed OIDC Emails (comma-separated, optional): " ALLOWED_OIDC_EMAILS
    fi
fi

# --- Validation & Pre-checks ---

[[ -z "$PROJECT_ID" ]] && error "Project ID is required"
[[ -z "$TELEGRAM_TOKEN" ]] && error "Telegram token is required"
[[ -z "$ALLOWED_USER_IDS" ]] && error "Allowed User IDs are required"

# Check for required tools
command -v gcloud >/dev/null 2>&1 || error "gcloud CLI is not installed"
command -v kubectl >/dev/null 2>&1 || error "kubectl is not installed"

log "Using Project: $PROJECT_ID"
log "Using Region: $REGION"
log "Using Cluster: $CLUSTER_NAME"

# --- GCP Resource Provisioning ---

log "Configuring gcloud project..."
gcloud config set project "$PROJECT_ID" --quiet

log "Enabling required APIs (this may take a minute)..."
gcloud services enable \
    container.googleapis.com \
    compute.googleapis.com \
    containerregistry.googleapis.com \
    --quiet

log "Checking for existing GKE cluster..."
if gcloud container clusters describe "$CLUSTER_NAME" --region "$REGION" >/dev/null 2>&1; then
    log "Cluster '$CLUSTER_NAME' already exists."
else
    log "Creating GKE Autopilot cluster '$CLUSTER_NAME' (this usually takes 5-10 minutes)..."
    gcloud container clusters create-auto "$CLUSTER_NAME" \
        --region "$REGION" \
        --quiet
fi

log "Getting cluster credentials..."
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION"

# --- Kubernetes Deployment ---

log "Creating namespace '$NAMESPACE'..."
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

log "Creating application secrets..."
kubectl create secret generic family-assistant \
    --namespace "$NAMESPACE" \
    --from-literal=TELEGRAM_BOT_TOKEN="$TELEGRAM_TOKEN" \
    --from-literal=GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
    --from-literal=OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}" \
    --from-literal=ALLOWED_USER_IDS="$ALLOWED_USER_IDS" \
    --from-literal=DEVELOPER_CHAT_ID="$DEVELOPER_CHAT_ID" \
    --from-literal=POSTGRES_PASSWORD="$DB_PASSWORD" \
    --from-literal=OIDC_CLIENT_ID="${OIDC_CLIENT_ID:-}" \
    --from-literal=OIDC_CLIENT_SECRET="${OIDC_CLIENT_SECRET:-}" \
    --from-literal=OIDC_DISCOVERY_URL="${OIDC_DISCOVERY_URL:-}" \
    --from-literal=ALLOWED_OIDC_EMAILS="${ALLOWED_OIDC_EMAILS:-}" \
    --from-literal=SESSION_SECRET_KEY="${SESSION_SECRET_KEY:-}" \
    --dry-run=client -o yaml | kubectl apply -f -

log "Deploying PostgreSQL with pgvector..."
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  namespace: $NAMESPACE
spec:
  serviceName: "postgres"
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
      - name: postgres
        image: pgvector/pgvector:pg16
        ports:
        - containerPort: 5432
        env:
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: family-assistant
              key: POSTGRES_PASSWORD
        - name: POSTGRES_USER
          value: postgres
        - name: POSTGRES_DB
          value: family_assistant
        volumeMounts:
        - name: postgres-data
          mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
  - metadata:
      name: postgres-data
    spec:
      accessModes: [ "ReadWriteOnce" ]
      resources:
        requests:
          storage: 10Gi
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: $NAMESPACE
spec:
  selector:
    app: postgres
  ports:
  - port: 5432
    targetPort: 5432
EOF

log "Deploying Family Assistant application..."
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: family-assistant
  namespace: $NAMESPACE
spec:
  replicas: 1
  selector:
    matchLabels:
      app: family-assistant
  template:
    metadata:
      labels:
        app: family-assistant
    spec:
      containers:
      - name: family-assistant
        image: $IMAGE
        ports:
        - containerPort: 8000
        envFrom:
        - secretRef:
            name: family-assistant
        env:
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: family-assistant
              key: POSTGRES_PASSWORD
        - name: DATABASE_URL
          value: "postgresql+asyncpg://postgres:\$(POSTGRES_PASSWORD)@postgres:5432/family_assistant"
        - name: TIMEZONE
          value: "Australia/Sydney"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 20
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 15
          periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: family-assistant
  namespace: $NAMESPACE
  annotations:
    cloud.google.com/neg: '{"ingress": true}' # Required for GCE Ingress + Autopilot
spec:
  type: ClusterIP
  selector:
    app: family-assistant
  ports:
  - port: 80
    targetPort: 8000
EOF

# --- Networking ---

if [[ -n "$DOMAIN" ]]; then
    log "Configuring Ingress for domain $DOMAIN with Managed SSL..."

    IP_NAME="family-assistant-ip"
    log "Checking for static IP '$IP_NAME'..."
    if ! gcloud compute addresses describe "$IP_NAME" --global >/dev/null 2>&1; then
        log "Reserving static IP '$IP_NAME'..."
        gcloud compute addresses create "$IP_NAME" --global --quiet
    fi
    STATIC_IP=$(gcloud compute addresses describe "$IP_NAME" --global --format='value(address)')
    log "Static IP Reserved: $STATIC_IP"

    cat <<EOF | kubectl apply -f -
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata:
  name: family-assistant-cert
  namespace: $NAMESPACE
spec:
  domains:
    - $DOMAIN
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: family-assistant-ingress
  namespace: $NAMESPACE
  annotations:
    kubernetes.io/ingress.global-static-ip-name: $IP_NAME
    networking.gke.io/managed-certificates: family-assistant-cert
    kubernetes.io/ingress.class: "gce"
spec:
  rules:
  - host: $DOMAIN
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: family-assistant
            port:
              number: 80
EOF
    echo ""
    log "=========================================================="
    log "   Next Step: Configure your DNS!"
    log "   Create an A record for $DOMAIN pointing to $STATIC_IP"
    log "   Wait for ManagedCertificate to be 'Active' (can take 30-60 mins)"
    log "=========================================================="
    if [[ -n "$OIDC_CLIENT_ID" ]]; then
        log "   OIDC Callback URL: https://$DOMAIN/auth"
        log "   Ensure this URL is added to your OIDC provider's Authorized Redirect URIs."
    fi
    log "=========================================================="
else
    log "No domain provided. Creating a LoadBalancer service for temporary access..."
    cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: family-assistant-lb
  namespace: $NAMESPACE
spec:
  type: LoadBalancer
  selector:
    app: family-assistant
  ports:
  - port: 80
    targetPort: 8000
EOF

    log "Waiting for LoadBalancer IP (this may take a few minutes)..."
    LB_IP=""
    while [[ -z "$LB_IP" ]]; do
        LB_IP=$(kubectl get svc family-assistant-lb -n "$NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' || echo "")
        sleep 5
    done
    log "Family Assistant is available at: http://$LB_IP"
    warn "This connection is UNSECURED. Please use a domain for production."
    if [[ -n "$OIDC_CLIENT_ID" ]]; then
        warn "OIDC requires HTTPS. Callbacks to http://$LB_IP/auth may fail with many providers."
    fi
fi

log "Deployment complete!"
log "Use 'kubectl logs -f deployment/family-assistant -n $NAMESPACE' to monitor the application."

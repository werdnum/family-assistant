# Delegating work from Family Assistant to Omnigent agents

**Status:** implemented (A2A service profile) **Goal:** family-assistant can delegate tasks to
[Omnigent](https://github.com/omnigent-ai/omnigent) agents running in a Kubernetes sandbox (or any
other omnigent-managed host).

## Current state (what already exists)

- **Omnigent is already deployed in-cluster** — see
  `kube-config/kubernetes/manifests/workloads/omnigent/` (ArgoCD app
  `applications/workloads/omnigent.yaml`). The server runs in **OIDC (Keycloak SSO)** mode at
  `http://omnigent.omnigent.svc.cluster.local:8000`, published publicly via Cloudflare Access.
- **Kubernetes sandbox runners already work**: `configmap-sandbox.yaml` configures the built-in
  `kubernetes` provider spawning per-session runner Jobs in `omnigent-sandboxes` (arm64, GitHub App
  creds, agent-state PVC, admission policies pinning it all down).
- **family-assistant speaks A2A client-side**: remote A2A service profiles
  (`service_profiles[].remote_a2a`) with submit-then-poll semantics, bearer/api_key auth via
  `token_env`, and per-profile `delegate_to_service` policy gating.

So the missing piece was a bridge exposing omnigent delegation over A2A — which is exactly what
`omnigent-bridge` is.

## Design

```
family-assistant ──(A2A JSON-RPC, bearer)──► omnigent-bridge ──(REST+SSE, bearer JWT)──► omnigent server
  service_profiles[]                              │                                        │
    .id: "omnigent"                               └── one omnigent chat per A2A contextId  ▼
    .remote_a2a.agent_url                                                            k8s sandbox host
```

The bridge is a small **A2A server** wrapping the first-party `omnigent-client` SDK:

- `message/send` with `blocking=false` returns immediately after the task reaches
  `submitted`/`working`; the turn keeps running and FA polls `tasks/get` until the task is terminal
  and the final artifact (the agent's answer) is available. This matches FA's async
  remote-delegation flow exactly — long coding turns don't hold an HTTP call open.
- One **omnigent chat per A2A context id**: clients signal conversation continuity by repeating
  `contextId`, so follow-up messages continue the same underlying session.
- `tasks/cancel` interrupts the in-flight turn; failures map to the A2A `failed` state.

Why A2A rather than MCP tools: delegation is a *service-level* capability with its own policy
boundary, not a grab-bag of tools an LLM composes. As a service profile it inherits FA's whole
remote-delegation machinery — submit/poll, timeouts, cancellation, UI affordances, and per-profile
`delegate_to_service` gating — instead of needing bespoke tool plumbing and tool-policy rules. The
bridge keeps the protocol surface minimal (one skill), so there is nothing for the model to misuse
at the tool layer.

### Policy scoping (important)

Delegation is gated by each profile's own `tools_policy` via the existing `delegate_to_service`
mechanism. Deliberately **no** allow rules are placed under `default_profile_settings.tools_policy`:
operator-layer rules run at priority +1000 against *every* profile, including restricted ones whose
own policies deny delegation — adding them there would silently override those denials (privilege
escalation). Restricted profiles stay restricted; profiles that may delegate keep allowing it
themselves.

## The bridge (`kube-config/containers/omnigent-bridge/`)

Self-contained container image built by `.github/workflows/build-omnigent-bridge.yaml` (digest
auto-pinned into the Deployment) and deployed to the `omnigent` namespace by ArgoCD app
`omnigent-bridge`.

Protocol surface:

| Route                              | Purpose                                                      |
| ---------------------------------- | ------------------------------------------------------------ |
| `GET /.well-known/agent-card.json` | Agent card (`omnigent-bridge`, JSONRPC transport, one skill) |
| `POST /`                           | JSON-RPC `message/send` / `tasks/get` / `tasks/cancel`       |
| `GET /health`                      | Unauthenticated probe endpoint                               |

Internally: resolve the configured agent (`OMNIGENT_BRIDGE_AGENT`, default `claude-native-ui`) →
create a session → `SessionsChat.query(task)` folds the SSE stream into final text. Delegation runs
are long-lived, so every turn runs under a generous timeout.

Two omnigent-server behaviors the bridge accommodates:

1. **A session needs a host bound before it can run** — a bare `POST /v1/sessions {agent_id}` 503s
   with "No runner bound for session". With `OMNIGENT_BRIDGE_SANDBOX_PROVIDER=kubernetes` (the
   cluster default) the bridge instead creates managed sessions and the server spawns the
   per-session runner Job itself — no connected host needed. For external hosts the bridge mirrors
   the web UI: pick an online host from `/v1/hosts`, mkdir a workspace via
   `POST /v1/hosts/{id}/directories`.
2. **Identity must match the host owner** — hosts are owner-scoped. The bridge acts as
   `family-assistant@andrewjgarrett.com`, which passes omnigent's domain allowlist without being a
   real Keycloak user (do not add it to `/data/admins`).

**Verified end-to-end** against a local `omnigent server` driving the bridge with FA's own
`A2AClientWrapper` (submit-then-poll): real harness turn ("What is 2+2?" → "4"), follow-up on the
same contextId reusing the session ("multiply by 10" → "40"), and the bearer-token gate rejecting
missing/wrong credentials with 401.

## Auth

Two independent credentials, deliberately separated:

1. **Clients → bridge.** `OMNIGENT_BRIDGE_API_TOKEN` makes the bridge require
   `Authorization: Bearer <token>` on every route except `/health`. Without this the MCP-era
   endpoint would have been callable from anywhere that can reach the Service (e.g. sandboxed
   agents). The value is sealed into both namespaces: `omnigent-bridge-auth` (omnigent ns, read by
   the bridge) and `family-assistant-omnigent-token` (ml-bot ns, presented by FA as
   `OMNIGENT_BRIDGE_TOKEN`).
2. **Bridge → omnigent server.** A weekly `omnigent-bridge-token-minter` CronJob is the *only*
   workload that sees `OMNIGENT_OIDC_COOKIE_SECRET`. It signs a derived 90-day single-identity HS256
   session JWT (byte-compatible with `omnigent.server.oidc.mint_session_token`) and publishes it as
   Secret `omnigent-bridge-server-token`; the bridge Deployment reads it as `OMNIGENT_BEARER_TOKEN`,
   and stakater Reloader rolls the bridge when the Secret rotates. RBAC is namespaced and limited to
   get/create/patch on that one Secret.

This addresses the earlier design's biggest caveat: the signing key no longer lives inside the
bridge container, and a leaked minted token expires within 90 days even if the cookie secret never
rotates. Longer term, upstreaming oidc-mode support for omnigent's device grant (RFC 8628) would
make the token scoped, consent-backed and revocable — tracked as future work.

Bootstrap order: apply manifests, then trigger the minter once
(`kubectl create job --from=cronjob/omnigent-bridge-token-minter ...`) before expecting delegation
to work — the bridge crash-loops until `omnigent-bridge-server-token` exists.

## family-assistant side

```yaml
service_profiles:
  - id: "omnigent"
    description: "Delegates self-contained tasks to Omnigent agents..."
    remote_a2a:
      agent_url: "http://omnigent-bridge.omnigent.svc.cluster.local"
      auth:
        type: "bearer"
        token_env: "OMNIGENT_BRIDGE_TOKEN"   # set in Deployment-family-assistant.yaml env
      timeout_seconds: 60
      poll_interval_seconds: 15
      max_async_seconds: 14400               # long coding turns
```

No `mcp_config` entry, no tool-policy changes: the profile description drives routing, and
delegation permission comes from each profile's `delegate_to_service` rules (see the policy scoping
section above). See `config.yaml.example` ("Omnigent delegation") for the commented example.

## Security notes

- Delegation grants the sub-agent whatever its harness credentials allow inside the sandbox; treat
  delegating to omnigent like handing work to `execute_script` — gate it per profile, never
  globally.
- Treat the agent's final report as untrusted model output about out-of-band work; FA already taints
  remote service results accordingly.
- The bridge holds only the minted single-identity token and the shared API token — not user
  credentials, not the signing key.

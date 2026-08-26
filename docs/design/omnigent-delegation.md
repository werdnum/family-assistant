# Delegating work from Family Assistant to Omnigent agents

**Status:** proposal / prototype
**Goal:** family-assistant can delegate tasks to [Omnigent](https://github.com/omnigent-ai/omnigent)
agents running in a Kubernetes sandbox (or any other omnigent-managed host).

## Current state (what already exists)

- **Omnigent is already deployed in-cluster** — see
  `kube-config/kubernetes/manifests/workloads/omnigent/` (ArgoCD app
  `applications/workloads/omnigent.yaml`). The server runs in **OIDC (Keycloak SSO)** mode at
  `http://omnigent.omnigent.svc.cluster.local:8000`, published publicly via Cloudflare Access.
- **Kubernetes sandbox runners already work**: `configmap-sandbox.yaml` configures the built-in
  `kubernetes` provider spawning per-session runner Jobs in `omnigent-sandboxes` (arm64,
  GitHub App creds, agent-state PVC, admission policies pinning it all down).
- So the *only* missing piece is the family-assistant → omnigent-server hop: a client that can
  create sessions and drive them headlessly.

## TL;DR recommendation

Skip the A2A protocol. Omnigent has **no native A2A support**, but it exposes a full REST +
SSE server API with a first-party Python SDK (`omnigent-client`), and family-assistant already
supports **MCP servers over stdio / SSE / streamable-HTTP** (`family_assistant/tools/mcp.py`,
`mcp_servers:` in `config.yaml`). So the lowest-friction bridge is:

> A small **MCP bridge service** that wraps the `omnigent-client` SDK and exposes tools like
> `omnigent_delegate(task, agent)` and `omnigent_send_message(session_id, message)`.
> family-assistant connects to it as just another MCP server — zero core code changes,
> only `config.yaml` + a `tools_policy` rule.

The bridge ships in the kube-config repo:
[`containers/omnigent-bridge/`](../../../kube-config/containers/omnigent-bridge/), deployed
in-cluster by ArgoCD app `omnigent-bridge`.

## Options considered

### 1. MCP bridge (recommended)

```
family-assistant ──(MCP: streamable-http)──► omnigent-mcp-bridge ──(REST+SSE)──► omnigent server
                                                                                        │
                                                                                        ▼
                                                                          k8s sandbox host (per-session)
```

Pros:

- family-assistant's MCP client support is already built and battle-tested (stdio, sse,
  streamable_http transports with token auth).
- The bridge is a tiny standalone process; it can run anywhere — beside family-assistant,
  in-cluster next to the omnigent server, or on a laptop for dev.
- Policy/taint tracking works unchanged: MCP tool descriptors get tags derived from their
  metadata, so a `tools_policy` rule like `match: { names: ["omnigent_delegate"] }` gates it.
- Same bridge is reusable by *any* MCP-capable client (Claude Code, pi, etc.), not just
  family-assistant.

Cons:

- One more small service to run/deploy.

### 2. Native tool module in family-assistant

Add `src/family_assistant/tools/omnigent.py` following the standard tool recipe
(`tools/CLAUDE.md`), calling `omnigent-client` directly instead of going through MCP.

Pros:

- Most idiomatic: first-class `ToolTag`, taint tracking, on-demand discovery, config via
  `config.yaml`.

Cons:

- Requires core changes + adding `omnigent-client` as a dependency of family-assistant.
- Duplicates what the MCP bridge gives; more code to maintain in-repo.

This is worth doing later if delegation becomes a marquee feature; start with the bridge.

### 3. A2A (agent2agent) protocol

Google's A2A protocol would let any A2A client call an "omnigent agent card". But:

- Omnigent ships no A2A endpoint; we'd have to build the entire A2A server side ourselves
  wrapping the same REST API — strictly more work than the MCP bridge for no extra payoff
  unless you specifically want non-MCP interop.
- family-assistant has no A2A client either.

Not recommended today. If A2A matters later, an `a2a` transport can be added to the same
bridge process alongside MCP since both wrap the same `omnigent-client` calls.

## How the pieces fit

### Omnigent side

- **Server**: deploy with the official Kustomize manifests (`omnigent/deploy/kubernetes/`) —
  Deployment + Service + PVC (+ optional Ingress). In kube-config this lands under
  `kubernetes/apps/…` per normal GitOps conventions.
- **Kubernetes sandbox provider**: omnigent has a built-in `kubernetes` sandbox launcher
  (`omnigent/onboarding/sandboxes/kubernetes.py`, "entrypoint-as-host"): each managed session
  becomes an on-demand Job whose container command IS `omnigent host`; launch token rides a
  per-Job Secret, LLM credentials come from a pre-created Secret referenced by
  `sandbox.kubernetes.secret_name`, Pod runs non-root with all caps dropped. Needs RBAC for the
  server SA to create Jobs/Secrets in the sandbox namespace. Enable by configuring the
  `kubernetes` sandbox provider on the server.
- **Auth between bridge and server** — this is the one real gap. Omnigent has exactly one active
  auth provider (`OMNIGENT_AUTH_PROVIDER`) and no API-key scheme (`omnigent/server/auth.py`).
  Our deployment uses `oidc` (Keycloak), which validates either the `__Host-ap_session` cookie or
  an HS256 JWT presented as `Authorization: Bearer <jwt>` (the same shape `omnigent login`
  issues). Machine-auth options, best-first:

  1. **Offline-minted bearer service token (works today).** Mint an HS256 session JWT
     (`{sub, iat, exp, provider: "oidc"}`, e.g. `sub: family-assistant@…`) signed with
     `OMNIGENT_OIDC_COOKIE_SECRET` from `sealedsecret-omnigent-secrets.yaml`, seal it as its own
     Secret for the bridge, and send it as a Bearer header. Same claim shape the server's own
     runner tokens use; TTL is whatever we sign. Caveats: no per-token revocation short of
     secret rotation, and rotation of the cookie secret invalidates it — re-mint then.
  2. **Device grant (RFC 8628) — clean but currently accounts-mode only.** Upstream has a full
     device-consent flow (`/oauth/device/*` + refresh grants, built for the Slack integration)
     that mints scoped, revocable delegated tokens for third-party clients. It is gated to
     `OMNIGENT_AUTH_PROVIDER=accounts`; our oidc deployment doesn't mount it. Extending the gate
     to allow oidc (consent page works fine behind Keycloak) would be a small upstream
     contribution and the right long-term answer.
  3. **Trusted-header mode** (`provider=header`) would be simplest but means abandoning Keycloak
     SSO for the human UI — not worth it.

  The bridge supports both (1) (`OMNIGENT_AUTH_MODE=bearer`) and (3)
  (`OMNIGENT_AUTH_MODE=header`).

### Bridge side (`kube-config/containers/omnigent-bridge/`)

The bridge is a self-contained container image in the kube-config repo
(`containers/omnigent-bridge/`, built by `build-omnigent-bridge.yaml`, deployed
to the `omnigent` namespace by ArgoCD app `omnigent-bridge`). Tools exposed:

| Tool | Purpose |
| --- | --- |
| `omnigent_list_agents()` | List registered agents (id/name/harness) |
| `omnigent_delegate(agent, task, workspace?, timeout_s?)` | Create a session bound to the agent, send the task, block until the turn completes, return final text + `session_id` |
| `omnigent_send_message(session_id, message)` | Follow-up turn on a previously delegated session |
| `omnigent_session_status(session_id)` | Fetch session snapshot/status |

Internally it uses `omnigent_client.OmnigentClient` → `sessions.create_from_agent_id(...)` →
`SessionsChat.query(task)` which folds the SSE stream into final text. Delegation runs are
long-lived (minutes), so the bridge enforces a generous per-call timeout and returns partial
status rather than hanging forever.

**Verified end-to-end** against a local `omnigent server`: delegate → real harness turn
(`"What is 2+2?"` → `"4"`), follow-up turn (`"multiply by 10"` → `"40"`), status lookup, and the
unknown-agent error path all work. Two server behaviors the bridge has to accommodate:

1. **A session needs a host bound before it can run.** A bare `POST /v1/sessions {agent_id}`
   503s with "No runner bound for session" — the runner relaunch path only fires for sessions
   that carry a `host_id`. So the bridge mirrors what the web UI does: pick an online host from
   `/v1/hosts` whose `configured_harnesses` covers the agent's harness, mkdir a fresh workspace
   via `POST /v1/hosts/{id}/directories` (`~/omnigent-delegations/<stamp>` by default), and
   create the session with `{agent_id, host_id, workspace}`.
2. **Identity must match the host owner.** Hosts/runner registries are owner-scoped; a client
   authenticated as user X cannot see or bind hosts registered by user Y. On the cluster this
   means the bridge's bearer identity must be the same account that runs `omnigent host`.

For Kubernetes-sandbox deployments no connected host is needed: set
`OMNIGENT_BRIDGE_SANDBOX_PROVIDER=kubernetes` and the bridge creates sessions with
`host_type: managed` + `sandbox_provider`, letting the server spawn the per-session runner Job.

Auth on the cluster deployment is fully automated: the bridge projects only the
`OMNIGENT_OIDC_COOKIE_SECRET` key from `omnigent-secrets` and signs its own long-TTL bearer JWT
in-process (byte-compatible with `omnigent.server.oidc.mint_session_token`; verified), re-signing
daily. Nothing to mint by hand.

### family-assistant side

The in-cluster family-assistant config already wires MCP servers this way (`fridge` etc. in
`kube-config/.../ConfigMap-family-assistant-config.yaml`), so the omnigent entry follows the
identical pattern:

```yaml
# config.yaml
mcp_servers:
  omnigent:
    transport: "streamable_http"     # or stdio for dev: command: python -m omnigent_bridge
    url: "http://omnigent-bridge.tools.svc.cluster.local/mcp"

default_profile_settings:
  tools_policy:
    rules:
      - match:
          names:
            - omnigent_delegate
            - omnigent_send_message
            - omnigent_list_agents
            - omnigent_session_status
        decision: "allow"
        priority: 10
```

Optionally add the names to `tools_config.on_demand_local_tools` if they should only be
discoverable when needed.

## Rollout status

1. **Dev loop** — done: local `omnigent server` + `--agent` test agent + host daemon; bridge
   driven over stdio MCP; real delegated turn verified (see "Verified end-to-end" above).
2. **In-cluster** — kube-config PRs:
   - `Add omnigent-bridge MCP delegation service`: container + build workflow (digest
     auto-pinned into the Deployment) + Deployment/Service in the `omnigent` namespace + ArgoCD
     app. Auth is the in-process cookie-secret signing described above — nothing to mint by hand.
   - `Wire Family Assistant to omnigent-bridge`: `mcp_config.mcpServers.omnigent` entry +
     per-tool `tools_policy` allow rules in `ConfigMap-family-assistant-config.yaml`.
   - family-assistant PR: this design doc + `config.yaml.example` example block.
3. **Hardening / later**: upstream the oidc-mode device grant so the bridge token becomes
   scoped, revocable and consent-backed instead of cookie-secret-derived; per-profile policy
   gating beyond the default profile.

## Security notes

- Delegation grants the sub-agent whatever its harness credentials allow inside the sandbox;
  treat `omnigent_delegate` like `execute_script` — allow-list per profile, not global.
- Don't forward raw user content into task text blindly; consider tainting delegated results
  like other untrusted tool output.
- The bridge holds only an identity header, not user credentials; rotate/restrict at the
  NetworkPolicy layer.

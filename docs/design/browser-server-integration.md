# Browser-Server Integration (optional remote browser backend)

## Status

Proposed → in implementation.

## Motivation

Family Assistant currently drives a **local, in-process, headless** Playwright browser for the
semantic DOM tools (`browser_open`, `browser_snapshot`, `browser_click`, `browser_fill`,
`browser_select`, `browser_wait`, `browser_extract`, `browser_screenshot`, `browser_exec`) and the
coordinate-based Computer Use tools. A headless browser living inside the assistant process can
**never** be handed to a human: there is no display, no remote control surface, and no lease/safety
model. That makes warm agent→human handoff (payments, credentials, OTP, CAPTCHA, cookie consent)
impossible today.

The companion [`browser-server`](https://github.com/werdnum/browser-server) service ("Browser
Handoff Service", designed in PR #838) already implements the hard part: it owns headed Chromium +
Playwright + a virtual display + noVNC, enforces a lease/state machine, and exposes a narrow
service-authenticated REST API. What's missing is the **Family Assistant adapter** (Milestone 5 of
the standalone-service plan): a client that lets the assistant create and drive sessions on the
service and request a human handoff.

This document specifies that adapter. The integration is **optional** and **off by default**; it is
turned on purely through configuration (in this repo's `config.yaml`, and operationally via the
`kube-config` ConfigMap). When disabled, behavior is byte-for-byte the current local Playwright
path.

## Goals

- One browsing surface. The existing semantic DOM tools work unchanged from the LLM's perspective;
  they gain a pluggable **backend**. We do **not** add a second, parallel set of browsing tools.
- When enabled, the same browser the agent drives can be transferred to a human via `browser-server`
  (noVNC), unlocking `request_browser_handoff`.
- No capability regression: the remote backend serves the same rich accessibility snapshots (with
  stable `eN` refs) and real screenshots that the local backend does. This requires a small,
  backward-compatible extension to `browser-server`'s agent-command protocol.
- Fail-closed and config-gated: absent configuration, none of the remote code paths are reachable.

## Non-goals (initial milestone)

- Remoting the coordinate-based **Computer Use / visual** profile (`browser_visual_profile`). It
  stays local for now; remoting it needs continuous screenshot streaming at device coordinates and
  is tracked as a follow-up. The local backend keeps serving it.
- Durable handoff history, family-wide claim, sanitized resume policy beyond what `browser-server`
  already implements.

## Architecture

### Backend abstraction (family-assistant)

Introduce a `BrowserBackend` protocol that the DOM tools call instead of touching a Playwright
`Page` directly. Two implementations:

- `LocalPlaywrightBackend` — the current behavior, extracted out of `browser_dom.py` /
  `browser_session.py`. Owns a local `BrowserSession`, runs the `_SNAPSHOT_JS` accessibility walker,
  resolves `eN` refs to `[data-fa-ref="eN"]` selectors, takes screenshots, runs `page.evaluate`.
- `RemoteBrowserBackend` — an httpx client for `browser-server`'s `/v1/sessions/*` API. Maps each
  high-level operation onto an `agent-command`. The remote worker runs the **same** accessibility
  walker server-side and returns the same `Snapshot` JSON, so ref handling and TOON rendering stay
  identical on the family-assistant side.

The backend is selected per execution context: when `browser_handoff_config.enabled` and the active
profile is handoff-capable, tools resolve a `RemoteBrowserBackend` keyed by `conversation_id`
(mirroring today's per-conversation `BrowserSession`); otherwise they use `LocalPlaywrightBackend`.

```text
browser_* tools  ──▶  BrowserBackend (protocol)
                         ├── LocalPlaywrightBackend  (default; local headless Playwright)
                         └── RemoteBrowserBackend     (httpx → browser-server /v1/sessions/*)
                                                          │ Bearer service token
                                                          ▼
                                                  browser-server (headed Chromium + noVNC + leases)
```

The `BrowserBackend` protocol surface (high level, matching the existing tool set):

- `open(url, query) -> SnapshotData`
- `snapshot(query) -> SnapshotData`
- `click(ref) -> SnapshotData`
- `fill(ref, text, submit) -> SnapshotData`
- `select(ref, value) -> SnapshotData`
- `wait(selector, state, timeout_ms) -> SnapshotData`
- `extract(selector) -> markdown`
- `screenshot() -> png bytes`
- `exec(code) -> result`
- `request_handoff(reason, note, expected_origin, allowed_resume) -> handoff_url` (remote only)
- `close()`

### Protocol extension (browser-server)

`browser-server`'s `agent-command` set is deliberately narrow and its `snapshot`/`screenshot` return
stubs (title + 500 chars; `{redacted: true}`). To let the rich tools work remotely without a
capability regression, extend the `PlaywrightBrowserWorker` (agent-owned states only —
human/sanitize guards are unchanged) so that:

- `snapshot` returns the full accessibility tree (`{url, title, forms, elements, roots:[...]}`) by
  running the same DOM walker that family-assistant uses; elements are tagged `data-fa-ref="eN"`.
- `screenshot` returns base64-encoded PNG bytes (`{mime_type, image_base64}`) when the agent owns
  the lease. Screenshots remain blocked in human/sanitize states by the existing
  `OBSERVATION_COMMANDS` guard — the no-observation-during-human-control invariant is preserved.
- New agent commands: `extract` (page/subtree → HTML for markdown conversion), `exec` (run
  `page.evaluate` JS, same-origin V8 only), and `wait` (load-state / selector). `click`/`type_text`/
  `select` already accept a `selector`, so family-assistant passes `[data-fa-ref="eN"]`.

These are additive and guarded; existing fake-runtime tests and the lease/redaction model are
unaffected for the human-controlled states.

## Configuration

### family-assistant config model

New optional top-level section, mirroring `ai_worker_config` (off by default):

```python
class BrowserHandoffConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    service_url: str | None = None          # e.g. http://browser-server.browser-server.svc.cluster.local:8000
    auth: RemoteA2AAuthConfig = Field(default_factory=RemoteA2AAuthConfig)  # reuse existing auth model
    timeout_seconds: float = 30.0
    handoff_capable_profiles: list[str] = Field(default_factory=lambda: ["browser_profile"])
```

Wired into `AppConfig` as `browser_handoff_config: BrowserHandoffConfig`. Auth reuses the existing
`RemoteA2AAuthConfig` (`type: bearer`, `token_env: BROWSER_HANDOFF_SERVICE_TOKEN`,
`header_name: Authorization`), so the bearer token is read from the environment, never stored in
YAML.

### Env var overrides (config_loader `ENV_VAR_MAPPINGS`)

```text
BROWSER_HANDOFF_ENABLED   -> browser_handoff_config.enabled (bool)
BROWSER_HANDOFF_URL       -> browser_handoff_config.service_url
BROWSER_HANDOFF_TIMEOUT   -> browser_handoff_config.timeout_seconds (float)
```

The service token itself is referenced by name (`token_env`) and read at request time, consistent
with `remote_a2a` / MCP `$VAR` handling.

### defaults.yaml

```yaml
browser_handoff_config:
  enabled: false
  service_url: null
  timeout_seconds: 30.0
  auth:
    type: "bearer"
    token_env: "BROWSER_HANDOFF_SERVICE_TOKEN"
  handoff_capable_profiles:
    - "browser_profile"
```

### kube-config (the operational on/off switch)

In `kubernetes/manifests/workloads/family-assistant/`:

1. `ConfigMap-family-assistant-config.yaml`: add a `browser_handoff_config` block with
   `enabled: true` and `service_url: http://browser-server.browser-server.svc.cluster.local:8000`.
2. Deployment: add `BROWSER_HANDOFF_SERVICE_TOKEN` env, sourced from a `browser-server` secret
   reflected into `ml-bot` (the same Stakater-reflector pattern already used for
   `family-assistant-asterisk`) or a dedicated SealedSecret in `ml-bot`.
3. A `NetworkPolicy` allowing egress `ml-bot/family-assistant → browser-server:8000`, plus matching
   ingress on the `browser-server` namespace if it default-denies.

The in-cluster Service call uses the **service token** and bypasses the external OIDC
`SecurityPolicy`, which only targets the public `HTTPRoute`.

## Security

- The remote backend authenticates with a scoped service token; no user secrets reach the worker.
- The no-observation-during-human-control invariant is enforced by `browser-server` (lease/state
  guards), not by the client — the client cannot snapshot/screenshot a human-owned session because
  the service returns `403`.
- `browser_exec` JS continues to run only in the page's same-origin V8 context (now server-side),
  with no access to the assistant process.
- Rule of Two: the browser profile remains an `[AC]`-style sandboxed surface; the remote backend
  does not widen data access.

## Tooling / UX

- `request_browser_handoff` is added to `browser_dom.py` and registered; it is allowed only in
  `handoff_capable_profiles` and is a no-op error when the remote backend is not active. The
  `browser_profile` system prompt in `prompts.yaml`/`defaults.yaml` gains a short note on when to
  hand off to a human.
- `docs/user/USER_GUIDE.md` documents the optional handoff capability.

## Milestones

1. **browser-server protocol extension** — rich snapshot, screenshot bytes, `exec`/`extract`/`wait`;
   unit tests against the fake + real runtimes. (browser-server repo)
2. **family-assistant config** — `BrowserHandoffConfig`, env mappings, `defaults.yaml`; config
   tests.
3. **family-assistant backend** — `BrowserBackend` protocol, `LocalPlaywrightBackend` (extract
   current behavior), `RemoteBrowserBackend`, tool refactor, `request_browser_handoff`, policy +
   prompt + user-guide; unit/functional tests with a fake browser-server.
4. **kube-config wiring** — ConfigMap, service-token secret, NetworkPolicy, Deployment env.

Each milestone is independently testable and shippable.

# Warm Browser Session Handoff

## Status

Draft for review.

## Summary

Add a separate browser-session handoff service that owns Playwright browser sessions and can lease a
live session to either the agent or an authenticated human. Family Assistant integrates with that
service through a narrow API and a small browser-tool adapter. The agent performs research and form
preparation, then calls a handoff tool when the next step requires payment details, credentials,
CAPTCHA solving, or other human-only judgment. The web app opens the handoff service's live remote
browser viewport and controls. While the human has the lease, the agent cannot observe, snapshot,
extract, or mutate the page.

The recommended V1 runs headed Chromium inside an isolated browser runtime container with a virtual
display exposed through noVNC. The agent still drives the page through Playwright via the handoff
service. The human drives the same browser window through the service's control UI or an embedded
Family Assistant page. The service coordinates leases, policy, handoff tokens, notifications,
cleanup, and audit events.

## Goals

- Let the agent do the tedious work before checkout: search, compare, add items, fill non-sensitive
  fields, and navigate to the point where a human must act.
- Let the human complete sensitive steps in the already-warm browser session without sharing
  passwords, payment card details, or one-time codes with the model.
- Prevent concurrent control. At any moment exactly one actor owns the browser lease: agent, human,
  service sanitizer, or nobody.
- Keep the agent blind during human control, including screenshots, DOM snapshots, page extraction,
  JavaScript execution, console logs, network bodies, and accessibility-tree values.
- Provide an explicit end state after human control: close the session, return a non-secret outcome,
  or sanitize and resume agent automation when policy allows.
- Fit the existing processing-profile and browser-tool architecture through an adapter rather than
  making Family Assistant responsible for browser hosting.

## Non-Goals

- V1 does not automate payment entry, credential entry, CAPTCHA solving, or legal consent.
- V1 does not guarantee that a merchant site cannot observe what the human types into that site.
- V1 does not make browser sessions durable across process or container restarts. Metadata can be
  durable, but live browser state is initially best-effort.
- V1 does not expose the user's local browser. The controlled browser is a hosted, isolated browser
  runtime owned by the handoff service.
- V1 does not let the agent watch the human work in real time.

## Current State

The existing browser implementation keeps one in-process `BrowserSession` per `conversation_id` in
`src/family_assistant/tools/browser_session.py`. DOM browser tools and Gemini Computer Use tools
share that object, so `/browse` can delegate to `/browse_visual` while preserving page state.

That is the right local primitive, but it is not enough for human handoff:

- sessions are stored in a module-level dictionary and are not externally addressable;
- Chromium is launched headless, so there is no browser surface a human can control;
- there is no lease owner, sensitive-state model, expiry, cleanup, notification, or audit trail;
- existing snapshot and screenshot tools can expose form values if policy does not block them;
- the web UI has no session-control page or remote browser viewport.

## Recommended Architecture

This should be designed as a separate service from the start. Browser hosting, virtual displays,
remote desktop proxying, lifecycle cleanup, and secret-sensitive session policy are heavy enough
that putting them directly into the Family Assistant FastAPI process would blur ownership and make
the assistant harder to operate. Family Assistant should remain the agent, chat, identity, and tool
orchestration application. The browser handoff service should be the browser-control plane.

An in-process prototype can still be useful for proving the tool contract, but it should be treated
as a compatibility shim over the same service interface, not as the target architecture.

```text
Agent/browser tools
        |
        | Browser-control API, lease checked on every command
        v
Family Assistant browser adapter
        |
        | authenticated service call
        v
Browser Handoff Service  <----> service metadata + audit events
        |
        | internal runtime API
        v
Browser Runtime Worker
  - headed Chromium
  - Playwright connection
  - Xvfb/wayland virtual display
  - noVNC websocket endpoint
        ^
        |
Web UI /browser-sessions/{id}
  - authenticated human opens a service-backed handoff URL
  - embedded or redirected remote browser control surface
  - complete/cancel/resume commands call the service
```

### Browser Handoff Service

The handoff service is the system of record for live browser sessions. Family Assistant may mirror a
small amount of metadata for chat display, but the service owns lease state, remote-control tokens,
runtime health, and cleanup.

Responsibilities:

- create and close browser sessions;
- map `conversation_id` to an active `browser_session_id`;
- track session policy state and current lease owner;
- issue short-lived handoff tokens bound to the authenticated target user;
- expose webhook/SSE events that let Family Assistant notify the web client and chat thread when a
  handoff is ready;
- route agent commands to the correct runtime only when the lease owner is `agent`;
- expose human-control metadata and noVNC connection details only when the lease owner is `human`;
- expire idle sessions and close abandoned browser runtimes;
- append audit events without recording page contents or secrets.

Family Assistant responsibilities:

- authenticate the user's request and decide which trusted profile may use browser handoff;
- call the service through a typed client from browser tools;
- render handoff cards in chat and optionally embed the service's control UI;
- record high-level conversation events such as "handoff requested" and "human completed";
- keep secrets and page content from human-control periods out of message history.

### Browser Runtime Worker

Each handoff-capable browser session should run in an isolated runtime boundary. V1 can use a
container per session or a bounded worker pool with one browser context per session; the service API
should not assume either.

Runtime requirements:

- launch headed Chromium under a virtual display from the start of the session;
- expose a Playwright connection for agent tools;
- expose a noVNC websocket for the human UI;
- support pause/resume of agent control by service lease checks, not by trusting the model;
- provide browser lifecycle operations: create context, close page, close context, collect coarse
  metadata such as URL/title, and health check;
- avoid recording video, screenshots, console payloads, network bodies, or clipboard contents by
  default.

V1 should use noVNC because it is mature, simple to operate in containers, and works in a normal web
page. The service should still hide the transport behind a `RemoteBrowserConnection` abstraction so
a future WebRTC implementation can replace it without changing the tool contract.

### Web UI

The handoff service should provide its own minimal control UI because it owns the remote browser
connection and security envelope. Family Assistant can either redirect to that UI or embed it in a
React page at `/browser-sessions` and `/browser-sessions/:sessionId`.

The detail page should be an operational control surface, not a marketing page:

- session status, origin, current lease owner, expiry, and requesting conversation;
- the live browser viewport;
- controls to mark the human step complete, cancel the session, extend the lease, or close the
  browser;
- a concise handoff note from the agent, for example "Review cart and enter payment details";
- a post-completion outcome form with structured choices such as `purchased`, `not_purchased`,
  `needs_agent_resume`, and `cancelled`.

The human UI should not ask the user to paste payment or credential values into Family Assistant
forms or handoff-service forms. Sensitive entry happens only inside the remote merchant/browser
page.

### Agent Tools

Add a browser-only Family Assistant tool backed by the service API:

```text
request_browser_handoff(
  reason: Literal["payment", "credential", "captcha", "legal_consent", "judgment", "other"],
  handoff_note: str,
  expected_origin: str | None,
  allowed_resume: Literal["never", "after_sanitize", "same_page"]
) -> BrowserHandoffRequest
```

Execution behavior:

1. Verify a browser session exists for the conversation and the agent owns the lease.
2. Verify the current page is not already in a non-transferable secret state.
3. Change lease owner from `agent` to `human`.
4. Block all agent browser tools for the session.
5. Create a one-time handoff token and URL.
6. Notify the authenticated user through the web chat, push notification, or Telegram channel.
7. Return only opaque handoff metadata to the model.

The model should then stop browser work and tell the user that the session is ready. It should not
keep trying to inspect checkout state.

Add service-only methods for deterministic UI actions:

```text
complete_browser_handoff(session_id, outcome, human_note)
cancel_browser_handoff(session_id, reason)
extend_browser_handoff(session_id, duration_seconds)
```

These are called by authenticated UI endpoints, not by the LLM.

## Lease And State Model

```text
agent_active
  Agent owns the browser lease and tools may act.

handoff_requested
  Service has created a handoff URL and is notifying the user.

human_active
  Human owns the lease. Agent browser tools are denied and observation is blocked.

human_sensitive
  Human has likely entered payment, credentials, one-time codes, or private account data.
  Default exit is close, not resume.

sanitize_pending
  Service has the lease and is closing sensitive pages or creating a fresh page/context.

agent_resumable
  Policy allows the agent to resume with a sanitized browser capability.

completed
  Human finished the task; live browser context is closed unless resume was explicitly allowed.

cancelled
  Human or service stopped the handoff and closed the session.

expired
  Timeout closed the session.
```

Key policy rules:

- `human_active` and `human_sensitive` are blind states for the agent.
- `human_sensitive` is entered automatically when the handoff reason is `payment`, `credential`, or
  `legal_consent`. The UI should also let the human mark a session sensitive before completing it.
- `same_page` resume is denied for `payment`, `credential`, and `legal_consent` reasons.
- `after_sanitize` resume closes the page controlled by the human and opens a fresh page in the same
  browser context only when origin policy allows it.
- `never` resume closes the browser runtime and returns only the human's structured outcome.

## Resume Policy

Payment and checkout should default to no agent resume. After a human enters card details or places
an order, the page and account session may contain sensitive receipts, addresses, order history, and
payment metadata. The safest default is:

1. human completes or cancels;
2. service closes the browser context;
3. assistant receives a structured event such as `{ "outcome": "purchased" }`;
4. assistant responds based on the human-provided outcome, not page inspection.

There are useful lower-risk cases where resume makes sense, such as a CAPTCHA, cookie consent, or
manual account login where the user wants the agent to continue shopping. Those cases should use
`allowed_resume="after_sanitize"`:

1. human completes the challenge;
2. service closes the human-controlled page;
3. service opens a fresh page in the same context at the approved origin;
4. service runs redaction checks for password, payment, and one-time-code fields;
5. session becomes `agent_resumable`;
6. browser tools can resume with origin-scoped policy.

## API Sketch

Backend API endpoints:

```text
POST /v1/sessions
GET  /v1/sessions/{session_id}
POST /v1/sessions/{session_id}/agent-command
POST /v1/sessions/{session_id}/handoff
POST /v1/sessions/{session_id}/claim
POST /v1/sessions/{session_id}/complete
POST /v1/sessions/{session_id}/cancel
POST /v1/sessions/{session_id}/extend
GET  /v1/sessions/{session_id}/events
GET  /v1/sessions/{session_id}/remote
POST /v1/sessions/{session_id}/close
```

`/remote` should authenticate the user and then mint or proxy a short-lived noVNC websocket URL. It
should not expose raw worker addresses or reusable runtime credentials to the browser.

Family Assistant can expose `/api/browser-sessions/...` as a thin authenticated proxy if that makes
same-origin embedding simpler, but the service API should remain independently usable.

Suggested database tables:

```text
browser_sessions(
  id,
  conversation_id,
  interface_type,
  requested_by_user_id,
  assigned_user_id,
  state,
  lease_owner,
  current_origin,
  handoff_reason,
  allowed_resume,
  handoff_note,
  created_at,
  updated_at,
  expires_at,
  closed_at
)

browser_session_events(
  id,
  browser_session_id,
  event_type,
  actor_type,
  actor_user_id,
  created_at,
  metadata_json
)
```

Do not store screenshots, DOM snapshots, clipboard values, form values, card details, credential
IDs, one-time codes, or full URLs with sensitive query parameters.

The tables above belong primarily to the handoff service. Family Assistant should store only the
opaque `browser_session_id`, `conversation_id`, current coarse state, and chat-visible outcome.

## Integration With Processing Profiles

Browser handoff should be available only to trusted browser profiles. It should be denied for
untrusted email intake, engineer/diagnostic profiles, generic scripting tools, and arbitrary worker
delegation.

Tool policy should match on browser session state:

- deny all browser tools unless the caller owns the session lease;
- deny observation tools when state is `human_active`, `human_sensitive`, or `sanitize_pending`;
- deny `browser_exec` on sessions that are authenticated or human-touched unless explicitly allowed
  by a narrower future design;
- require confirmation for checkout-like actions before handoff is requested;
- require origin scoping for any session that has been authenticated or human-touched.

The existing browser tools can be adapted by replacing `get_browser_session(exec_context)` with a
typed service client. Tool code should receive structured results from the service rather than a raw
Playwright page handle. Raw Playwright handles should stay inside the handoff service.

## Security And Privacy

- Handoff links are one-time, short-lived, and require normal web authentication.
- The session is assigned to a specific user when possible. Other users cannot claim it unless an
  operator policy explicitly allows family-wide handoff.
- The service never sends payment fields, password fields, one-time codes, clipboard content, or DOM
  snapshots from human-control periods to the LLM.
- The agent does not receive live URL changes during human control. At most it receives coarse state
  transitions such as "human completed checkout".
- noVNC traffic is proxied through authenticated service routes or a same-origin Family Assistant
  websocket proxy when the UI is embedded.
- Runtime workers run without access to the application database, config secrets, or host filesystem
  beyond a scratch profile directory.
- Browser profile directories are encrypted at rest if persistent storage is introduced later.
- Idle sessions expire aggressively. Payment handoffs should use shorter defaults than research
  browsing.
- Audit logs store metadata sufficient to debug ownership and lifecycle bugs, not page content.

## User Experience Flow

1. User asks: "Find the best dishwasher-safe lunchbox and buy it if it is under $40."
2. Agent searches, compares options, adds the selected item to cart, and navigates to checkout.
3. Agent calls `request_browser_handoff(reason="payment", allowed_resume="never", ...)`.
4. User receives a chat card or push notification with "Open browser session".
5. User opens `/browser-sessions/{id}`, reviews the cart, enters payment details directly in the
   merchant page, and places or cancels the order.
6. User clicks `Purchased` or `Cancelled` in the Family Assistant page.
7. Service closes the browser context and emits a completion event to the conversation.
8. Assistant replies with a non-secret summary based on the user's selected outcome and note.

## Tactical Alternative

A simpler tactical implementation would expose the current Playwright browser through a debug VNC
endpoint inside Family Assistant and ask the agent to pause voluntarily. That is not recommended. It
is quick, but it leaves the core security property unenforced because the agent could still call
snapshots, screenshots, or JavaScript while the human is entering sensitive data.

A reasonable prototype is an in-process adapter that implements the service client interface and
supports only `request_browser_handoff` plus lease denial. That can validate the assistant-facing
contract. The target architecture should still be a separate handoff service; otherwise Family
Assistant accumulates browser hosting, remote desktop, container lifecycle, and high-risk secret
handling responsibilities that do not belong in the core assistant process.

## Implementation Milestones

### Milestone 1: Service Contract And Policy

- Add typed browser session state and lease-owner models.
- Define the external browser handoff service API and typed Family Assistant client.
- Make existing browser tools check lease ownership through the client before acting.
- Add unit tests for allowed and denied state transitions.

### Milestone 2: Handoff Tool Without Remote Control

- Add `request_browser_handoff`.
- Create durable session metadata and lifecycle events.
- Surface pending handoffs in chat as deterministic UI cards.
- Deny agent browser tools while a handoff is pending.

### Milestone 3: Remote Browser Runtime

- Create the standalone handoff service.
- Launch handoff-capable sessions as headed Chromium in a virtual display.
- Add noVNC proxy endpoints and worker health checks.
- Add cleanup for abandoned runtimes.

### Milestone 4: Human Web UI

- Add service-hosted control pages and optional Family Assistant embedding at `/browser-sessions`
  and `/browser-sessions/:sessionId`.
- Render the remote browser viewport.
- Add complete, cancel, extend, and close controls.
- Add SSE or polling for state changes.

### Milestone 5: Sanitization And Resume

- Implement `after_sanitize` resume for non-payment cases.
- Close the human-controlled page and open a fresh page at the approved origin.
- Redact/check sensitive fields before returning the lease to the agent.
- Keep `payment`, `credential`, and `legal_consent` handoffs closed by default.

### Milestone 6: Hardening

- Add origin policy configuration.
- Add runtime resource limits.
- Add metrics for active sessions, expired handoffs, runtime failures, and denied tool calls.
- Add security regression tests for no model-visible secrets during human control.

## Testing Strategy

Unit tests:

- state transitions reject invalid owner changes;
- browser tools fail closed when lease owner is not `agent`;
- handoff URLs are one-time, user-bound, and expire;
- payment handoffs cannot use `same_page` resume;
- sensitive metadata is redacted before events are stored.

Functional tests:

- agent opens a fixture shop page, fills a cart, requests handoff, and loses tool access;
- authenticated web user claims the handoff and completes it;
- completion closes the runtime for `allowed_resume="never"`;
- CAPTCHA-style handoff can sanitize and resume on a fresh page;
- abandoned handoff expires and closes the runtime.

Browser UI tests:

- `/browser-sessions` lists pending and active sessions;
- detail page renders status, controls, and remote viewport placeholder;
- complete/cancel buttons update state without page reload;
- unauthorized users cannot claim another user's session.

Security regression tests:

- no screenshots, DOM snapshots, console messages, network bodies, clipboard values, passwords, card
  numbers, or one-time codes from `human_active` or `human_sensitive` states appear in tool results,
  message history, logs, or audit metadata;
- agent cannot call `browser_exec` or `browser_screenshot` during human control;
- remote websocket endpoints require authenticated, unexpired handoff claims.

## Open Questions

- Should V1 allow family-wide session claiming, or should every handoff target one canonical user?
- Should the first implementation run one container per session or a small pool of browser workers?
- Which deployment environments can run the virtual display/noVNC stack reliably?
- How much current URL/title metadata should the human UI show in lists without leaking sensitive
  merchant query parameters?
- Should successful payment handoffs always close the merchant session, or should the human be able
  to choose "keep logged in for future tasks" as a separate explicit setting?

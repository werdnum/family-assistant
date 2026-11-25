# Link Agent Wallet Integration

## Context

Family Assistant can browse the web via `browser-server`, sign in to sites without exposing
passwords to the model (`on-demand-browser-autofill.md`), and build UCP carts up to a human checkout
link (`ucp-client-prerequisites.md`). Completing a payment still requires handing the browser or
checkout URL to the user, and direct merchant UCP `complete_checkout` remains gated behind
platform-level merchant onboarding.

Stripe's **Link Agent Wallet** (`api.link.com` / `login.link.com`, with the async Python SDK
`stripe-link-sdk`) provides a consumer-controlled payment layer: an agent submits a spend request
(merchant, amount, line items, and at least 100 characters of context), the user approves it
out-of-band in the Link app or web UI with biometrics/passkey, and Link issues a scoped single-use
(or recurring) virtual card capped at that amount without exposing the user's underlying funding
card.

### Verified live findings (2026-11-24)

Live probes against an Australian Link account (`stripe-link-sdk 0.2.0`), a real approval prompt,
and Stripe's `stripe/link-cli` reference code established what works today and what is constrained:

- **Device-code OAuth works from pure Python** against `POST https://login.link.com/device/code` and
  `/device/token` using Link CLI's public client (`lwlpk_U7Qy7ThG69STZk`), scopes
  `payment_methods.agentic userinfo:read`, and our own `User-Agent` (no `link-cli` User-Agent
  required):
  - `user_info.retrieve()` returns default currency (`aud`), verification status (`not_required`),
    and spend limits (A$500/tx, A$5,000/day, A$20,000/30-day).
  - `payment_methods.list()` returns **all** saved cards and tags restricted ones with
    `capabilities.agentic_payments: {"eligible": false, "ineligibility_reasons": [...]}` rather than
    omitting them.
  - `shipping_addresses.list()` returns structured saved addresses.
  - `spend_requests.create()` enforces a minimum amount (A$0.50 in AUD; A$0.01 returned `400`). Both
    live and `test=True` requests require approval; declining moves the request to `denied`, and
    `cancel()` moves it to `canceled`.
  - Link's approval window displays the amount, currency, `merchant_name`, connection label,
    truncated `context`, and funding card — it does **not** prominently display `merchant_url`.
- **Refresh token rotation is immediate and non-RFC-6749 on error:**
  - `POST /device/token` with `grant_type=refresh_token` returns a new ~1-hour access token and
    rotates the refresh token.
  - The previous refresh token is invalidated immediately: replaying it returns `400` with
    `{"error": {"message": "refresh_token is not active: status: redeemed"}}` (not
    `{"error": "invalid_grant"}`). Replaying a redeemed token does not revoke the newer token.
  - An immediate refresh seconds after connect failed once with `400` while later refreshes
    succeeded; seeding the initial access token at connect time avoids refreshing immediately.
- **Test-mode and LPT constraints:**
  - Per `stripe/link-cli`'s checkout validator, `test=True` virtual cards always decline in Stripe
    test mode, and Link Pay Tokens (LPT) do not support `test=True`.
  - A live probe of `https://stripe.press/checkout` did not expose `.AiAgentPaymentSteering` or a
    Link Pay Token input; LPT is therefore deferred until observed on a live checkout.
- **Gated (`403`) on this client/account — excluded from v1:**
  - `approval_policy.retrieve()` (OAuth application cannot use Approval Policies).
  - `web_bot_auth.sign_url()` (Web Bot Auth signing not enabled for this account).
  - `GET /ucp/catalog/search` (`403 Forbidden` in both test and live mode).

## Decision & Boundary Split

Do **not** mount `link-cli --mcp` into Family Assistant:

- `link-cli --mcp` returns virtual card PAN/CVC in tool output (its `--output-file` escape hatch
  assumes a shared local filesystem between CLI and browser, which does not exist between FA and
  `browser-server`), leaking card credentials into LLM context, provider logs, and FA message
  history.
- `link-cli` manages a single `~/.link-cli` token file, whereas FA is multi-user and already has
  encrypted per-user OAuth token storage.
- Bundling payment authority and Financial Insights behind one MCP server mixes banking read access
  into browsing sessions.

Instead, split responsibilities cleanly between Family Assistant and `browser-server`:

1. **Family Assistant** owns per-user Link OAuth, `stripe-link-sdk` wallet and spend-request
   orchestration, tool policy (`review` + `checked` browser mode), approval relay, and approved
   virtual card retrieval into `SecretStr`-typed fields. **No virtual card PAN, CVC, or expiry ever
   reaches a tool result, LLM context, message history, log line, or `error_logs`.**
2. **`browser-server`** stays **completely Link-agnostic**: it exposes a generic blind payment fill
   endpoint (`POST /v1/sessions/{id}/fill-payment`) that receives an expected top-level HTTPS origin
   and card/billing payload, verifies origin, document nonce, and payment-iframe origins, fills the
   controls, stamps them `data-fa-protected`, and latches readback protection on the session.

### Why FA fetches and forwards the scoped card (not forwarding Link's OAuth token)

Keychute browser autofill (`on-demand-browser-autofill.md`) has `browser-server` fetch secrets
directly because `browser-server` is registered with Keychute as its own `trusted-client` and a
human approves each release specifically for `browser-server`. Link has no way to mint a narrower
token per spend request: forwarding the user's ~1-hour Link bearer token to `browser-server` would
grant a second service full wallet authority (creating spend requests, listing cards and addresses)
and couple `browser-server` to Link's API.

Having FA retrieve the approved virtual card only after confirming `status == "approved"` and
matching the browser's current HTTPS origin — holding it briefly in `SecretStr`-typed models and
posting it to `browser-server` without logging request bodies or including them in exceptions —
keeps Link authority inside FA and keeps `browser-server` provider-neutral.

## 1. Per-User Link OAuth in Family Assistant

Keep Link inside the existing OAuth chokepoints (`user_oauth_connections`, `CredentialEncryption`,
`OAuthCredentialResolver`, `OAuthIntegrationState`, and `filter_oauth_tool_registrations`) so
startup enablement, identity checks, and tool registration gating live in one place. No new database
tables are required.

Keeping Link inside `OAuthIntegrationState` specifically preserves the check that real web
authentication is enabled and `config.users` is populated — preventing a Link wallet from ever being
attached to the shared dev `test_user` identity — as well as Fernet key validation, the scope
allowlist (blocking `financial_insights.*` at startup), and hiding Link tools globally when
disabled.

### Extensions to shared OAuth machinery

1. **Provider spec & `OAuthIntegrationState` support for public device-code clients:**
   - Add a Link provider spec (`provider="link"`, `config_attr="link_integration"`) with default
     scopes `payment_methods.agentic`, `userinfo:read` (no Financial Insights scopes in v1), device
     code / token / revoke endpoints on `https://login.link.com/device/*`, and Link CLI's public
     `oauth_client_id` as the default (overridable by an operator who registers their own Link OAuth
     client, with optional `oauth_client_secret` typed `SecretStr`).
   - Support public OAuth clients in `OAuthIntegrationState` (allowing an empty
     `oauth_client_secret`) and opt Link out of the Google-style taint floor — Link's spend control
     is static tool-call review plus Link's own out-of-band approval rather than a taint-mode gate.
     An unconfigured deployment (no `CREDENTIAL_ENCRYPTION_KEY` / Link not enabled) must remain
     quietly disabled rather than logging startup errors for the default public `client_id`.
2. **Device-code connect flow (`web/routers/oauth_integration.py`):**
   - Mount session-authenticated endpoints under `/api/integrations/link` (via
     `get_current_session_user`, never reachable via `DIAGNOSTICS_READONLY_TOKEN`):
     - `GET /api/integrations/link` — connection status (never returns tokens).
     - `POST /api/integrations/link/device/start` — requests a device code from
       `login.link.com/device/code` (`connection_label="Family Assistant"`), keeps the pending
       device code in a 10-minute in-memory map bound to `(user_id, flow_id)`, and returns the
       verification URI, user phrase, expiry, and poll interval.
     - `POST /api/integrations/link/device/poll` — verifies `flow_id` belongs to the signed-in user,
       polls `/device/token`, and on success consumes the pending entry, fetches the connected
       account email via `stripe-link-sdk`'s `user_info.retrieve()`, upserts the encrypted refresh
       token into `user_oauth_connections`, seeds `OAuthCredentialResolver`'s in-memory access-token
       cache with the freshly minted `access_token`, and notifies the user.
     - `DELETE /api/integrations/link` — best-effort `POST /device/revoke`, then deletes the row.
   - Settings → Connected Accounts renders a "Connect Link" card showing the verification link and
     short phrase, polling until approved or expired.
3. **Rotating refresh tokens, initial seeding, and dead-token detection in
   `OAuthCredentialResolver`:**
   - Omit `client_secret` from `grant_type=refresh_token` requests when the provider's
     `oauth_client_secret` is empty.
   - Seed the in-memory token cache on connect so FA uses the initial ~1-hour access token before
     refreshing.
   - When a refresh response includes a new `refresh_token`, encrypt and persist it to
     `user_oauth_connections` **under the existing per-user single-flight `asyncio.Lock`**,
     conditioned on matching `credential_generation` (without bumping `credential_generation`),
     before releasing the lock and caching the new access token.
   - Allow the provider spec to classify its own terminal invalid/revoked refresh responses so
     Link's `400 {"error": {"message": ...}}` flips the connection to `needs_reauth` and notifies
     the owning user instead of retrying forever as a transient error.

## 2. Link Wallet Tools, Profiles, and Review Policy

Add `src/family_assistant/services/link_wallet.py` wrapping `stripe-link-sdk`'s `AsyncClient` with
an `async def get_access_token()` backed by `OAuthCredentialResolver` bound strictly to
`exec_context.user_id` (never to a user ID passed in tool arguments).

### Tools (`src/family_assistant/tools/link_wallet.py`)

1. **`link_wallet_status`** (`read_only`):
   - Fetches `user_info.retrieve()` and `payment_methods.list()`.
   - Labels cards using `capabilities.agentic_payments.eligible` (defaulting to eligible when
     `capabilities` is absent) so the model never picks a restricted card, returning only redacted
     summaries (`id`, `brand`, `last4`, `is_default`, `eligible`, `ineligibility_reasons`, default
     currency, spend limits, and verification requirement). Returns no shipping addresses.
2. **`link_list_shipping_addresses`** (`read_only`, `sensitive_data`):
   - Returns saved shipping addresses from `shipping_addresses.list()` as a separate tool so digital
     checkouts never read home addresses and shopping delegations can start `unchecked` without
     stuffing street addresses into the initial brief.
   - Under the central executor chokepoint, any successful call records a `sensitive_read`, flipping
     an `unchecked` browser session to `checked`.
3. **`link_create_spend_request`**:
   - Creates a `card` spend request (`amount` integer minor units, `currency`, `context` ≥100 chars,
     `merchant_name`, `merchant_url`, optional `payment_details`, `line_items`, `totals`,
     `expires_at`, `recurring`) with a fresh opaque `idempotency_key` and `request_approval=True`.
   - **Mechanical `merchant_url` origin binding:** When called with an active browser session,
     verify that `merchant_url`'s origin matches the browser session's current HTTPS origin and
     refuse mismatches in code. This closes the gap where Link's approval window shows
     `merchant_name` prominently but hides `merchant_url`.
   - Polls briefly (matching `_AUTOFILL_WAIT_SECONDS` in `browser_autofill.py`). If still
     `pending_approval`, returns `{status, spend_request_id, approval_url}` with guidance to share
     `approval_url` with the user and stop until they approve and ask to continue. On
     `requires_action`, returns `{status, resolution, display_message, action_url}` so the model
     branches on `resolution` (`auto_resume` vs creating a new request).
   - Never requests `include=["card"]`; returns metadata only.
4. **`link_check_spend_request`** / **`link_cancel_spend_request`**:
   - Inspect status / `next_action` (no credential fields) or cancel an unused request.
5. **`browser_fill_payment`**:
   - Parameters: `spend_request_id`, optional `field_refs` (for ambiguous card forms), optional
     `include_billing_address: bool = True`.
   - Verifies the conversation has an active `RemoteBrowserBackend` session, verifies the spend
     request is `approved` and belongs to the acting user, and verifies the browser's current HTTPS
     origin matches the approved spend request's `merchant_url` **before** fetching
     `spend_requests.retrieve(id, include=["card"])`.
   - Wraps the returned PAN, CVC, expiry, and billing address in `SecretStr` fields, invokes
     `browser-server`'s `POST /v1/sessions/{id}/fill-payment`, and returns only
     `{status, origin, filled_fields, reason}`.

### Profile placement, `unchecked → checked`, and review policy

- **Available in both `browser_profile` and `credential_browser_profile`** (with
  `browser_fill_payment` also in `browser_visual_profile` and `credential_browser_visual_profile`):
  - `credential_browser_profile` opens a separate browser context (discarding cookie-backed guest
    carts built in `browser_profile`), disables `browser_extract`, and runs in `checked` mode from
    step 1 (paying a `ToolCallReviewer` call on every search/navigation step).
  - Allowing Link tools in `browser_profile` lets product discovery, comparison, and cart building
    run `unchecked` with full browsing tools, while Keychute password autofill keeps its
    from-creation isolation on `credential_browser_profile` for reusable passwords.
- **Every Link tool marks the browser scope `checked`:** In addition to
  `link_list_shipping_addresses` recording a `sensitive_read`, every Link tool calls
  `mark_browser_scope_checked` on the active browser scope. Once a session touches wallet status,
  addresses, spend requests, or payment fill, all subsequent browser actions are reviewed.
- **Static `review` on `link_create_spend_request`, no double-tap FA confirmation:**
  - `link_create_spend_request` carries a static `tools_policy` rule with `decision: "review"`,
    which runs regardless of taint mode or `unchecked` state. `ToolCallReviewer` checks that
    `merchant_name`, `merchant_url`, `amount`, currency, and items align with the user's request and
    the active checkout page.
  - Link's own out-of-band biometric prompt serves as the human approval; FA does not add a second
    confirmation modal by default. Operators can override `link_create_spend_request` to `confirm`
    or `allow` via standard `operator_tools_policy`.
  - `browser_fill_payment` requires no extra confirmation: its safety comes from filling only an
    already-approved spend request into a page whose HTTPS origin matches the approved
    `merchant_url`.

## 3. `browser-server`: Blind Payment Fill

Extend `browser-server`'s existing autofill plumbing (`registry.py`, `runtime.py`, `main.py`) with a
Link-agnostic endpoint `POST /v1/sessions/{id}/fill-payment` (service-token auth, agent lease
required, serialized under `self.locks[session_id]`).

### Fill behavior (`card` mode)

1. **Origin and document pinning:**
   - Refuse plaintext HTTP documents before touching any credential.
   - Verify the top-level document's exact HTTPS origin matches `expected_origin` (derived from the
     approved spend request's `merchant_url`), refusing mismatches with `wrong_origin`.
   - Pin the top-level document with a nonce across target resolution and filling so a mid-fill
     navigation aborts cleanly with `target_invalidated`.
2. **Controlled payment-iframe traversal:**
   - Unlike password autofill (which refuses `in_iframe`), card inputs routinely live inside hosted
     payment iframes (Stripe Elements, Adyen, Braintree, Shopify, Square, etc.).
   - Traverse the top-level document and HTTPS child frames whose origin is either the top-level
     origin or on a built-in fail-closed allowlist of known payment-processor origins. Refuse any
     target in an unrecognized third-party frame origin with `untrusted_payment_frame`.
   - Resolve card number, expiry (combined `MM/YY` or split month/year), CVC, cardholder name, and
     optional billing address via `autocomplete` (`cc-number`, `cc-exp`, `cc-exp-month`,
     `cc-exp-year`, `cc-csc`, `cc-name`, billing address tokens), labels/names, or explicit
     `field_refs` when ambiguous. Refuse cleanly on `ambiguous_fields` or `no_eligible_field`.
3. **Fill-time readback protection (monotonic latch):**
   - Stamp every filled control with `data-fa-protected="true"` **before** filling (including inside
     payment iframes). The existing snapshot walker predicate (`isProtected`) already hides
     `data-fa-protected` values in every session.
   - On fill, monotonically latch a session-level `readback_protected` flag in `registry.py` and set
     `worker.mask_protected = True` in `runtime.py`, reusing the existing chokepoints that deny
     `exec`, `extract`, clipboard transfer chords, and unmasked screenshots for the remainder of the
     session. Keychute password autofill remains gated on `autofill_enabled` from session creation.
   - Clear credential buffers after filling; never log request payloads or return any card digits or
     CVC in responses or errors.
   - Return `{status: "filled", origin, filled: [{kind, frame_origin}]}` so the agent can review the
     checkout summary and submit the order, or call `browser_request_handoff` if 3DS/CAPTCHA
     appears.

## Deliberate Simplifications and Accepted Trade-offs

- **Virtual `card` mode only in v1; Link Pay Tokens (LPT) deferred:** `.AiAgentPaymentSteering` was
  not observed on live `stripe.press` checkout and LPT has no `test=True` mode, whereas virtual
  cards work on any standard web checkout. Revisit LPT once the steering surface is observed live.
- **WebMCP bridge deferred to a separate design/PR:** Generic `document.modelContext` support in
  `browser-server` is useful independently of payments and is not required for Link checkout.
- **No Link UCP proxy, Web Bot Auth, Link Balance approval policies, or Financial Insights in v1:**
  The first three returned `403` on the live probe; Financial Insights scopes stay unrequested so
  banking read access does not enter browsing/payment tokens.
- **Borrowed `link-cli` public OAuth `client_id`:** Stripe may restrict or rotate the public CLI
  client at any time. `link_integration` allows overriding `oauth_client_id` (and optional
  `oauth_client_secret`) if a dedicated Link OAuth app is registered later.
- **Pre-fill `browser_exec` residual in `browser_profile`:** In an ordinary `browser_profile`
  session, `exec` is available before `fill-payment` latches readback protection. Because `exec`
  runs only in the main frame, hosted cross-origin payment iframes are unreachable from it; on rare
  top-frame card forms, a listener injected by an earlier `exec` could observe the filled single-use
  card capped at the approved amount. Accepted as proportionate for single-use Link cards (while
  Keychute keeps from-creation isolation for reusable passwords).
- **Approval outlasting the browser session:** `browser-server` sessions expire after 15 minutes
  idle. If the user approves after session expiry, a guest cart is lost and the task restarts (same
  accepted trade-off as `on-demand-browser-autofill.md`; no background approval daemon or
  completion-callback webhook in v1).
- **In-memory pending connect flows & refresh-rotation crash window:** Unfinished device-code
  connect flows live in memory (10-minute TTL); restarting during connect requires clicking Connect
  again. Connected accounts persist encrypted in the database and survive normal restarts, except in
  the narrow window where the process crashes after Link redeems a refresh token and before the
  database update commits (requiring one reconnect).
- **No separate per-session payment fill cap, `test_mode_only` switch, or FA-side spend-request
  idempotency cache:** Each Link spend request requires explicit user approval and caps spend to a
  single approved amount; tool-call review prevents approval spam.
- **Read-back masking, final totals, and 3DS:** Read-back masking is best-effort once the approved
  merchant page receives payment details. If shipping/tax changes the total after approval, the
  agent updates/re-approves or cancels and creates a new request. 3DS/KYC `next_action` URLs and
  in-page challenges stay human-driven via `action_url` or `browser_request_handoff`.

## Milestones and Verification

1. **`family-assistant` PR 1 — Link device-code OAuth + wallet and spend-request tools:**
   - Extend `OAuthProviderSpec` / `OAuthIntegrationState` for public clients and taint-floor
     opt-out; extend `OAuthCredentialResolver` for locked rotating `refresh_token` persistence,
     initial `access_token` seeding, and provider-specific dead-token detection; add device-code
     endpoints and Settings UI card.
   - Add `link_wallet_status`, `link_list_shipping_addresses`, `link_create_spend_request`,
     `link_check_spend_request`, `link_cancel_spend_request`, the static `decision: "review"` rule,
     mechanical `merchant_url` origin check, and `mark_browser_scope_checked` latching.
   - Verify with real SQLite/PostgreSQL database tests + `httpx.MockTransport` fake Link server:
     rotating refresh tokens, Link `400` redeemed/revoked error → `needs_reauth`, dev `test_user`
     refusal, scope-allowlist enforcement, ineligible-card tagging, `unchecked → checked` flip,
     `merchant_url` mismatch refusal, `pending_approval`, and `requires_action` handling.
2. **`browser-server` PR 1 + `family-assistant` PR 2 — Blind payment fill (`card` mode):**
   - `browser-server`: add Link-agnostic `POST /v1/sessions/{id}/fill-payment` with exact top-level
     origin + document nonce pinning, fail-closed payment-iframe allowlist, `data-fa-protected`
     stamping, and fill-time readback protection latching.
   - `family-assistant`: add `browser_fill_payment` (`SecretStr`-wrapped card retrieval, approved
     status and origin verification before `include=["card"]`, wired into ordinary/credential and
     visual browser profiles).
   - Verify end-to-end against the real `browser-server` ASGI app + local multi-origin iframe
     checkout harness + fake Link API: cross-origin payment-iframe fill, untrusted third-party
     iframe refusal, origin mismatch refusal, mid-fill navigation `target_invalidated`, masked
     snapshots/screenshots and denied `exec`/`extract`/clipboard after fill, and zero PAN/CVC bytes
     in FA tool results, transcripts, or logs.

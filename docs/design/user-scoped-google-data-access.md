# User-Scoped Access to Personal Google Data (Gmail & Drive)

## Status

Proposed for approval before implementation.

## Problem

Family Assistant serves multiple household members through a unified identity system
(`docs/design/unified-user-identities.md`): every interface resolves to a canonical `user_id` that
is already available during tool execution (`ToolExecutionContext.user_id`). But the assistant has
no access to the personal data users actually ask about most: their own email and files.

- "Did the school send the excursion permission form?" — the assistant cannot look, unless the email
  happened to be forwarded to the assistant's intake address.
- "Find the tax PDF my accountant shared with me" — no Drive access at all.

Today's integrations are all deployment-scoped, not user-scoped: one shared CalDAV calendar, one
Home Assistant instance, MCP servers with a single server-wide token. There is no per-user
credential storage, no OAuth token persistence, and no encryption-at-rest for credentials.

Any Gmail/Drive integration must be **user-scoped**:

1. When the assistant acts on behalf of user A, it must be able to read A's mailbox and Drive — and
   structurally unable to read B's.
2. Background and ambient contexts (reminders, event handlers, email intake) must not silently
   inherit mailbox access.
3. Mailbox and Drive content is attacker-addressable input (anyone can email you), so ingesting it
   must compose with the prompt-injection containment machinery rather than bypass it.

## Goals

- Per-user "Connect Google account" / disconnect flow in the web UI, bound to the authenticated
  canonical user.
- Durable, encrypted-at-rest storage of per-user OAuth refresh tokens.
- Read-only Gmail tools (search, read message, fetch attachment) and Drive tools (search, fetch
  file) that operate strictly as the acting user of the current turn.
- Fail-closed behavior everywhere: no connection → actionable error; no acting user → tools
  unavailable; no encryption key → feature disabled.
- Taint-correct results: Gmail/Drive content enters the turn as classified untrusted input via the
  runtime taint machinery (`docs/design/runtime-taint-machinery.md`).

## Non-Goals

- **Write actions** (send email, create/modify Drive files, delete anything). Future work, gated
  behind confirmation and taint sink policy. v1 requests only read-only OAuth scopes, which also
  caps the blast radius of a leaked token.
- **Ambient mailbox sync / background indexing** of email or Drive into the vector store. The
  runtime taint design explicitly calls this out as a separate connector design. v1 is
  interactive-only: the user asks, the assistant searches.
- **Replacing CalDAV with per-user Google Calendar.** Natural follow-up once the connection
  infrastructure exists, but out of scope.
- **Cross-user sharing** ("check whether my partner got the school email"). v1 is strictly
  self-scoped; sharing grants are future work with their own consent design.
- **Non-Google providers.** The storage and resolver are shaped so a second provider slots in
  (provider column, provider-keyed resolver), but only Google is implemented.

## Alternatives Considered

- **MCP servers (e.g. a Gmail MCP server per user).** `MCPToolsProvider` establishes one session per
  configured server at startup with server-wide auth; there is no per-user session or per-call
  auth-header mechanism. Running N server instances for N users would multiply configuration, bypass
  taint provenance, and put refresh tokens in `config.yaml` (plaintext). Native tools give us
  per-user credential resolution at the execution-context chokepoint, tool metadata tags, and
  provenance-tiered taint — none of which MCP output carries today.
- **Email forwarding / existing email intake.** Already supported, but only covers mail explicitly
  addressed or forwarded to the assistant. It cannot answer "search my mailbox," and it cannot see
  Drive.
- **Service account with domain-wide delegation.** Only works for Google Workspace domains, not
  consumer `@gmail.com` accounts, and grants a standing installation-wide superpower that is much
  worse than per-user consented tokens.

## Design Overview

Three layers, each independently testable:

1. **Connections** — OAuth flow + encrypted storage (`user_google_connections` table,
   `GoogleConnectionsRepository`, web endpoints, settings UI).
2. **Credential resolution** — `GoogleCredentialResolver` service, injected into the tool execution
   context by the tool executor; strictly keyed by the turn's acting user.
3. **Tools** — read-only Gmail/Drive local tools calling Google REST APIs via an injectable async
   backend, emitting taint provenance per result.

### 1. Connections

#### Data model

New table `user_google_connections` (Alembic migration):

| Column                    | Type              | Notes                                            |
| ------------------------- | ----------------- | ------------------------------------------------ |
| `id`                      | int PK            |                                                  |
| `user_id`                 | string(255)       | canonical user id; unique together with provider |
| `provider`                | string(64)        | `"google"` in v1                                 |
| `provider_account_email`  | string(255)       | the Google account that was connected            |
| `scopes`                  | JSON list[str]    | granted scopes as returned by the token exchange |
| `refresh_token_encrypted` | text              | Fernet ciphertext, never logged                  |
| `credential_version`      | int               | incremented on every refresh-token write         |
| `status`                  | string(32)        | `active` \| `needs_reauth`                       |
| `created_at`/`updated_at` | datetime          |                                                  |
| `last_used_at`            | datetime nullable | updated on successful API use                    |

Access tokens are cached **in memory only**, keyed by `(connection_id, credential_version)`, with
expiry and an asyncio lock per connection to serialize refreshes. They are never persisted.
`credential_version` is bumped whenever the stored refresh token changes (reconnect, account
switch), so a reconnect to a different Google account can never keep serving cached tokens for the
previous account. Disconnect and `needs_reauth` transitions additionally evict the cache entry
eagerly.

Repository access follows the existing pattern: `db.google_connections` on `DatabaseContext`,
returning Pydantic models.

#### Encryption at rest

Refresh tokens are encrypted with Fernet (`cryptography` is already a transitive dependency; add it
as a direct dependency). The key comes from a new `CREDENTIAL_ENCRYPTION_KEY` environment variable
(maps to `google_integration.credential_encryption_key`, redacted from logged/exported config like
`APNS_AUTH_KEY`).

- Key unset → the Google integration is disabled: connect endpoints return a clear error, tools are
  not registered. Fail closed, no plaintext fallback.
- Key present but a stored token fails to decrypt (rotated/wrong key) → mark the connection
  `needs_reauth` and surface an actionable error; never crash the turn pipeline.
- Future key rotation via `MultiFernet` (accept-old/encrypt-new) is noted but not built in v1.

#### OAuth flow (web only)

New router `src/family_assistant/web/routers/google_integration.py`, session-authenticated (normal
web auth; explicitly **not** covered by `DIAGNOSTICS_READONLY_TOKEN`):

- `GET /api/integrations/google` — connection status for the settings UI (account email, scopes,
  status; never tokens).
- `GET /api/integrations/google/authorize` — starts the authorization-code flow. Generates a random
  nonce, stores `{nonce, user_id}` server-side (session), redirects to Google's consent screen with
  `state=nonce`, `access_type=offline`, `prompt=consent`, and the requested scopes: the configured
  data scopes (v1 default: `gmail.readonly`, `drive.readonly`) plus `openid` and `email`, which are
  always appended in code — not operator-configurable — so the callback can identify the connected
  account.
- `GET /api/integrations/google/callback` — validates `state` against the stored nonce **and** that
  the session's canonical user matches the user who initiated the flow, exchanges the code
  (server-side, client secret from `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`), fetches
  the account email from the ID token / userinfo, and upserts the connection row for the **session's
  canonical user**. The Google account email is recorded for display but plays no role in
  authorization — the binding is session user → connection.
- `DELETE /api/integrations/google` — best-effort token revocation against Google's revoke endpoint,
  then deletes the row.

Telegram gets no connect flow; users connect once via the web UI. The frontend adds a "Connected
accounts" section to settings showing status, granted scopes, a connect/reconnect button, and
disconnect.

Deployment note: a personal Google Cloud OAuth client in "testing" mode with the household members
as test users is sufficient; no Google verification is required for this deployment model (refresh
tokens for test-mode clients expire after 7 days, which the `needs_reauth` path handles; operators
who want durable tokens move the OAuth client to production mode, which for these sensitive scopes
may involve Google's verification process — documented in the user guide, not worked around in
code).

### 2. Credential resolution — the scoping chokepoint

A single service enforces user scoping:

```python
class GoogleCredentialResolver:
    async def access_token_for(
        self, exec_context: ToolExecutionContext, scope: GoogleScope
    ) -> GoogleAccessToken:
        """Return a valid access token for the turn's acting user, refreshing if needed.

        Raises GoogleNotConnectedError / GoogleReauthRequiredError /
        GoogleNoActingUserError — all rendered as actionable tool errors.
        """
```

Deliberate properties:

- The resolver takes the **execution context**, not a user id. Tools have no user parameter, so the
  LLM cannot address another user's data; there is no code path from tool arguments to the
  credential lookup.
- `exec_context.user_id is None` (system/ambient contexts) → `GoogleNoActingUserError`, fail closed.
  Delegation and threaded wake contexts that propagate the originating `user_id` keep working
  unmodified — the acting user follows the existing context plumbing.
- The resolver is constructed once (DI at app wiring, like `home_assistant_client`) and exposed on
  `ToolExecutionContext` as `google_credentials`, following the established constructor-injection
  pattern in `ToolExecutor._build_execution_context()`.
- Refresh failures with `invalid_grant` (revoked/expired) flip the connection to `needs_reauth` and
  notify the owning user through the existing user-scoped notification path, so the user learns
  immediately rather than at their next request.

### 3. Tools

New module `src/family_assistant/tools/google_data.py`, registered in `tools/__init__.py` +
`config.yaml` per the standard two-place registration:

| Tool                   | Behavior                                                                                                                    |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `gmail_search`         | Gmail query syntax; returns id, thread id, date, from, to, subject, snippet per message (bounded page size)                 |
| `gmail_get_message`    | Full message by id: parsed headers + text body (HTML converted to text); lists attachment metadata                          |
| `gmail_get_attachment` | Fetches one attachment into the existing attachment registry; returns an attachment reference                               |
| `drive_search`         | Drive query; returns id, name, mimeType, owner, modifiedTime, webViewLink                                                   |
| `drive_get_file`       | Exports Google-native docs to text / downloads small text files inline; binary or large files go to the attachment registry |

Implementation notes:

- Direct REST calls (`gmail/v1`, `drive/v3`) through an injectable async backend (`GoogleApiBackend`
  protocol on the resolver/tools, same test-seam pattern as `tools/video_backends.py`), not the sync
  `google-api-python-client`. Tests use a fake backend; no mocking of internals.
- Large results compose with the existing large-result-to-attachment conversion and the global
  `read_text_attachment` tool.
- Result size is bounded (default page sizes, body truncation with an explicit truncation marker) so
  a hostile mailbox cannot blow out the context window.

#### Taint provenance

All five tools are tagged `OUTPUT_UNTRUSTED`. Statically, that means the runtime records an
`unknown_external` taint source for every result — the correct fail-safe default. Graduated tiers
are layered on top as follows.

**Classification.** Tier reduction below `unknown_external` requires the same evidence contract as
inbound email intake (`email_intake/taint.py::email_initial_taint_source`): the sender must match
the existing `email_intake.known_contact_sender_addresses` / `recognized_machine_sender_addresses`
allowlists **and** sender authentication must pass. A bare `From` header match is never sufficient —
`From` is attacker-controlled. For Gmail-fetched messages, authentication evidence comes from the
`Authentication-Results` header that Gmail's own delivery pipeline stamps on received mail: the
classifier parses only the topmost `Authentication-Results` instance with authserv-id
`mx.google.com` (receivers strip inbound headers claiming their own authserv-id per RFC 8601, and
taking only the topmost instance is a second defensive layer) and requires `dmarc=pass`, mirroring
`email_authentication_passed`. Any parse failure, missing header (e.g. self-sent mail in the Sent
folder), or non-pass result classifies as `unknown_external`. Drive results are always
`unknown_external` in v1 — Drive has no equivalent authentication evidence, and file ownership
metadata does not establish who authored the content.

**Runtime path.** The current runtime cannot honor per-result tiers on a statically tagged tool:
`_record_result_taint` derives its source from the static descriptor alone, and the
`OUTPUT_UNTRUSTED` tag adds an `unknown_external` source unconditionally, which dominates any lower
tier under the max rule. Milestone 2 therefore includes a small, general runtime extension: a single
atomic helper, e.g.

```python
def record_tool_result_taint(
    exec_context: ToolExecutionContext, call_id: str, source: TaintSource
) -> None:
    """Add `source` to the turn tracker AND register it as this call's result provenance."""
```

There is deliberately no standalone "mark as recorded" flag: registering result provenance and
adding the taint source are one operation, so no code path can suppress the fallback without having
contributed an actual source. `_record_result_taint` consults the per-call registry: when one or
more explicit sources were registered for this `call_id`, they are the result's provenance and the
static-descriptor source is skipped; for every call with an empty registry — including a Gmail tool
code path that forgets to classify — the unconditional `unknown_external` static source applies as
today. Provenance is computed exclusively by first-party connector code from the authentication
evidence above; it is never derived from LLM-supplied arguments or message content. Tests cover both
directions: an authenticated known-contact message yields `known_contact`, and a result whose
classification path is skipped still taints the turn as `unknown_external`.

**Enforcement precondition.** The taint matrix only blocks anything when `taint_policy.mode` is
`enforce`; the shipped default is `observe`, which converts confirm/deny outcomes to audit events.
Observe mode is not containment: an interactive profile keeps all of its existing communication and
state-changing tools, so a prompt-injected email could drive them — read-only Google scopes
constrain only the Google tools themselves, not the rest of the profile. Therefore the Google data
tools are registered **only when the effective taint policy is enforcing**, validated at startup:

- `taint_policy.mode` must be `enforce`, and
- the effective matrix (defaults + overrides + `operator_minimum`) must yield at least `confirm` at
  the `unknown_external` tier for the sink classes `arbitrary_external_message`,
  `attacker_addressable_egress`, `sandbox_network`, and `sensitive_read_broadening`.

If the integration is configured but these conditions fail, the tools are not registered, a startup
error-log entry states why, and the integration status endpoint reports the unmet precondition.
There is no partial fallback. For the production deployment this means flipping taint enforcement on
(per the runtime taint design's rollout plan) is a prerequisite to shipping Milestone 2, not a
nice-to-have.

With that precondition, the turn's taint state does its job: after a Gmail read, the matrix gates
attacker-addressable egress and sensitive-read broadening with real confirm/deny outcomes, and
graduated tiers keep authenticated household mail low-friction.

#### Tool policy defaults

In `defaults.yaml`:

- Tools carry tags `google_personal_data` + `OUTPUT_UNTRUSTED`.
- Allowed in interactive trusted profiles (`default_assistant` tiers, `complex_tasks`).
- **Denied by default in ambient/untrusted-input profiles**: `email_intake`, `reminder`,
  event-handler and browser profiles. Rationale: those profiles process attacker-influenced
  triggers; combining that with mailbox read is exactly the sensitive-read-broadening pattern the
  taint design worries about, and v1 has no product need for it. Operators can override per-profile
  deliberately.
- Not added to `global_tools_policy` (that is reserved for must-always-work plumbing tools).

## Security Analysis (Rule of Two)

- `default_assistant` is [BC] today (sensitive data + state/communication), anchored on
  authenticated users. These tools introduce \[A\]: mailbox and Drive content is
  attacker-addressable. The composition is made safe not by pretending mail is trusted but by:
  1. **Enforced runtime taint (hard precondition)** — the tools do not register unless
     `taint_policy.mode` is `enforce` and the matrix floor above holds, so after a Gmail/Drive read
     the profile's existing communication and state-changing tools are actually gated
     (confirm/deny), not merely audited. This is what prevents the [BC] profile from becoming an
     un-gated [ABC] agent.
  2. **Read-only scopes** — the OAuth grant itself cannot send mail or write files, so the Google
     tools add no egress capability of their own.
  3. **Profile policy** — ambient profiles that already process untrusted triggers cannot also read
     the mailbox.
- **Cross-user isolation** is structural: connection rows are keyed by canonical `user_id`; the
  resolver reads the acting user from the execution context only; tool schemas contain no
  user-addressable parameter. A prompt-injected model cannot ask for someone else's mailbox because
  no reachable code path takes a user identifier as input.
- **Token safety**: refresh tokens Fernet-encrypted at rest; access tokens in memory only; both
  redacted from config dumps and diagnostics export; tool results and errors never include tokens.
  The new endpoints are excluded from the diagnostics read-only token's scope.
- **CSRF/linking safety**: `state` nonce stored server-side in the session, callback bound to the
  initiating session's canonical user — a crafted callback URL cannot attach an attacker's Google
  account to a victim's user (login-CSRF) because the connection is written to the session user
  resolved at callback time, and a mismatch with the flow-initiating user aborts.

## Configuration

```yaml
google_integration:
  oauth_client_id: ""        # env GOOGLE_OAUTH_CLIENT_ID
  oauth_client_secret: ""    # env GOOGLE_OAUTH_CLIENT_SECRET (secret, redacted)
  credential_encryption_key: ""  # env CREDENTIAL_ENCRYPTION_KEY (secret, redacted)
  scopes:                    # operator-tunable DATA scopes; v1 defaults
    - "https://www.googleapis.com/auth/gmail.readonly"
    - "https://www.googleapis.com/auth/drive.readonly"
```

The identity scopes `openid` and `email` are **not** part of the operator-tunable `scopes` list: the
authorize endpoint always appends them in code, so the callback reliably receives an ID token
identifying the connected account regardless of operator configuration.

Integration is enabled only when client id, secret, and encryption key are all present.

## Milestones

Each lands independently with tests and passes `poe test`.

1. **Connection infrastructure** — config models, migration, repository, Fernet encryption helper,
   OAuth endpoints with a fake Google token/userinfo server in functional web tests, settings UI
   section, `GoogleCredentialResolver` with refresh + `needs_reauth` handling. Deliverable: a user
   can connect/disconnect and the row round-trips encrypted.
2. **Gmail read tools** — `gmail_search`, `gmail_get_message`, `gmail_get_attachment` against a fake
   `GoogleApiBackend`; the atomic `record_tool_result_taint` runtime extension with authenticated
   sender classification; the taint-enforcement startup precondition (tools unregistered + surfaced
   reason when unmet); policy defaults; per-user isolation tests (two connected users, assert each
   context reads only its own data; unconnected and no-acting-user contexts fail closed with
   actionable messages). Shipping this milestone to production requires the deployment to run
   `taint_policy.mode: "enforce"`.
3. **Drive read tools** — `drive_search`, `drive_get_file` incl. export/attachment-registry paths;
   same test posture.
4. **Docs & prompts** — USER_GUIDE section (connect flow, what the assistant can/can't do, token
   expiry in test-mode deployments), system prompt guidance in `prompts.yaml` (tools act as the
   requesting user; suggest connecting when not connected), AGENTS.md env var documentation.

## Future Work

- Write actions (send/reply/label, Drive upload) behind `confirm` + taint sink classes
  (`arbitrary_external_message`, `artifact_write`).
- Ambient mailbox ingestion/indexing with visibility labels per owner (separate design, per the
  runtime taint doc's connector split).
- Per-user Google Calendar via the same connections.
- Cross-user sharing grants with explicit consent UX.
- Encryption key rotation via `MultiFernet`.

## Testing Strategy

- **Unit**: encryption round-trip + wrong-key behavior; resolver fail-closed matrix (no user, no
  connection, needs_reauth, disabled integration); refresh single-flight under concurrency.
- **Functional (web)**: full connect/disconnect flow against stubbed Google endpoints; status
  endpoint redaction; auth required on all routes.
- **Tool tests**: fake `GoogleApiBackend` (DI, no monkeypatching); cross-user isolation; taint
  provenance tiers on results; truncation/attachment conversion paths.
- **Policy tests**: tools visible in `default_assistant`, denied in `email_intake`/`reminder`;
  conformance with the per-profile tool inventory endpoint.

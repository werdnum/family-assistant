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
- Attachments registered by `gmail_get_attachment` / `drive_get_file` carry their classified
  `taint_metadata` (the source state computed for the containing message/file) in the registry
  metadata, so provenance survives storage: a later `read_text_attachment` re-derives the graduated
  tier from that metadata via the existing artifact-provenance merge path instead of collapsing an
  authenticated known-contact attachment to `unknown_external` — and an attachment registered
  without metadata still falls back to `unknown_external`, keeping the fail-safe direction.
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
    exec_context: ToolExecutionContext, source: TaintSource
) -> None:
    """Add `source` to the turn tracker AND register it as the current call's result provenance."""
```

Tool implementations today never see their own call id — it stays inside the provider wrapper — so
the helper takes it from the execution context instead: `ToolExecutionContext` gains a
`tool_call_id: str | None` field that `LocalToolsProvider` populates for each invocation (the
wrapper already receives `call_id` alongside the arguments; it stamps the per-call context before
dispatch). Connector code never handles raw call ids, and the LLM cannot influence the value.

There is deliberately no standalone "mark as recorded" flag: registering result provenance and
adding the taint source are one operation, so no code path can suppress the fallback without having
contributed an actual source. `_record_result_taint` consults the per-call registry: when one or
more explicit sources were registered for this call id, they are the result's provenance and the
static-descriptor source is skipped; for every call with an empty registry — including a Gmail tool
code path that forgets to classify — the unconditional `unknown_external` static source applies as
today. Provenance is computed exclusively by first-party connector code from the authentication
evidence above; it is never derived from LLM-supplied arguments or message content. Tests cover both
directions: an authenticated known-contact message yields `known_contact`, and a result whose
classification path is skipped still taints the turn as `unknown_external`.

**Taint enforcement interaction.** The taint matrix only blocks anything when `taint_policy.mode` is
`enforce`; the shipped default is `observe`, which converts confirm/deny outcomes to audit events.
Observe mode is not containment: an interactive profile keeps all of its existing communication and
state-changing tools, so a prompt-injected email could drive them — read-only Google scopes
constrain only the Google tools themselves, not the rest of the profile.

Whether that residual risk is acceptable is an **operator decision**, not one this feature makes
unilaterally. The security posture of this deployment is already operator-owned end to end
(`tools_policy`, `operator_minimum`, taint mode itself), and hard-coupling Gmail/Drive to enforce
mode would also undercut the taint design's own observe-first rollout strategy. So the design is
safe by default and overridable explicitly:

- `google_integration.require_taint_enforcement` (default `true`). With the default, startup
  validates that `taint_policy.mode` is `enforce` and that the effective matrix (defaults +
  overrides + `operator_minimum`) yields at least `confirm` at the `unknown_external` tier for the
  sink classes `arbitrary_external_message`, `attacker_addressable_egress`, `sandbox_network`, and
  `sensitive_read_broadening`. If the check fails, the Google tools are not registered, a startup
  error-log entry states why, and the integration status endpoint reports the unmet condition.
- An operator who accepts the tradeoff (e.g. single-user deployment, taint rollout still in observe
  mode, willing to rely on read-only scopes + profile policy + confirmations) sets
  `require_taint_enforcement: false`. The tools then register regardless of taint mode; the choice
  is logged at startup and shown on the integration status endpoint so it is a visible, deliberate
  risk acceptance rather than a silent default.

With enforcement on, the turn's taint state does its job: after a Gmail read, the matrix gates
attacker-addressable egress and sensitive-read broadening with real confirm/deny outcomes, and
graduated tiers keep authenticated household mail low-friction. Observe mode still gets the full
audit trail and provenance, which is exactly what the observe-first rollout needs to tune the matrix
before flipping to enforce.

**What the floor does and does not guarantee.** The matrix floor covers the exfiltration and
corpus-broadening sinks: with it enforced, a prompt-injected message cannot send arbitrary external
messages, reach attacker-addressable egress, run networked sandbox code, or broaden sensitive reads
without a confirm/deny outcome. Two sink classes are deliberately **not** in the floor, because the
shipped default matrix intentionally leaves them softer at `unknown_external` (`home_local: allow`,
`artifact_write: audit`), and this feature should not re-litigate the taint design's matrix through
a side-door registration check:

- `artifact_write` — the taint design's mitigation for writes is provenance propagation, not
  confirmation: notes and other artifacts written from a tainted turn are stamped with taint labels
  (already implemented in the notes write path), so injected instructions cannot launder themselves
  into "trusted" storage — they re-enter later turns as tainted. Automations carry provenance and
  wake under their originating profile. Confirming every write after any external content is the
  rubber-stamping failure mode the taint design explicitly avoids.
- `home_local` — the taint design treats the household as inside the trust boundary: Home Assistant
  actions cannot exfiltrate mailbox content to an attacker. The residual risk is attacker-influenced
  household actuation (e.g. a hostile email inducing a device action). Operators with
  high-consequence actuators (locks, garage doors, alarms) should raise `home_local` at
  `unknown_external` to `confirm` via `taint_policy.matrix_overrides` / `operator_minimum` — the
  user guide documentation in Milestone 4 calls this out explicitly alongside the Gmail/Drive setup
  instructions.

This keeps the guarantee statement accurate: the default floor prevents un-confirmed exfiltration
and read-broadening after Google content enters a turn; in-household actuation and audited,
provenance-labeled writes follow the deployment's matrix, which the operator tunes.

#### Tool policy defaults

In `defaults.yaml`:

- All five tools carry tags `google_personal_data`, `OUTPUT_UNTRUSTED`, `READ_ONLY`, and
  `SENSITIVE_DATA`. The last two are load-bearing, not descriptive: `resolve_tool_sink_class` maps a
  descriptor to `sensitive_read_broadening` only when both `sensitive_data` and `read_only` are
  present, and that sink class is what makes a *second* Gmail/Drive read after tainted content hit
  the matrix floor's confirm outcome instead of falling through to a default.
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
  1. **Enforced runtime taint (default-on requirement)** — by default the tools do not register
     unless `taint_policy.mode` is `enforce` and the matrix floor above holds, so after a
     Gmail/Drive read the exfiltration and read-broadening sinks (arbitrary external messages,
     attacker-addressable egress, sandbox network, sensitive-read broadening) are actually gated
     (confirm/deny), not merely audited. That is the precise guarantee: injected mailbox content
     cannot exfiltrate data or steer external communication un-confirmed. In-household actuation
     (`home_local`) and provenance-labeled artifact writes remain governed by the deployment's
     matrix as discussed above — residual risk the operator tunes, not a gap this feature hides. An
     operator can also explicitly waive the whole requirement (`require_taint_enforcement: false`)
     and accept the larger observe-mode exposure — a deliberate, logged deployment decision with the
     remaining mitigations below still in place, consistent with the operator owning the security
     posture everywhere else in the config.
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
  scopes:                    # operator-tunable DATA scopes; subset of the supported
    - "https://www.googleapis.com/auth/gmail.readonly"   # read-only allowlist below
    - "https://www.googleapis.com/auth/drive.readonly"
  # Require taint_policy.mode=enforce (plus the matrix floor) before registering
  # the Gmail/Drive tools. Set false to accept running them under observe mode;
  # the waiver is logged and shown on the integration status endpoint.
  require_taint_enforcement: true
```

The identity scopes `openid` and `email` are **not** part of the operator-tunable `scopes` list: the
authorize endpoint always appends them in code, so the callback reliably receives an ID token
identifying the connected account regardless of operator configuration.

`scopes` is validated at startup against the allowlist of scopes the shipped tools can actually use
— in v1: `gmail.readonly`, `drive.readonly`, and `drive.metadata.readonly`. The field exists to
*narrow* the grant (e.g. Gmail-only, no Drive), not to broaden it: a write-capable scope such as
`gmail.send`, `gmail.modify`, or full `drive` would add power to the stored refresh token that no v1
tool can exercise — pure credential-theft blast radius for zero functionality — so an unlisted scope
disables the integration with a clear startup error. This is coherence validation, not a policy
knob; when write actions land (future work), the allowlist grows with them.

Integration is enabled only when client id, secret, and encryption key are all present, the `scopes`
list passes allowlist validation, **and** web session support is configured (`SESSION_SECRET_KEY`):
the app installs `SessionMiddleware` only when that key is present, and the OAuth flow stores its
state nonce and initiating user in the server-side session. A configured-but-sessionless deployment
disables the integration with a clear startup error rather than failing at authorize time.

## Milestones

Each lands independently with tests and passes `poe test`.

1. **Connection infrastructure** — config models, migration, repository, Fernet encryption helper,
   OAuth endpoints with a fake Google token/userinfo server in functional web tests, settings UI
   section, `GoogleCredentialResolver` with refresh + `needs_reauth` handling. Deliverable: a user
   can connect/disconnect and the row round-trips encrypted.
2. **Gmail read tools** — `gmail_search`, `gmail_get_message`, `gmail_get_attachment` against a fake
   `GoogleApiBackend`; the atomic `record_tool_result_taint` runtime extension with authenticated
   sender classification; the `require_taint_enforcement` startup check (tools unregistered +
   surfaced reason when unmet, explicit logged waiver path); policy defaults; per-user isolation
   tests (two connected users, assert each context reads only its own data; unconnected and
   no-acting-user contexts fail closed with actionable messages). Before enabling this in a
   deployment, the operator either flips `taint_policy.mode` to `enforce` or explicitly waives the
   requirement.
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

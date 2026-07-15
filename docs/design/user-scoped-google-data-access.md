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

## Design Principles

The security-critical invariant is **cross-user isolation and injection containment for sensible
scenarios**. Where a scenario is obscure (a user racing their own reconnect, mixed-trust tier
optimization), the design accepts *reasonable* behavior instead of ideal behavior and says so
explicitly, rather than growing machinery. Accepted simplifications are collected in
[Deliberate simplifications](#deliberate-simplifications-accepted-behavior).

## Goals

- Per-user "Connect Google account" / disconnect flow in the web UI, bound to the authenticated
  canonical user.
- Durable, encrypted-at-rest storage of per-user OAuth refresh tokens.
- Read-only Gmail tools (search, read message, fetch attachment) and Drive tools (search, fetch
  file) that operate strictly as the acting user of the current turn.
- Fail-closed behavior everywhere: no connection → actionable error; no acting user → tools
  unavailable; no encryption key → feature disabled.
- Taint-correct results: Gmail/Drive content enters the turn as untrusted input via the runtime
  taint machinery (`docs/design/runtime-taint-machinery.md`).

## Non-Goals

- **Write actions** (send email, create/modify Drive files, delete anything). Future work, gated
  behind confirmation and taint sink policy. v1 requests only read-only OAuth scopes, which also
  caps the blast radius of a leaked token.
- **Ambient mailbox sync / background indexing** of email or Drive into the vector store. The
  runtime taint design explicitly calls this out as a separate connector design. v1 is
  interactive-only: the user asks, the assistant searches.
- **Graduated taint tiers for Google content.** All Gmail/Drive content taints the turn at
  `unknown_external` in v1 (see [Taint](#taint)); authenticated-sender tier reduction is future
  work.
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
  taint integration, and put refresh tokens in `config.yaml` (plaintext). Native tools give us
  per-user credential resolution at the execution-context chokepoint and tool metadata tags — none
  of which MCP output carries today.
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
   backend, tainting the turn through existing tool metadata.

### 1. Connections

#### Data model

New table `user_google_connections` (Alembic migration):

| Column                    | Type              | Notes                                                          |
| ------------------------- | ----------------- | -------------------------------------------------------------- |
| `id`                      | int PK            |                                                                |
| `user_id`                 | string(255)       | canonical user id; unique together with provider               |
| `provider`                | string(64)        | `"google"` in v1                                               |
| `provider_account_email`  | string(255)       | the Google account that was connected                          |
| `scopes`                  | JSON list[str]    | granted scopes as returned by the token exchange               |
| `refresh_token_encrypted` | text              | Fernet ciphertext, never logged                                |
| `credential_generation`   | string(36)        | random UUID; rotated on credential write and on `needs_reauth` |
| `status`                  | string(32)        | `active` \| `needs_reauth`                                     |
| `created_at`/`updated_at` | datetime          |                                                                |
| `last_used_at`            | datetime nullable | updated on successful API use                                  |

Access tokens are cached **in memory only**, keyed by `(user_id, credential_generation)`, and never
persisted. `credential_generation` is a random UUID rotated on every credential write (reconnect,
account switch) and on `needs_reauth`, so a reconnect, disconnect, or revocation makes stale cache
entries unreachable immediately — a UUID rather than a counter so a deleted-and-recreated connection
can never reproduce an old key.

A single asyncio lock per user serializes token refreshes with credential mutations (reconnect,
disconnect, status changes): refresh is single-flight, a refresh and a reconnect cannot interleave,
and a refresh re-checks the generation before persisting any status change so it can never mark a
*replacement* connection `needs_reauth`.

An API request already in flight when its user reconnects or disconnects may still complete with the
old token — see [Deliberate simplifications](#deliberate-simplifications-accepted-behavior).

Repository access follows the existing pattern: `db.google_connections` on `DatabaseContext`,
returning Pydantic models.

#### Encryption at rest

Refresh tokens are encrypted with Fernet (`cryptography` is already a transitive dependency; add it
as a direct dependency). The key comes from a new `CREDENTIAL_ENCRYPTION_KEY` environment variable
(maps to `google_integration.credential_encryption_key`, redacted from logged/exported config like
`APNS_AUTH_KEY`).

- Key unset → the Google integration is disabled: connect endpoints return a clear error, tools are
  not registered. Fail closed, no plaintext fallback.
- Key present but a stored token fails to decrypt → surface an actionable **configuration** error
  ("credential decryption failed — check `CREDENTIAL_ENCRYPTION_KEY`") without mutating the
  connection row; never crash the turn pipeline. Fernet cannot distinguish a wrong deployment key
  from corrupt ciphertext, so a transient key typo or partially rolled-out secret must not durably
  invalidate otherwise-valid connections — restoring the correct key restores service with no user
  action. `needs_reauth` is reserved for authoritative signals from Google (`invalid_grant` on
  refresh), where re-consent genuinely is the only fix.
- Future key rotation via `MultiFernet` (accept-old/encrypt-new) is noted but not built in v1.

#### OAuth flow (web only)

New router `src/family_assistant/web/routers/google_integration.py`, session-authenticated (normal
web auth; explicitly **not** covered by `DIAGNOSTICS_READONLY_TOKEN`):

- `GET /api/integrations/google` — connection status for the settings UI (account email, scopes,
  status; never tokens).
- `GET /api/integrations/google/authorize` — starts the authorization-code flow. Generates a random
  nonce and persists the pending flow **in the database** (a `pending_google_oauth_flows` table:
  hashed nonce, initiating canonical `user_id`, `created_at`), then redirects to Google's consent
  screen with `state=nonce`, `access_type=offline`, `prompt=consent`, and the requested scopes: the
  configured data scopes (v1 default: `gmail.readonly`, `drive.readonly`) plus `openid` and `email`,
  which are always appended in code — not operator-configurable — so the callback can identify the
  connected account. A database store is required, not a convenience: the app's `SessionMiddleware`
  keeps the whole session in a **signed client-side cookie**, so a cookie-stored nonce cannot be
  single-use.
- `GET /api/integrations/google/callback` — **atomically consumes** the pending flow before
  exchanging the code: a single conditional `DELETE` on the hashed state value claims the flow row,
  exactly one callback can win it, and every subsequent callback presenting the same state is
  rejected. The handler additionally requires the session's canonical user to match the flow's
  initiating user. Without single-use consumption, `state` is only echoed by the authorization
  server, so an attacker who learned an old value could mint a fresh code for *their* Google account
  and hand the victim a callback URL that overwrites the victim's connection with an
  attacker-controlled mailbox. After claiming the flow, the handler exchanges the code (server-side,
  client secret from `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`), fetches the account
  email from the ID token / userinfo, and upserts the connection row for the flow's canonical user.
  The Google account email is recorded for display but plays no role in authorization — the binding
  is initiating user → connection. Pending flows expire after a short TTL (10 minutes), enforced on
  claim and cleaned up opportunistically.
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
  `read_text_attachment` tool. No taint changes are needed for read-back: `read_text_attachment` is
  already tagged `OUTPUT_UNTRUSTED`, so re-read content taints the turn `unknown_external` — the
  same tier the original fetch did.
- Result size is bounded (default page sizes, body truncation with an explicit truncation marker) so
  a hostile mailbox cannot blow out the context window.
- **Attachment owner enforcement.** The existing attachment registry performs no user-scoped
  authorization, so without a change any user (or any turn acting as another user) holding an
  attachment ID could read Google-derived content through `read_text_attachment` or the HTTP
  attachment routes — a hole in the cross-user isolation this design promises. The registry
  therefore gains an optional `owner_user_id` on registered attachments. The Gmail/Drive tools (and
  their auto-conversion of large results) always set it to the acting user; enforcement lives in the
  registry itself, not in individual callers, and covers **every operation on an owned attachment**
  — content read, the metadata route (`GET /api/attachments/{id}/metadata`, which today calls
  `get_attachment` without resolving the session user), delete, and any future mutation. An
  operation with a non-matching acting user (tool path, via `exec_context.user_id`) or session user
  (HTTP routes) is refused as not-found. Owned attachments are also served with
  `Cache-Control: private, no-store` instead of the route's current
  `public, max-age=31536000, immutable` — otherwise a shared cache could hand user A's file to user
  B without ever reaching the ownership check. Attachments without `owner_user_id` (uploads, legacy,
  non-personal tool output) behave exactly as today, so this is additive and backward-compatible. To
  make enforcement unavoidable rather than per-consumer, the registry read/delete APIs become
  **actor-bound**: `acting_user_id: str | None` is a required (non-defaulted) parameter on
  `get_attachment` / content / delete. Passing `None` is valid and means "no user context" — it can
  read only ownerless attachments; on an owned attachment it fails. This is deliberately a breaking
  signature change: the type checker enumerates every existing consumer (`jq_query`,
  `execute_script`, `attach_to_response`, notes flows, chat delivery, HTTP routes, …), and each one
  must state where its actor comes from — `exec_context.user_id` for tool paths, the session user
  for HTTP routes, and a new `on_behalf_of_user_id` threaded through the attachment-delivery APIs
  (`ChatInterface` send paths read attachments by ID alone today, with neither an execution context
  nor an HTTP session; without the parameter, strict enforcement would break sending a Gmail
  attachment back to its own requester, while a blanket exemption would reopen the bypass). Per the
  no-backwards-compatibility rule, there is no actorless shim.

#### Taint

All five tools carry the tags `google_personal_data`, `OUTPUT_UNTRUSTED`, `READ_ONLY`, and
`SENSITIVE_DATA`. The existing runtime already does everything v1 needs with these tags alone:

- `OUTPUT_UNTRUSTED` makes `_record_result_taint` record an `unknown_external` taint source for
  every result — every Gmail/Drive read taints the turn at the least-trusted tier.
- `READ_ONLY` + `SENSITIVE_DATA` together make `resolve_tool_sink_class` classify these tools as
  `sensitive_read_broadening`, so a *second* mailbox/Drive read after untrusted content has entered
  the turn is gated by the taint matrix.

**v1 adds no new taint runtime machinery.** Uniform `unknown_external` is deliberately conservative
— mail from household members taints the turn the same as mail from strangers — and the practical
friction is small: at `unknown_external` the default matrix still freely allows replying to the user
(`user_local`) and audits artifact writes; confirmation appears only on external messaging,
attacker-addressable egress, sandbox network, and read broadening, which is where it belongs.
Graduated tiers for authenticated household senders (classifying Gmail's `Authentication-Results`
DMARC evidence against the email-intake allowlists, plus a per-result provenance runtime extension
so a classified tier can override the static tag) are future work; a detailed sketch was worked out
during design review and can be recovered from this document's git history if the friction ever
warrants it.

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
  validates that `taint_policy.mode` is `enforce` and that the **fully merged effective policy —
  queried through the same evaluator the runtime uses**, i.e. defaults after applying
  `taint_policy.matrix`, `matrix_overrides`, `operator_minimum`, and any profile-level policy for
  the profiles the tools are enabled in — yields at least `confirm` at the `unknown_external` tier
  for the sink classes `arbitrary_external_message`, `attacker_addressable_egress`,
  `sandbox_network`, and `sensitive_read_broadening`. Validating a re-derived approximation instead
  of the real evaluator would let a full-`matrix` replacement (which the runtime honors) silently
  drop a gate the floor claims to guarantee. If the check fails, the Google tools are not
  registered, a startup error-log entry states why, and the integration status endpoint reports the
  unmet condition.
- An operator who accepts the tradeoff (e.g. single-user deployment, taint rollout still in observe
  mode, willing to rely on read-only scopes + profile policy + confirmations) sets
  `require_taint_enforcement: false`. The tools then register regardless of taint mode; the choice
  is logged at startup and shown on the integration status endpoint so it is a visible, deliberate
  risk acceptance rather than a silent default.

**What the floor does and does not guarantee.** With enforcement on, a prompt-injected message read
from Gmail or Drive cannot drive arbitrary external messages, attacker-addressable egress, networked
sandbox code, or further sensitive reads without a confirm/deny outcome. Two sink classes are
deliberately **not** in the floor, because the shipped default matrix intentionally leaves them
softer at `unknown_external` (`home_local: allow`, `artifact_write: audit`), and this feature should
not re-litigate the taint design's matrix through a side-door registration check:

- `artifact_write` — the taint design's mitigation for writes is provenance propagation, not
  confirmation: notes and other artifacts written from a tainted turn are stamped with taint labels
  (already implemented in the notes write path), so injected instructions cannot launder themselves
  into "trusted" storage — they re-enter later turns as tainted. Automations carry provenance and
  wake under their originating profile. Confirming every write after any external content is the
  rubber-stamping failure mode the taint design explicitly avoids. Provenance stamping does *not*
  help with **destructive** mutations (`delete_note`, `delete_calendar_event`, …), which resolve to
  the same sink class and would run un-confirmed at `unknown_external`; this exposure is identical
  to what email intake already accepts under the shared matrix, and the same operator override
  applies (`artifact_write` → `confirm` via `matrix_overrides`, documented alongside the
  `home_local` callout). Splitting `artifact_write` into creation vs. destructive-mutation sink
  classes so the matrix can treat them differently is proposed as future work on the taint
  machinery, not smuggled in here.
- `home_local` — the taint design treats the household as inside the trust boundary: Home Assistant
  actions cannot exfiltrate mailbox content to an attacker. The residual risk is attacker-influenced
  household actuation (e.g. a hostile email inducing a device action). Operators with
  high-consequence actuators (locks, garage doors, alarms) should raise `home_local` at
  `unknown_external` to `confirm` via `taint_policy.matrix_overrides` / `operator_minimum` — the
  user guide documentation shipped with Milestone 2 calls this out explicitly alongside the Gmail
  setup instructions.

#### Tool policy defaults

In `defaults.yaml`:

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
     unless `taint_policy.mode` is `enforce` and the matrix floor above holds, so after any
     Gmail/Drive read the exfiltration and read-broadening sinks are actually gated (confirm/deny),
     not merely audited. In-household actuation (`home_local`) and provenance-labeled artifact
     writes remain governed by the deployment's matrix as discussed above — residual risk the
     operator tunes, not a gap this feature hides. An operator can also explicitly waive the whole
     requirement (`require_taint_enforcement: false`) and accept the larger observe-mode exposure —
     a deliberate, logged deployment decision with the remaining mitigations below still in place,
     consistent with the operator owning the security posture everywhere else in the config.
  2. **Read-only scopes** — the OAuth grant itself cannot send mail or write files, so the Google
     tools add no egress capability of their own.
  3. **Profile policy** — ambient profiles that already process untrusted triggers cannot also read
     the mailbox.
- **Cross-user isolation** is structural: connection rows are keyed by canonical `user_id`; the
  resolver reads the acting user from the execution context only; tool schemas contain no
  user-addressable parameter. A prompt-injected model cannot ask for someone else's mailbox because
  no reachable code path takes a user identifier as input. Google-derived attachments carry
  `owner_user_id` and the registry enforces it on every operation.
- **Token safety**: refresh tokens Fernet-encrypted at rest; access tokens in memory only; both
  redacted from config dumps and diagnostics export; tool results and errors never include tokens.
  The new endpoints are excluded from the diagnostics read-only token's scope.
- **CSRF/linking safety**: OAuth `state` is a single-use nonce persisted in
  `pending_google_oauth_flows` and atomically claimed at callback time, and the callback requires
  the session's canonical user to match the flow's initiating user — a crafted or replayed callback
  URL cannot attach an attacker's Google account to a victim's connection.

## Deliberate simplifications (accepted behavior)

Simplifications adopted on the principle that sensible scenarios get good behavior and obscure
scenarios get *reasonable* behavior — recorded here so future reviewers know they are deliberate,
not oversights:

- **Self-race on reconnect/disconnect.** An API request already in flight when its user reconnects a
  different Google account (or disconnects) may complete and return data fetched with the old token.
  The only party who can observe the result is the same user who owned the old account and initiated
  both actions — there is no cross-user exposure, and the window is one HTTP request. We therefore
  do not add response leases, post-response revalidation, or mutation-waits-for-drain
  synchronization; the generation-keyed cache and the per-user mutation lock are the whole
  mechanism.
- **Uniform `unknown_external` taint.** Household mail is tainted like stranger mail in v1. Cost:
  occasional extra confirmation on egress after reading family email. Benefit: zero new taint
  runtime machinery (no per-result provenance path, no authenticated-sender classification, no
  aggregate tier folding, no attachment provenance round-trip). Graduated tiers are future work.
- **Same-user stale reads are not "leaks."** Several smaller behaviors follow the same rule: a
  needs_reauth flip mid-request fails the *next* request rather than the in-flight one, and a
  disconnect does not chase down responses already on the wire.

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

Tool registration follows the configured scopes, so the LLM is never advertised a tool its
credentials cannot serve: the `gmail_*` tools register only when `gmail.readonly` is configured;
`drive_search` registers when `drive.readonly` or `drive.metadata.readonly` is configured;
`drive_get_file` (content download/export) registers only with `drive.readonly`. A Gmail-only
deployment therefore exposes exactly the three Gmail tools, and a metadata-only Drive scope yields
search without fetch.

Integration is enabled only when all of the following hold, validated at startup with a clear error
naming the unmet condition:

- client id, secret, and encryption key are present;
- the `scopes` list passes allowlist validation;
- **real web authentication is enabled and resolving canonical identities** (OIDC configured and
  active with `SESSION_SECRET_KEY` set, `users` block resolution in effect). The app's
  unauthenticated development mode serves a synthetic `test_user` from `get_current_user()` without
  authenticating the request; in that mode any caller could connect or read a Google account under
  the shared identity, so the integration must refuse to enable.

## Milestones

Each lands independently with tests and passes `poe test`. Per the repository rule that user-visible
features ship with their documentation, every milestone that exposes a surface includes its
USER_GUIDE and prompt updates in the same PR — there is no trailing docs milestone.

1. **Connection infrastructure** — config models, migration, repository, Fernet encryption helper,
   OAuth endpoints with a fake Google token/userinfo server in functional web tests, settings UI
   section, `GoogleCredentialResolver` with refresh + `needs_reauth` handling. Docs in the same PR:
   USER_GUIDE "Connect your Google account" section (flow, disconnect, test-mode 7-day token expiry)
   and AGENTS.md env var documentation (`GOOGLE_OAUTH_CLIENT_ID`/`_SECRET`,
   `CREDENTIAL_ENCRYPTION_KEY`). Deliverable: a user can connect/disconnect and the row round-trips
   encrypted.
2. **Gmail read tools** — `gmail_search`, `gmail_get_message`, `gmail_get_attachment` against a fake
   `GoogleApiBackend`; attachment `owner_user_id` registration + registry enforcement; the
   `require_taint_enforcement` startup check (tools unregistered + surfaced reason when unmet,
   explicit logged waiver path); policy defaults; per-user isolation tests (two connected users,
   assert each context reads only its own data; unconnected and no-acting-user contexts fail closed
   with actionable messages). Docs in the same PR: USER_GUIDE Gmail section (what the assistant
   can/can't do, the operator security notes incl. the `home_local` override callout) and
   `prompts.yaml` system-prompt guidance (tools act as the requesting user; suggest connecting when
   not connected). Before enabling this in a deployment, the operator either flips
   `taint_policy.mode` to `enforce` or explicitly waives the requirement.
3. **Drive read tools** — `drive_search`, `drive_get_file` incl. export/attachment-registry paths;
   same test posture. Docs in the same PR: USER_GUIDE Drive section and the matching `prompts.yaml`
   additions.

## Future Work

- Write actions (send/reply/label, Drive upload) behind `confirm` + taint sink classes
  (`arbitrary_external_message`, `artifact_write`).
- Graduated taint tiers for authenticated household senders: classify Gmail's
  `Authentication-Results` DMARC evidence against the email-intake allowlists, add a per-result
  provenance runtime path so a classified tier can override the static `OUTPUT_UNTRUSTED` tag, and
  fold aggregates at the max tier. A reviewed sketch exists in this document's git history.
- Ambient mailbox ingestion/indexing with visibility labels per owner (separate design, per the
  runtime taint doc's connector split).
- Per-user Google Calendar via the same connections.
- Cross-user sharing grants with explicit consent UX.
- Split `artifact_write` into creation vs. destructive-mutation sink classes in the taint machinery,
  so the matrix can confirm deletes after untrusted content without confirming every note write.
- Encryption key rotation via `MultiFernet`.

## Testing Strategy

- **Unit**: encryption round-trip + wrong-key behavior; resolver fail-closed matrix (no user, no
  connection, needs_reauth, disabled integration); refresh single-flight under concurrency.
- **Functional (web)**: full connect/disconnect flow against stubbed Google endpoints; state nonce
  single-use (a second callback with the same state is rejected); status endpoint redaction; auth
  required on all routes.
- **Tool tests**: fake `GoogleApiBackend` (DI, no monkeypatching); cross-user isolation for tools
  and attachments (tool read path, HTTP content/metadata/delete routes, cache-header assertion,
  interface delivery with `on_behalf_of_user_id`); every Google result taints the turn
  `unknown_external`; truncation/attachment conversion paths.
- **Policy tests**: tools visible in `default_assistant`, denied in `email_intake`/`reminder`;
  conformance with the per-profile tool inventory endpoint.

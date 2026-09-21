# Agentic Credential Autofill via Keychute

## Status

Proposed — third revision.

- Supersedes the draft browser credential broker design
  ([PR #833](https://github.com/werdnum/family-assistant/pull/833), never merged).
- Revised against [authenticated-site-capabilities.md](authenticated-site-capabilities.md) (PR
  #1136) and the design synthesis posted on
  [PR #1069](https://github.com/werdnum/family-assistant/pull/1069#issuecomment-5753973619). The
  previous revision of this document specified a login-only broker profile driving a terminal login
  session with a post-fill lockdown; that architecture is withdrawn here — see "What #1136 changed".

What carries over unchanged from the previous revision, and from
[Keychute's design](https://github.com/werdnum/keychute/blob/main/docs/DESIGN.md) (§2 CUJ 3, §8):
**browser-server is the Keychute `trusted-client` and receives the credential bytes directly; Family
Assistant requests the operation and receives status and metadata, never plaintext.** Keychute
already provides approval, standing grants, idempotent request/wait, origin constraints, single-use
grant reads, non-secret grant metadata, and audit — none of it is recreated in FA.

## Problem

The product capability is: **operate a configured account, using its stored credential when
necessary, without showing that credential to the model.** Bounded authenticated-site capabilities
(#1136) deliver the "operate" part from a human-provisioned saved login. This design adds the
credential part: when a task on a configured site hits a login form — expired session, password
re-prompt, a site whose sessions never persist — the agent can have the operator's stored password
filled into that form, and then continue the task, with the value never entering LLM context, tool
results, message history, or logs.

## Three risks, separated

The synthesis separates three risks the earlier revisions of this document partly conflated, and the
separation sets the scope:

1. **Giving a site its own password is the intended operation** — not a risk to design against.
2. **The password reaching the model transcript, tool results, or logs is a credential leak**, and
   is prevented mechanically. This property is why this design exists, and it is enforced by
   Keychute's delivery model plus browser-server's read-back protections below.
3. **The authenticated agent doing something unwanted *inside* the already-authorized account is the
   bounded-damage risk #1136 already accepts.** Enabling a site authorizes useful, mutation-capable
   operation of that account within the configured origins, including ordinary model mistakes and
   possible page influence. The hard boundary is where the authority can go, not a proof that the
   model cannot make an unwanted same-site change.

## What #1136 changed

The previous revision built a login-only principal: a dedicated broker profile whose authority ended
at login, a terminal session destroyed after jar save, and a lock that removed every model command
once the fill ran. Each piece followed logically from the premise — once an actor is authorized
*only* to log in, a post-login lock, deterministic finalization, login-vs-signup classification, and
fresh-context transfer all become necessary — and each piece then generated its own review churn
(pending-approval retry holes, two-step-form contradictions, SPA-navigation edge cases,
latch-clearing gaps).

Under #1136's bounded-damage contract the premise itself is wrong. The very next useful operation
after login is operating the account, and the operator authorized exactly that when they enabled the
site. Risk 3 above does not justify a special principal plus a state machine proving that principal
cannot observe an account it was never going to operate. The login-only authority split was
architecture-generated complexity, and it is removed: **autofill is a primitive available on an
already-bound authenticated-site session, and the same agent continues the task afterwards.**

## V1: `browser_autofill` on a bound session

### Binding at session creation

An `authenticated_sites` entry (#1136 configuration model) may bind a Keychute credential:

```yaml
authenticated_sites:
  hellofresh:
    jar_id: "jar_0123456789abcdef0123456789abcdef"
    authenticated_origins:
      - "https://www.hellofresh.com.au"
    credential:
      secret_name: "hellofresh-login"
      alias: "hellofresh"        # the model-visible name; defaults to the site_id
```

When `run_authenticated_site_task` creates the browser-server session, it passes the credential
binding (service-auth only, like `jar_id`). The binding is fixed for the session's lifetime, one
credential per session, and it makes the session **credential-enabled**: the read-back protections
below apply from creation, and `browser_autofill` becomes available in it. Sessions without a
binding are unchanged, and the tool refuses in them.

Credential identifiers need **authorization, not secrecy**. The alias and site id are ordinary
model-visible data; what the model cannot do is choose a different credential, browse the Keychute
store, or fill outside the session's granted origins — the binding and browser-server enforce that,
not identifier hiding. This reuses the existing configured site/profile boundary instead of adding a
broker profile whose main purpose was hiding a secret name from the model.

### The tool

```text
browser_autofill(credential_alias, field_refs?) ->
    filled | approval_pending | refused(reason)
```

A browser-server primitive (`POST /v1/sessions/{id}/autofill`, service-token auth) surfaced to the
authenticated browser profiles as an FA tool. It **fills — it is not a login engine**. The agent
navigates to the login form itself, requests the fill, clicks Sign in itself, inspects the
(redacted) result, handles the next page, and continues the task:

- `filled`: the eligible field(s) on the checked page were filled. Metadata reports which field
  kinds (username, password), never values.
- `approval_pending`: Keychute requires a human decision (no standing grant yet). The session is
  untouched; the agent retries the same call after approval, or reports the pending approval to the
  user. Keychute requests are idempotent, grants are durable, and browser-server long-polls the wait
  endpoint within the tool-call budget before returning this status.
- `refused(reason)`: wrong origin, no eligible field, target invalidated by navigation, policy
  denial, alias not bound to this session, or a clear bad-password outcome previously recorded in
  this session. Structured reason, no page content.

Multi-page (username-first) logins happen in steps: one call fills the identifier field, the agent
clicks Continue, a second call fills the password. The account email is usually legitimate
model-visible data already; it does not need password-grade handling merely because it is stored in
the same structured `{username, password}` secret, and nothing forces the sequence into one atomic
deterministic operation. Both fields still come from Keychute so the flow works when the username
*isn't* otherwise known; a fill that only placed a username reports exactly that.

### Keychute flow

browser-server registers as a Keychute client (`max_tier: trusted-client`, `mechanisms: [autofill]`)
— unchanged from the previous revision, and requiring no Keychute server changes. On the first
`browser_autofill` call it creates the access request (page origins = the session's configured
authenticated origins; context passed through from FA: site, objective snippet, acting user), waits,
reads the single-use grant, fills, and drops the plaintext. Standing grants give the steady state
(auto-approve or notify-only per Keychute policy); the first release per site gets Keychute's
approval page, where the operator sees the server-parsed grant. Every release is audited with the
`secret_version_id` actually decrypted.

## Load-bearing boundaries

These are the properties that are *not* simplified away, each enforced in browser-server — the
component that owns the page:

1. **Check the real destination.** The model saying `origin=hellofresh.com` is not evidence. At fill
   time browser-server verifies the actual target document's origin against the constraints on the
   **granted** capability — Keychute exposes grant metadata precisely because an approval may narrow
   the requested constraints, so the granted origins are enforced, not the requested ones, including
   after an `approval_pending` wait. Main-frame-only autofill is the V1 limitation: no filling into
   iframes.
2. **Bind the fill to the page and element that were checked.** Resolve the target, validate it, and
   fill as one serialized operation; if a navigation or document replacement invalidates the checked
   target — the site can navigate itself while an approval is pending; the session mutex only
   serializes API commands — the fill fails with `refused(target_invalidated)` rather than filling
   whatever is there now.
3. **Element sanity checks, honestly labelled.** The password goes only into `input[type=password]`;
   an explicit `autocomplete=new-password` or confirm-password field is rejected; the identifier
   goes into a text/email/tel input, `autocomplete=username` preferred. These are cheap, worthwhile
   checks — they are **not** proof that the approved site cannot read or echo its own field value,
   and ambiguity in web-form heuristics is a `refused`, not a reason to build a universal
   login-classification subsystem.
4. **Site and tool containment, unchanged from #1136.** Autofill grants no jar selection, no
   Keychute browsing, no other credential, no unrelated household tools, and no cross-origin
   browsing. The session's origin confinement, profile tool policy, pinned delegation, and
   no-recursive-acquisition boundaries all still hold; `browser_autofill` is admissible in the
   mechanical tool validation because it is browser-server-mediated.
5. **Bounded retries.** A single-use Keychute read authorizes one fill, but one read is not
   automatically one login *submission*, and Keychute's pending-request caps are not a site's
   account-lockout policy. So: one fill per grant read; on a clear bad-password outcome (the site
   says the credentials are wrong) the session records it, further `browser_autofill` calls in the
   session are `refused`, and the run returns `needs_human`. No automatic re-request loop. If
   unattended retries become a real workflow later, a simple persisted needs-attention latch is the
   upgrade — it is not V1 machinery.
6. **MFA and unexpected challenges go to the human.** The existing handoff flow is the fallback;
   `browser_autofill` never collects codes. Broad IdP credentials and autonomous MFA are deferred
   for blast-radius and complexity reasons — deferred, not declared permanently impossible.

## Read-back protection

If the same session continues after autofill, the real prerequisite is that the model cannot read
the secret back out of the page. Current browser-server does not yet provide this: the snapshot
walker copies non-empty `el.value` into model-facing snapshots, the production runtime exposes
screenshot, raw extract, and arbitrary `exec`, and `exec` is default-denied only when
`session.jar_id` is set. A **credential-enabled session** therefore enforces, from creation — so an
agent cannot install a page listener before the password is filled:

- form control **values are redacted from snapshots**;
- **screenshots mask form controls** (including after a "show password" toggle) or are denied;
- **no `exec`, raw-DOM extract, or equivalent escape hatches** that can read field values,
  regardless of whether a jar is loaded;
- the secret appears in **no tool arguments, results, events, exception text, traces, or logs** —
  the discipline the jar store already applies to cookie values, extended to the fill path.

These are modest, testable claims, and they compose with the global snapshot-redaction milestone
rather than replacing it. The accepted residual stands and is stated plainly (as Keychute DESIGN §3
does): once filled, the approved origin's own JavaScript can read the field — the same exposure as
any password manager's autofill. The controls are which sites the operator wires up and the
destination checks above; a hostile approved origin is out of scope.

## Relationship to jars

**Jars are valuable persistence, not a prerequisite for login.** A credential-enabled session is an
ordinary #1136 site-capability session — usually created from the configured jar, and usable even
when that jar is stale (the agent logs in and continues) or, for a site whose sessions never
persist, potentially without meaningful jar state at all.

Automatic jar refresh after a successful in-session login is a **deferred follow-up**, added only if
measured expiry friction justifies it. When it is added, the correctness rules from the previous
revision remain the important part and are retained by reference: a refresh binds to the exact
configured `jar_id` (never label matching, never a new unlinked jar from a loaded session),
preserves the stored scope and probe rather than re-deriving them, and saves only state that passes
the stored freshness probe. Existing exclusive-human-handoff protections are untouched.

## What is cut or deferred

Cut (withdrawn from the previous revision, per the synthesis):

- the login-only `browser_login_broker` profile;
- the terminal login session and mandatory export/probe/destruction/fresh-context reload after every
  fill;
- the post-fill lock removing model commands;
- deterministic orchestration of every two-step login inside one atomic fill;
- the generic login-vs-signup/reset classification subsystem (the cheap explicit-attribute checks in
  boundary 3 stay);
- the refresh-only constraint and the pre-existing-jar requirement;
- credential-identifier secrecy (replaced by authorization via the site binding);
- the V1 needs-attention latch (replaced by in-session stop-on-bad-password; the latch returns only
  if unattended retry workflows become real).

Deferred, in response to observed need rather than up front: automatic jar refresh (above),
TOTP-seeds-in-Keychute, IdP/SSO credentials, autonomous MFA.

Separate workstream, explicitly **not** a dependency or scope expansion here: magic-link login
(several target sites support it; it sidesteps password autofill entirely, the link is still
credential-bearing, and the mailbox interaction belongs to the taint machinery's
`sensitive_read_broadening` vocabulary in [runtime-taint-machinery.md](runtime-taint-machinery.md)).

## Build order

1. **browser-server:** the Keychute client (request/wait/read against the in-cluster URL, internal
   CA); credential binding on `create_session`; the `autofill` endpoint with granted-constraint
   destination checks, serialized target binding, element sanity checks, and one-fill-per-read;
   credential-enabled read-back protection from creation. Security regression tests: plaintext in no
   response/event/log/exception; fill refused off-origin, in iframes, on new-password fields, after
   target invalidation, and in non-credential sessions; snapshot/screenshot redaction and `exec`
   denial active before the first fill.
2. **family-assistant:** `credential` on `authenticated_sites` entries; the `browser_autofill` tool
   in the authenticated browser profiles (admitted by the mechanical browser-server-mediated
   validation); `approval_pending`/`needs_human` surfacing through the #1136 result contract;
   startup validation that a credential binding names a configured site the acting user is
   authorized for; user and operator docs.
3. **Keychute / kube-config:** register the `browser-server` client (token + values) — no server
   changes.
4. **Prove it:** a real password-login workflow end to end on a configured site, then add jar
   repair, wider login coverage, or other authentication mechanisms only in response to observed
   failures.

## Open questions

1. **Secret format** — unchanged: one structured `{"username": …, "password": …}` Keychute secret
   per site account, validated by the fill path.
2. **Notify-only cadence** for standing autofill grants (Keychute DESIGN open question 2): every
   release, until volume says otherwise.
3. **Where the bad-password signal comes from** — the agent observing a rejection message is
   model-judged, not mechanical. V1 records the agent's own report plus the deterministic signal of
   a repeated eligible login form after a submission; is that enough to gate `refused`, or should V1
   simply cap fills per session at a small constant? Leaning: both, with the cap as the backstop.

# Agentic Credential Autofill via Keychute

## Status

Proposed. Supersedes the draft browser credential broker design
([PR #833](https://github.com/werdnum/family-assistant/pull/833),
`docs/design/browser-credential-broker.md`, never merged).

That draft was written before two systems existed that now carry most of its weight:

- **[Keychute](https://github.com/werdnum/keychute)** — the cluster's secrets delivery broker. Its
  tier-1 `autofill` mechanism (origin-constrained, single-use, human-approved or standing-grant
  release of a credential to deterministic client code) is implemented server-side with e2e
  coverage. It replaces PR #833's entire hypothetical "Agent Access" subsystem.
- **[browser-server](https://github.com/werdnum/browser-server)** cookie jars
  (`cookie-jar-design.md` there, [browser-cookie-jars.md](browser-cookie-jars.md) here) — the
  encrypted, scope-filtered, probe-verified capture of post-login browser state, loaded only into
  fresh confined sessions. A jar *is* the `authenticated_browser(origin)` capability PR #833
  invented, with stronger enforcement than that draft specified.

This document re-evaluates PR #833 against that landscape: what survives, what collapses, and what
remains to build. Per [project-assessment-2026-07](project-assessment-2026-07.md) milestone 4, the
credential broker is the *last* stage of the authenticated-browsing roadmap; this design makes it a
small one instead of the largest.

## Problem, restated

Cookie jars plus the warm-session handoff already deliver authenticated browsing: a human logs in
once (typing the password into the real page over noVNC — the value never approaches the model),
saves the jar, and the agent browses that site logged-in from then on. What is still missing is
**re-login without a human at the keyboard**: jars expire, and today every expiry costs a human a
handoff dance. For households that store site passwords in Keychute anyway, the assistant should be
able to repair an expired login on its own — without the password ever entering LLM context, tool
results, message history, or logs.

That narrower goal — *agent-driven login as a jar refresh path*, not a new kind of session — is the
key reframing, and it is what makes most of PR #833's machinery unnecessary.

## What changed since PR #833

PR #833's central artifact was a five-state browser-session state machine (`public_browser`,
`credential_in_field`, `user_secret_in_field`, `authenticated_browser`, `discarded`) with
non-transferable states, an 11-step finalization tool, and per-state tool denial. All of that
existed to answer one question: *how do you safely hand a live browser context that once held a
password over to a general-purpose profile?*

The jar design already answered it differently, and better: **you don't**. Nothing live ever crosses
the boundary. The login happens in a terminal session; the only artifact that survives is a
scope-filtered, encrypted, value-opaque jar; and the task profile gets a *fresh* browser context
seeded from that jar, behind the existing `load_saved_session` confirmation. The password-holding
DOM is unreachable from the task session by construction — there is no page to close, no leftover
populated field to check for, no capability object to mint.

Concretely, PR #833's components map to:

| PR #833 component                                                                                                                        | Now                                                                                                                                                                                                                                                                                     |
| ---------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent Access status/gating/pairing (`agent_access_status`)                                                                               | Keychute: policy engine, approval UI, standing grants, audit — implemented. Discovery of "which credential for this origin" is an FA config mapping (below).                                                                                                                            |
| `browser_fill_credential` + human approval                                                                                               | Keychute `autofill` release (tier 1, page-origin-constrained, max_uses=1) into browser-server's deterministic fill (below).                                                                                                                                                             |
| `credential_in_field` non-transferable state                                                                                             | Structural: the login session is never transferable, never jar-loaded, and is closed unconditionally. One residual per-session flag in browser-server (below).                                                                                                                          |
| `user_secret_in_field` + `browser_fill_user_supplied_secret` (5 purpose kinds, trusted-UI collection, push-approval waits, backup codes) | Dropped from V1. MFA falls back to the existing human login flow. TOTP-in-Keychute is the eventual replacement (below).                                                                                                                                                                 |
| `browser_finalize_authenticated_session` (11 checks, capability object)                                                                  | `save-jar` with its **mandatory freshness probe**: the probe's authenticated-only indicator is the "positive authentication evidence" check, and the jar id is the capability — already a first-class, policy-visible object.                                                           |
| Authenticated-browser policy (origin scoping, exec deny, redaction, cross-origin nav)                                                    | Shipped as jar mechanism: exact-origin navigation confinement (pre-request, incl. redirects, popups, service-worker bypass), `exec` default-deny, scope filtering that drops IdP cookies, revocation kill-switch. FA-side gating per [browser-cookie-jars.md](browser-cookie-jars.md).  |
| Password re-prompt → transition back to `credential_in_field`                                                                            | Never fill into a task session. A mid-task password re-prompt means: invalidate the jar, run the login flow fresh, reload. Session state (cart etc.) survives server-side via the refreshed cookies; where it doesn't, that is accepted reasonable-not-ideal behaviour for a rare case. |
| Snapshot redaction while a secret is live                                                                                                | Login sessions always redact all form values in snapshots (simpler than state-conditional rules), layered under the global snapshot-redaction milestone that precedes this work anyway.                                                                                                 |

What survives from PR #833 unchanged: the core invariant (the secret never transits the model), the
isolation of login-driving into a dedicated restricted profile, exact-origin pinning of the fill,
human approval before first release, and discard-on-uncertainty.

## Architecture

Three parties, each doing the thing it already does:

```
family-assistant                browser-server                     Keychute
  (policy + LLM login             (deterministic fill,               (storage, approval,
   navigation, no plaintext)       page ownership, jars)              release, audit)

  login_to_site(origin) ───────►  create login session
                                  POST /v1/access-requests ────────►  policy: standing grant?
                                                                       ├─ auto/notify → grant
                                                                       └─ pending → Pushover → human
  broker profile drives page      wait for grant  ◄─────────────────  grant_id
  (redacted snapshots,
   click/wait only)
  request_credential_fill ─────►  read grant → plaintext ◄──────────  single-use read
                                  verify page origin ∈ grant origins
                                  verify element is a credential input
                                  Playwright fill + submit
                                  (plaintext dropped; never logged,
                                   never in any response)
  finalize ────────────────────►  save-jar (probe must prove
                                  logged-in) → close login session
  load_saved_session ──────────►  fresh confined context from jar
  (existing confirmation gate)
```

### browser-server is the Keychute client, not family-assistant

The decision PR #833 never had to make: with browsing remote (`RemoteBrowserBackend`,
[browser-server-integration.md](browser-server-integration.md)), the fill happens in a different
process from the one hosting the LLM. Two options:

1. FA reads the plaintext from Keychute and forwards it inside a fill command.
2. browser-server registers as its own Keychute client (`max_tier: trusted-client`,
   `mechanisms: [autofill]`) and redeems the grant itself; FA never holds plaintext.

**Option 2.** The component that owns the page — and therefore is the only one that can enforce the
fill-time checks deterministically (current page origin against the grant's origins, target element
type, value-free logging) — should be the one that receives the bytes. It also keeps the plaintext
out of the FA process entirely, which hosts the model loop, verbose logging, and message history;
the tier-1 "trusted client" registration then vouches for browser-server's small fill path rather
than for all of family-assistant (Keychute's "mechanism honesty" principle, DESIGN §6). Keychute
grants are client-bound and non-transferable, so this composes with no Keychute changes: the party
that redeems the grant is the party that requested it.

FA still originates the intent and the context: `login_to_site` passes the origin, the credential
name, and request context (reason, conversation snippet) to browser-server, which forwards them as
the Keychute request's client-asserted context. Approval UX, waiting, and expiry are Keychute's
existing machinery; browser-server long-polls the wait endpoint and surfaces `pending approval` to
FA rather than blocking a tool call across minutes (Keychute requests are idempotent and grants are
durable, so retrying is safe).

### Credential discovery

PR #833's `agent_access_status(origin)` becomes a static FA config mapping:

```yaml
credential_autofill:
  sites:
    - origin: "https://www.woolworths.com.au"
      secret_name: "woolworths-login"
      login_path: "/login"        # optional starting point
```

The household site list is operator-curated anyway (the same list that motivates jars), Keychute
fails closed regardless (no policy row and no approval → no release), and the mapping keeps
arbitrary secret names out of the model's gift: the broker profile can only ask for logins the
operator wired up. A Keychute "what can I request for origin X" metadata endpoint remains a possible
later convenience, not a V1 dependency.

The secret's value format is `username\npassword` (or a JSON object — decided at implementation with
the browser-server fill API), stored once in Keychute per site. Both fields come from the same
release; there is no separate username fetch.

### The fill, hardened

`POST /v1/sessions/{id}/fill-credential` (login sessions only, service-token auth) with the grant id
and optional element ref hints. Deterministic checks, all fail-closed, all in browser-server:

- The session must be a **login session** (created with `login_mode: true`, carrying the target
  origin and an optional configured same-site auth-origin allowlist). Login sessions are
  navigation-confined like jar-loaded sessions, cannot be created from a jar, and cannot save a jar
  for any origin outside their declared target.
- Current page origin must be within the login session's declared origins **at fill time** — not
  just at grant time.
- The password is filled only into an element that resolves to `input[type=password]`; the username
  only into a text/email input (heuristics like `autocomplete=username` preferred, model ref hints
  validated, never trusted). PR #833 accepted a free `password_ref`; a prompt-injected login page
  could point it at a field the page echoes. Element-type validation closes that.
- From fill until either a post-submit navigation commits or the session is discarded, the session
  carries a **`secret_live` flag**: snapshot, screenshot, `exec`, `extract`, and handoff minting are
  denied. This is the one surviving fragment of PR #833's state machine — a boolean on one session
  type, enforced where the page lives.
- Snapshots in login sessions are **always** form-value-redacted, before and after the flag window.
- The plaintext exists only in the fill call path: never in a response, event, log line, or the
  session record. Same discipline the jar store already practices for cookie values.
- Grant reads are single-use (`max_uses: 1`, Keychute v1 enforces this for releasing tiers), so one
  approval is one fill attempt. A retry is a new access request — visible in Keychute's audit and
  throttled by its pending-request caps.

Accepted residual, stated plainly (as PR #833 and Keychute DESIGN §3 both do): once filled, the
page's own JavaScript can read the field. This is the same exposure as any password manager's
autofill; the controls are strict origin matching and human approval of which sites get wired up at
all. A hostile *approved* origin is out of scope.

### Finalization is jar save

Success criterion: the **mandatory jar probe** proves logged-in (authenticated-only selector present
/ no redirect to the login wall) in a fresh throwaway context seeded from the candidate state. That
is strictly stronger than PR #833's in-place evidence checks — it validates that the *persisted,
filtered* state suffices for a future session, which is the thing that will actually be used. On
probe success the jar is created or refreshed (refresh cannot widen scope — the existing rule); the
login session is closed unconditionally. On any failure — submit didn't navigate, challenge
appeared, probe stale/uncertain — the login session is discarded and **no jar is written**; the user
is told the site needs a human login (the existing flow).

The task profile then reaches the site exactly as it does today: `load_saved_session` into a fresh
confined context, with the confirmation and taint policy of
[browser-cookie-jars.md](browser-cookie-jars.md) unchanged. This design adds **zero** new ways for a
general profile to touch authentication.

### MFA: out of scope for V1, by design

PR #833's largest single mechanism was `browser_fill_user_supplied_secret` — five challenge kinds, a
trusted-UI collection channel, push-approval waits, backup-code UX. Dropping it is the point of this
revision:

- The human-in-the-loop MFA path already exists in strictly better form: the warm handoff. The human
  types the code into the *actual page* over noVNC — deterministic UI, value never near the model,
  zero new tools. A V1 broker that hits any challenge it cannot complete simply discards and reports
  "this site needs a human login."
- The genuinely autonomous MFA path is **TOTP seeds stored in Keychute**: a future `autofill-totp`
  mechanism where Keychute (or browser-server's deterministic code, from a released seed) computes
  the six-digit code and fills it through the same hardened path. That is a small extension of the
  existing release model — not an FA-side secret-collection subsystem — and it is where the effort
  should go if MFA re-login demand materializes. SMS/email codes and push approvals stay human.

Per the behaviour-altitude principle: MFA-at-relogin without a human present is the uncommon case,
and it gets a reasonable outcome (a clear handoff to the human path), not bespoke machinery.

### SSO / IdP logins: permanently the human path

PR #833 left "approved login redirect chains" open, and the July assessment called IdP chains the
"make-or-break" for whether the broker was worth building. The reframe dissolves that: with Keychute
and jars carrying the load, the broker is cheap enough to be worth building for
first-party-credential sites alone. Sites behind "Sign in with Google" stay on the human login flow
— the credential there *is* the IdP account, filling it agentically multiplies the blast radius, and
the jar scope filter already deliberately drops IdP cookies. The login session's navigation
confinement (target origin + optionally a configured same-site auth origin like `auth.example.com`)
enforces this: an unexpected bounce to an unconfigured origin aborts the attempt.

## The broker profile

A `browser_login_broker` processing profile survives from PR #833, but its job shrank to *page
navigation between well-defined deterministic endpoints*: dismiss the cookie banner, find and click
"Sign in", get the form on screen, request the fill, request finalization. Tool surface:

- `browser_open`, `browser_click`, `browser_wait`, `browser_snapshot` (login-session snapshots are
  redacted by mechanism), `browser_fill` (non-secret incidentals only — e.g. a "find my store"
  postcode; never receives credential material because it never has it)
- `request_credential_fill` — triggers the browser-server fill for the session's configured
  credential; takes ref *hints* only
- `finalize_login` / `abort_login` — jar save-and-close / discard

Denied by omission (not by state machinery): `browser_exec`, `browser_extract`,
`browser_screenshot`, delegation, `attach_to_response`, all data/notes/calendar/comms tools, jar
tools other than the finalize path. The profile receives only `{origin, credential_name}` from
delegation — no page content, no task description — exactly PR #833's input-minimization rule. Entry
is via the task profile calling `login_to_site` (delegation), or automatically when
`load_saved_session` finds the jar stale/invalidated and the origin has a configured credential.

Policy tests enumerate the allowed/denied surface, as PR #833 planned; the difference is that the
security-load-bearing denials (secret visibility, transfer, exfiltration) are browser-server
mechanism and Keychute policy, so an FA profile misconfiguration degrades UX, not containment.

## Security properties, checked

1. **Secret never enters LLM context, tool results, history, or logs.** Keychute releases to
   browser-server's deterministic code; FA never holds plaintext; login snapshots are redacted;
   `secret_live` blocks observation between fill and navigation; browser-server's never-log
   discipline extends to the fill path. (PR #833's property, now enforced across two services'
   mechanism rather than one profile's policy.)
2. **Origin pinning, twice.** Keychute policy constrains the release to declared page origins;
   browser-server re-checks the live page origin at fill time in the same process as the page.
3. **Fill-target integrity.** Element-type validation — tighter than PR #833.
4. **No live-DOM transfer.** Structural: login sessions are terminal; only the jar crosses; task
   sessions are fresh contexts behind the existing load confirmation.
5. **Post-login containment** is the already-designed jar mechanism (confinement, exec-deny, scope
   filter, revocation kill-switch, probes) — nothing new to get wrong.
6. **Human in the loop, calibrated.** First release per site: Keychute approval page (server-parsed
   grant shown authoritatively). Steady state: standing grant with notify-only pushes. Load into a
   task session: FA confirmation. MFA: the human, on the real page.
7. **Failure fails closed.** No fresh probe → no jar → context discarded. Lost connection mid-flow →
   grant idempotent-replay or expiry; nothing durable was created.
8. **Prompt-injected login page** can waste the attempt (mis-click, fail login) but cannot: read the
   secret (never model-visible), redirect the fill off-origin (fill-time origin check +
   confinement), capture it into an echoing field (element validation), widen the jar (scope rules
   - FA save policy), or exfiltrate page content (no extract/exec/screenshot in the profile, no
     external-comm tools).
9. **Audit end-to-end.** Keychute: request → decision → release with `secret_version_id`.
   browser-server: durable jar audit. FA: tool-call history. A compromised FA or browser-server can
   burn releases only for operator-wired origins, at notify-only visibility, under Keychute's
   per-client rate caps.

Residuals accepted and documented: page JS reading the filled field on an approved origin (password
manager equivalence); Keychute cannot verify browser-server's containment (tier-1 mechanism honesty
— the operator's registration *is* the trust statement); a compromised browser-server exposes fill
plaintext and jars (already its trust posture as jar-key holder).

## What gets built, in order

Prerequisites (already on the roadmap, unchanged): cookie jars end-to-end with human login
([browser-cookie-jars.md](browser-cookie-jars.md)); global snapshot redaction
([runtime-taint-machinery](runtime-taint-machinery.md)); Keychute M0–M3 (done).

1. **Keychute: nothing.** The autofill mechanism, origin constraints, single-use reads, standing
   grants, and notify-only outcomes exist. Register `browser-server` as a client (`kube-config`
   values + token) when milestone 3 lands.
2. **browser-server: login sessions.** `login_mode` on `create_session` (declared origins,
   confinement, no-jar-load, redacted snapshots); Keychute client (request/wait/read against the
   in-cluster URL with the internal CA); `fill-credential` with origin + element checks and the
   `secret_live` observation block; finalize = existing save-jar + close. Security regression tests:
   plaintext never in any response/event/log; fill refused off-origin, on non-password elements, and
   in non-login sessions; observation denied while `secret_live`.
3. **family-assistant: broker profile + wiring.** `credential_autofill.sites` config;
   `login_to_site` + `request_credential_fill` + `finalize_login`/`abort_login` tools on the
   `RemoteBrowserBackend`; `browser_login_broker` profile in `defaults.yaml` with policy tests;
   stale-jar → broker flow from `load_saved_session`; user docs (`docs/user/`) and prompt guidance.
4. **Later, if demand:** TOTP-in-Keychute (`autofill-totp`); a Keychute credential-discovery
   endpoint; MFA continuation via handoff mid-login-session.

## Open questions

1. **Secret format for site logins** — `username\npassword` convention vs. structured JSON vs. two
   Keychute secrets. Leaning single secret, JSON `{"username": …, "password": …}`, validated by the
   fill path. (Keychute stores opaque bytes; no server change either way.)
2. **Who triggers re-login** — only on-demand (task hits a stale jar) or also from the scheduled
   probe automation that already flags stale jars? On-demand first; scheduled repair is a policy
   knob later.
3. **`login_path` navigation** — start at the configured path vs. let the broker find "Sign in" from
   the landing page. Config-first with broker fallback seems right; measure against the real site
   list.
4. **Notify-only cadence** for standing autofill grants (Keychute DESIGN open question 2) — first
   release per grant per day, or every release? Every release, until volume says otherwise.

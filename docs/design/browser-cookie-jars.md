# Browser Cookie Jars: Family Assistant Integration

## Status

Proposed. Companion to `cookie-jar-design.md` in the
[browser-server](https://github.com/werdnum/browser-server) repository, which specifies the
mechanism (encrypted storage, save/load/probe API, audit events). This document specifies the Family
Assistant side: tools, policy gating, and taint integration.

Origin: [project-assessment-2026-07](project-assessment-2026-07.md), capability roadmap milestone 4
("persistent per-origin browser contexts, human-performed login via the existing warm-session
handoff, and session-expiry detection"), resequenced from PR #833. The credential broker remains
deferred; cookie jars capture post-login session state only, never credentials.

## Division of responsibility

browser-server enforces mechanism invariants regardless of what this repo does: jar contents are
never model-visible (metadata only, everywhere), jars are encrypted at rest and fail closed without
a key, load happens only at session creation so a session's authenticated origins are immutable and
truthfully reported, `exec` is denied in jar-loaded sessions unless explicitly enabled, and every
operation is audited.

Family Assistant owns **all policy**: which profiles see the tools, which operations require
confirmation, how authenticated sessions interact with taint, and when to probe/re-login. Adding or
tightening policy here must require zero browser-server changes — that is the acceptance test for
the mechanism design.

## Tool surface

Four new tools in the browser tool family, all thin wrappers over the `RemoteBrowserBackend` client
(they are meaningless without `browser_handoff_config.enabled`, and error cleanly when the remote
backend is not active, like `request_browser_handoff`):

- `list_saved_sessions()` → renders `GET /v1/jars` metadata: label, origins, saved-by, freshness
  (last probe result, earliest cookie expiry, invalidated flag). The service guarantees no cookie
  names/values appear in metadata, but `label` is caller-supplied and could carry planted
  instruction-like text, so the tool presents jar metadata as **untrusted data**, not as
  instructions — safe to display, not to obey. (browser-server also normalizes labels at save.)
  **Tagging, not just prose:** any tool that returns jar labels/origins — `list_saved_sessions`, and
  the ambiguity/candidate path — must carry the right tags for both halves of the taint story:
  `OUTPUT_UNTRUSTED` so a planted label raises turn taint (`derive_tool_output_taint` keys on it),
  **and `READ_ONLY` + `SENSITIVE_DATA`** so the sink resolver classifies it as
  `sensitive_read_broadening` (the resolver requires both of those; `OUTPUT_UNTRUSTED` alone would
  raise taint on output but leave the read itself ungated). An explicit sink override works too, but
  the tag triple is the idiomatic path.
- `save_browser_session(label, probe, jar_id=None, origins=None)` →
  `POST /v1/sessions/{id}/save-jar` against the conversation's active session. `probe` is
  **required** (the mechanism rejects a save without one; for an agent save the model supplies a
  selector/URL indicator it saw on the logged-in page). `jar_id=None` **creates a new jar** with
  `label`; a **`jar_id` refreshes that exact stored jar** (label stays user-facing). Refresh takes
  an explicit id rather than overloading `label` — matching a label against existing jars to decide
  create-vs-refresh is ambiguous (a duplicate label would create a second jar or rename instead of
  replacing the stale one). A **refresh defaults `origins` to the existing jar's approved origin
  set, never to just the current origin**: after an SSO re-login the human is back on the app
  origin, and defaulting to that would silently drop the previously-approved IdP/other origins and
  re-break the next load. Refresh never changes scope; widening requires a new jar. Default scope
  for a *new* jar is the current origin. **Capturing any origin beyond the session's current origin
  is an elevated ask**: the confirmation must enumerate *every* origin the save would capture **and
  every `nav_allowlist` entry it would add**, and adding an off-site origin (e.g. an IdP like
  `accounts.google.com`) — whether as a captured origin *or* an allowlist entry — is deny-by-default
  (an allowlist entry expands where the confined session may navigate, so it needs the same elevated
  consent as a captured origin, not a quiet add). A routine site-login save must not be able to slip
  an IdP or unrelated service into the jar. The `probe` target is constrained to the jar's own
  origins (rejected otherwise) and must satisfy three properties, else the save is rejected: **(1)
  safe/idempotent** — a selector check on a stable page, never an action URL like `/logout` that the
  scheduled automation would replay under the login and end the session; **(2) a stable, sanitized
  target** — query and fragment are stripped and one-time/transient URLs are rejected, so a save
  that defaults to "the current page" never persists an OAuth callback (`?code=…&state=…`),
  magic-link, or checkout-session URL (which is both sensitive and useless for later probes); **(3)
  an authenticated-only indicator** — the success signal must distinguish logged-in from logged-out
  (a logout control, an account-name element, or a redirect-to-login check via
  `logged_out_url_prefix`), not a chrome element like the logo/footer that is present on both the
  login wall and the authed page (which would report a dead jar as fresh forever). The default
  human-derived probe therefore targets a stable authenticated page with an authenticated-only
  selector, not the raw current URL. The probe target is named in the save confirmation. **This
  origin-set comparison lives in the `save_browser_session` tool wrapper, not the tag-based policy
  engine**: the tool-policy matcher keys on tool name / tags / MCP id / exact argument values and
  cannot compute "is this capture set a superset of the session's current origin," so the wrapper
  performs that check (and raises the elevated/deny confirmation) before it ever calls
  browser-server. The policy engine still supplies the coarse always-confirm rule on top.
- `load_saved_session(jar_label_or_id)` → creates the conversation's browser session with `jar_id`
  set (and `allow_exec` false). This is **the** policy chokepoint: after this call the session acts
  as the logged-in user at the jar's origins. Because a jar can only be attached at browser-server
  session *creation*, `load_saved_session` must not try to graft credentials onto a pre-existing
  untrusted browser session: if the conversation already has a remote browser session (e.g. from
  prior arbitrary browsing), the tool **errors**, or — on explicit confirmation — closes that
  session and creates a fresh jar-loaded one. It never attaches a jar to a running non-jar page
  context.
- `forget_saved_session(jar_label_or_id)` → `DELETE /v1/jars/{id}`.

**Jar reference resolution.** The mutating/session-creating tools accept a label or id, so the
adapter must resolve unambiguously: a token is only accepted if it matches exactly one jar across
**both** namespaces, by exact id or exact label — never a fuzzy/first match. Two jars sharing a
label (separate family accounts, a stale duplicate) make it ambiguous, and — because labels are
caller-supplied — so does a token that matches one jar's id *and* another jar's label; any token
resolving to more than one jar by id-or-label is treated as ambiguous rather than decided by
resolver precedence. In all ambiguous cases the tool **errors**. Crucially the ambiguity error must
not itself become an inventory-disclosure side channel (it would hand back the same
ids/origins/saved-by that `list_saved_sessions` is interim-gated to protect): in a browser-tainted /
interim context it returns a **generic** "ambiguous label, please choose from your saved logins" and
routes the actual candidate list through the same confirmed / user-local path as
`list_saved_sessions`, rather than dumping candidates inline. Every `load`/`forget` confirmation
names the resolved jar's id and origins, not just its label.

**Confirmations bind to a resolved jar id, not a label (avoid TOCTOU).** The policy confirmation
wrapper prompts *before* calling the tool, and durable confirmations can span time during which the
jar set changes (a refresh bumps `version`, another jar is added/deleted). So the adapter resolves
the label to a specific `jar_id` **before** requesting confirmation, shows the user that id +
origins, and executes against exactly that id — re-checking the jar's identity (id + `version`
fingerprint) at execution and **aborting if it changed** since approval. Otherwise an approval for
"Woolworths" could load or delete a different jar than the one the human reviewed.

Supporting behavior (not separate tools): when a jar goes stale, re-login starts from a **fresh
human-controlled session, not by loading the stale jar**, and the refresh happens **while that
browser is still alive**. Loading the stale jar first would drop the human into a confined
(exact-origin) session, and confinement would then block the very off-origin IdP redirect
(Google/Microsoft/Facebook) an SSO login needs — SSO-backed jars would become unrecoverable. The
flow is a human-first session with **no jar loaded** (full un-confined control for any IdP bounce),
and the jar is refreshed by **human-save-in-place only**: the human clicks "Save this login", which
refreshes the jar id from the live page *while the human still holds control*, before completing.
Re-login deliberately does **not** hand the freshly-logged-in session back to the agent
(handover-then-agent-save): that would give the agent an authenticated, jar-less (therefore
*un-confined*, un-load-confirmed) browser sitting on a post-credential page — where a malicious
post-login page could drive authenticated navigation/submit, and where password/OTP/recovery-code
fields the human just typed could be exposed to the agent's next snapshot. Human-save-in-place keeps
the browser alive to export from without ever handing the agent that window. (A general human→agent
handover for *other* tasks still exists and rides browser-server's existing sanitized-resume; it is
just not the re-login refresh path.) Refresh keeps the jar's existing approved origin set (scope
unchanged — refresh cannot widen origins). Origin confinement constrains *agent-driven* navigation
in a jar-loaded session; it never traps a human performing a login. Invalidation is **not** taken on
the agent's word alone: a page merely *looking* like a login wall (a modal, an injected "your
session expired" banner) must not be able to flip a jar's state and trigger nuisance re-login
prompts. So the backend `invalidate` call is made only after an independent freshness `probe`
returns `stale`, or on explicit user confirmation — never directly from an agent snapshot of
untrusted page content.

## Policy gating

Via the existing tool-policy engine and durable confirmations — no new enforcement machinery:

- `load_saved_session`: **confirm** by default in every profile that has it. The confirmation
  message names the jar label and **the full reachable-origin set — `jar_origins` *and*
  `jar_nav_allowlist`** ("Open a browser logged in to woolworths.com.au as Andrew?"), so an
  allowlist entry can't silently widen where the confined session may go without the user seeing it
  at load time. Durable confirmations make this work across interfaces and background turns.
- `save_browser_session`: confirm by default, and the confirmation **enumerates every origin** the
  save would capture. Capturing any origin beyond the session's current origin (an IdP/off-site
  origin) is **deny-by-default** — allowed only on an explicit elevated confirmation, never as part
  of a routine site-login save. A human-initiated save (the checkbox on the browser-server handover
  form, recorded as `saved_by: "human"`) already carries explicit consent at save time and needs no
  FA-side confirmation. That checkbox path never passes through this FA wrapper, so the origin-scope
  minimization, off-origin enumeration, and mandatory in-scope probe are **not** FA-only policy:
  they are enforced by browser-server server-side for every save path (see the mechanism doc), and
  the browser-server UI shows the human exactly which origins a save captures. FA's wrapper adds the
  model-facing confirmation on top only for the agent-initiated tool path.
- `forget_saved_session`: **confirm** by default. Deletion is user-recoverable by re-login, but an
  unconditional allow would let injected browser content revoke a household login as a
  denial-of-service (forcing a re-login dance) with no user in the loop. The confirmation is cheap
  relative to that nuisance; the human UI `/jars` page remains the friction-free revocation path.
  Delete (and `invalidate`) also **terminate any live session** loaded from that jar — so "forget my
  login" is a real kill-switch that stops an in-progress authenticated session, not just a
  file-deletion that leaves the running context alive (enforced by browser-server; see mechanism
  doc).
- `list_saved_sessions`: the metadata carries no cookie material, but the labels and origins are an
  inventory of which accounts the household holds — data an injected page could get the model to
  read and then exfiltrate via browser egress. The **end-state** control is frictionless: once the
  taint matrix lands, `list_saved_sessions` is a `sensitive_read_broadening` sink gated only when
  high-tier taint has entered the turn (no prompt on a clean read). But the **interim** cannot lean
  on the egress confinement above — that confinement only exists inside a *jar-loaded* session, and
  the leak path here is an ordinary browser turn (arbitrary-web snapshot, no jar loaded) where the
  browser profile's normal navigation/fill/exec egress is wide open. So until taint enforcement
  exists, `list_saved_sessions` is **confirm-gated** (or, equivalently, unavailable once the turn
  has consumed an untrusted browser snapshot). This is the one spot where the interim accepts a
  confirmation on a read, precisely because the frictionless taint control isn't there yet to cover
  the non-jar case. It relaxes to pure taint-gating **only when the taint policy is actually
  *enforcing* for this sink** — not merely when the taint machinery ships. The shipped taint default
  is observe mode, which downgrades confirm/deny to audit-only; if `list_saved_sessions` dropped its
  static confirm the moment milestone 7 landed, a browser-tainted turn in an observe-mode deployment
  could read the inventory with no gate at all. So the interim confirm is removed per-deployment
  only once this sink is confirmed to be enforcing (milestone 7 must flip it to enforce, not leave
  it observe).
- Profile placement: the browser/handoff-capable profiles only (same `handoff_capable_profiles` gate
  as `request_browser_handoff`). Notably **not** in untrusted-input profiles.

When the taint machinery ([runtime-taint-machinery](runtime-taint-machinery.md)) lands, taint gating
is **additive** — it can escalate a tool to block, but it never drops a tool below its baseline
confirm. `load_saved_session` **keeps its always-confirm baseline** regardless of taint: the
user-visible contract is that the assistant *always asks before opening a saved login*, so creating
an authenticated session must never become unprompted just because the turn is clean; taint can only
*escalate* it (e.g. block after high-tier taint). `save_browser_session` and `forget_saved_session`
likewise keep their static baseline confirmations (always-confirm, plus save's elevated/deny for
off-site origins). The only tool that becomes *frictionless-when-clean* under taint is the pure read
`list_saved_sessions` (a `sensitive_read_broadening` sink gated only once high-tier taint is
present) — which is exactly why it is the one tool confirm-gated in the interim. So across the board
taint *adds* conditions; it never removes a baseline confirm. Taint policy likewise **layers on top
of**, and does not replace, the origin-confinement invariant below. Authenticated browser state is a
credential capability the moment it loads, before any high-tier taint enters the turn; a clean,
user-initiated jar-loaded session must still not be able to navigate or submit cross-origin under
the login. So confinement to the jar's exact origins stays a non-taint baseline, and taint rules add
to it: `load_saved_session` maps to a high-sensitivity sink whose outcome depends on turn taint, and
navigation/form-submit inside a jar-loaded session is the "browser cell" —
`attacker_addressable_egress` evaluated against the session's `jar_origins` (in-origin navigation is
the authenticated-browsing use case; taint tightens same-origin *state-changing* submits after
high-tier taint, on top of the confinement that already blocks cross-origin). The session metadata
(`jar_id`, `jar_origins` on every session response) exists precisely so those rules have ground
truth to evaluate against.

**Where the taint decision physically happens.** A `browser_click`/`browser_fill` tool call exposes
only a DOM ref and a `submit` flag *before* execution — not the final request URL, nor whether a
click will trigger a navigation/submit — so FA policy cannot evaluate the sink from the tool
arguments alone (it would decide before the facts exist, or after the request already left). The
enforcement therefore rides **browser-server's request interceptor**, which does see the actual
outgoing document/form request: for the taint hookup the interceptor pauses an in-scope but
high-sensitivity request and consults the turn's taint verdict (a per-turn taint level passed at
session create / carried on the session, not a per-request round-trip to the model) before
continuing. The conservative interim, until that callback exists, is to **gate `click`/`submit`
under high-tier taint** at the tool layer (confirm or block), accepting some false positives rather
than letting an unknown-destination submit through. Either way the exact-origin confinement below is
unconditional and already blocks the cross-origin case; taint only adds the same-origin state-change
decision.

Snapshots from jar-loaded sessions remain `UNKNOWN_EXTERNAL` taint like all web content — being
logged in makes the fetched content more sensitive, not more trustworthy.

### The interim gap: confirming the load is not enough

Confirming `load_saved_session` gates the *creation* of an authenticated session, but says nothing
about what happens *after*. Once a jar is loaded, the browser profile still permits navigation,
clicks, fills, and (unless disabled) `browser_exec` while every page snapshot is `UNKNOWN_EXTERNAL`.
A malicious authenticated page could therefore drive cross-origin egress (navigating/fetching to an
attacker URL, now carrying authenticated-session-derived data) or same-site state changes under the
login. Deferring *all* of this to the taint milestone would ship a real window where a single
load-time confirmation is the only control.

So the initial saved-session release does **not** wait for the full taint matrix; it pairs the
load-time confirmation with two egress controls that are cheap because the mechanism already exists
in browser-server:

- **Origin-scoped navigation confinement** on jar-loaded sessions: **every off-scope document/form
  request in any frame** — main-frame navigation, form-submit, redirect chains (aborted
  pre-request), popup/`window.open`/`target=_blank` new pages, **and child-frame (iframe) document
  loads / form POSTs** — is blocked. The guard filters on request *type* (`document`), so a form
  targeted at a hidden iframe cannot POST to `https://attacker` while the top-level URL stays on a
  jar origin; only document/navigation requests are gated, so ordinary subresource rendering is
  untouched. The boundary is the jar's **exact `jar_origins`** (scheme + host + **port**, plus any
  explicit saved allowlist exposed as `jar_nav_allowlist`) — *not* its registrable domain (a
  `Domain=.example.com` cookie would ride along to a sibling like `evil.example.com`), and *not*
  scheme+host alone (a differing port `example.com:8443` is a distinct origin). This is
  browser-server's route-interception backstop, pulled forward from a follow-up to a prerequisite
  for enabling load. (Page-JavaScript *subresource* egress — `fetch`/beacon/`<img>`, not document
  requests — remains the deferred residual below.)
- **`exec` default-deny** in jar-loaded sessions (already a browser-server mechanism invariant),
  keeping the model off the one command that can read cookie/storage values.

Two residuals remain after these, both **acknowledged and deliberately not closed in the interim**:

1. **Same-origin action under the login** — the injected page inducing an action on the very site
   the user authorized. This is inherent to authenticated browsing and is precisely what the user
   consented to at the load confirmation ("operate a browser logged in as me at this site"). The
   full taint-by-sink matrix later tightens it (confirming same-origin state-changing submits after
   high-tier taint).
2. **Page-JavaScript subresource egress** — a script on a jar-origin page issuing `fetch`/beacon/
   `<img>` requests to an attacker host while the top-level URL stays on an allowed origin.
   Navigation confinement (top-level document + form-submit) does **not** cover this, and we
   deliberately do **not** try to block all non-jar-origin subresource requests in the interim:
   doing so breaks normal rendering (CDNs, fonts, analytics, third-party widgets that legitimate
   sites depend on), i.e. it would make authenticated browsing unusable to close a low-bandwidth
   channel that the realistic spray-and-pray adversary is unlikely to weaponize per-site. This is
   the general web-content exfiltration problem; the proper home for it is the assessment's
   **egress-proxy / CSP layer under the taint matrix** (tier-dependent network allowlists), not a
   per-jar hack here. Until that lands it is an accepted, disclosed residual — bounded by `exec`
   default-deny (the model itself cannot read cookie/storage values to feed such a script).

Neither residual is an *undisclosed* hole: both are named here, both are what the taint/egress-proxy
milestone exists to address, and the cheap high-value vector (agent-driven cross-origin
navigation/submit under the auth) **is** closed from day one. **Acceptance rule:
`load_saved_session` is not enabled in any profile until origin confinement + `exec` default-deny
are in place** — the two are shipped together, or loading stays disabled.

This gate is a **named, default-off config field**, not merely the broad
`browser_handoff_config.enabled`. The design adds
`browser_handoff_config.saved_sessions_enabled: bool = False` (gating all four jar tools) — so
turning on browser-server handoff does *not* implicitly enable saved-session load/save. An operator
flips `saved_sessions_enabled` only once the confinement prerequisite is verified in their
deployment; while it is off, the four tools are unavailable even in handoff-capable profiles.

### Threat-boundary summary (where this design deliberately stops)

To anchor scope: cookie jars make authenticated browser state **savable, loadable, confinable, and
auditable**, and gate the model-reachable paths from "use a login" to "read/exfiltrate the
credential" (`exec` deny, no cookie material in any response/metadata/log, exact-origin navigation
confinement, confirmations on load/save/forget). It does **not** attempt to make an authenticated
page's own JavaScript trustworthy, to sandbox subresource network egress, or to defend against a
targeted attacker crafting a bespoke low-bandwidth exfil chain against a specific household — those
belong to the egress-proxy/CSP and taint milestones, and to the general web-security posture, not to
this primitive. Calibrated to the assessment's stated adversary (scalable injection in newsletters
and web pages), that is the right stopping point.

## Expiry and re-login orchestration

browser-server provides three signals (cookie-expiry metadata, an on-demand rate-limited probe
endpoint, and agent-reported invalidation); it never acts on its own. Family Assistant owns the
loop, as userspace configuration in keeping with primitives-in-code / behavior-in-configuration:

- A schedule automation probes household-critical jars (e.g. daily) and, on `stale`, notifies the
  user with the re-login ask.
- Re-login is the existing warm-handoff flow, starting from a **fresh un-confined human session**
  (no stale jar loaded, so an off-origin IdP/SSO bounce isn't blocked by confinement): human-first
  session → human logs in → **human-save-in-place** refresh while still in control (never handed
  back to the agent on a post-credential page). Refresh preserves the jar's approved origin set. No
  new primitives.

## User-visible behavior

To document in `docs/user/USER_GUIDE.md` and the browser-profile prompt when implemented:

- "Save this login" appears on the browser handover page; saved logins are listed and revocable in
  the browser service UI (`/jars`) and via chat ("forget my Woolworths login").
- The assistant always asks before opening a browser with a saved login.
- Saved logins keep the site's session data (cookies and browser storage) only — never passwords —
  encrypted on the browser server.

## Milestones

1–4 are browser-server milestones (JarStore; save path; load path; expiry) — see the mechanism doc.
Origin-scoped navigation confinement (the interim egress control) is a **prerequisite of the load
milestone**, not a later add-on: `load_saved_session` ships only once confinement + `exec`
default-deny are enforced.

5. **FA adapter (this repo)**: the four tools on `RemoteBrowserBackend`, registration in code +
   `config.yaml`, policy defaults above, prompts + user guide. Functional tests against a fake
   browser-server asserting (a) confirmation fires before a jar-loaded session exists, (b) no cookie
   material ever reaches the model transcript, (c) `forget`/`load`/`save` confirmations fire, a
   `save` naming an off-site/IdP origin is elevated/denied, and the confirmation resolves+binds to a
   stable `jar_id` (approval aborts if the jar changes before execution), (d) a jar-loaded session
   refuses cross-origin **navigation, form-submit, redirect, and popup** (including a
   same-registrable-domain sibling and a differing port) and `exec` by default, (e) an ambiguous jar
   label errors *generically* without leaking candidates in a browser-tainted turn, (f)
   `load_saved_session` against a conversation that already has a non-jar browser session errors (or
   closes-and-recreates on confirmation) rather than attaching to it, and (g) `list_saved_sessions`
   (and the candidate path) carries `OUTPUT_UNTRUSTED` + `READ_ONLY` + `SENSITIVE_DATA` so a planted
   label both raises turn taint and maps to the `sensitive_read_broadening` sink. The four tools
   stay behind the named default-off `browser_handoff_config.saved_sessions_enabled` flag until
   confinement is verified.
6. **Expiry automation**: shipped example schedule automation for probing + re-login notification.
   Invalidation only follows a `stale` probe or user confirmation, never a bare agent snapshot.
7. **Taint hookup** (after #992/#993 land): taint gating is layered on **additively**.
   `list_saved_sessions` becomes the frictionless-when-clean `sensitive_read_broadening` sink (its
   interim confirm relaxes); `load_saved_session`/`save`/`forget` keep their baseline confirms and
   taint can only *escalate* them; and same-origin state-changing submits under high-tier taint are
   tightened. Origin confinement is **not** replaced: it stays a non-taint baseline invariant.

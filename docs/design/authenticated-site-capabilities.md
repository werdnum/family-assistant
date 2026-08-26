# Bounded Authenticated-Site Capabilities

## Status

Proposed.

If accepted, this design supersedes the **Family Assistant policy and product surface** in
[browser-cookie-jars.md](browser-cookie-jars.md) and the implementation direction in
[PR #1018](https://github.com/werdnum/family-assistant/pull/1018). It does not replace
browser-server's cookie-jar mechanism. It also leaves the Keychute autofill design in
[PR #1069](https://github.com/werdnum/family-assistant/pull/1069) as a later, optional
session-refresh path.

## Decision

Authenticated browsing is exposed as a configured, bounded **site capability**, not as general
model-facing cookie-jar management.

A trusted user request or standing automation grant selects the site capability before any web
content is read. Family Assistant then starts an isolated worker with exactly one saved login, a
fixed browser scope, and no unrelated tools. Web content may influence actions inside that delegated
site capability, including making poor but reversible choices. It may not acquire another saved
login, retrieve a credential, invoke another household capability, or choose a new external
destination.

The operating assumption is deliberately stronger than "the model will resist prompt injection":

> The site worker may become fully controlled by page content. The design remains acceptable when
> the authority available to that worker is too narrow for the compromise to matter materially.

For accounts where the complete same-site browser authority is not an acceptable blast radius,
generic authenticated browsing is the wrong mechanism. Those sites require a site-specific,
deterministic commit adapter that enforces action invariants, or they remain unavailable to an
autonomous agent.

## Why this replaces the earlier policy direction

The earlier saved-session design made loading a jar an always-confirmed high-sensitivity operation
and expected eventual turn-level taint enforcement to govern actions inside an authenticated
browser. [Operational findings](runtime-taint-enforcement-operational-findings.md) show that the
shipped taint matrix remains in observe mode because enforcing it would interrupt ordinary tasks too
often. The follow-up [risk-adjudication design](risk-adjudicated-taint-enforcement.md) correctly
identifies the structural problem: source tier and sink class alone do not express whether an action
follows the trusted user's request.

Authenticated browsing makes that mismatch especially expensive. Every useful browser task reads
untrusted page content and then performs state-changing actions. A policy that treats the resulting
context as generally unable to act prevents the feature from doing its job. A policy that asks on
every load or same-site submit trains confirmation rubber-stamping without creating a meaningful
new decision for the user.

The useful security question is narrower:

> Did the trusted user grant this authority before the untrusted content arrived, and can the
> untrusted content expand that authority?

This design makes that question enforceable without requiring general runtime taint enforcement.

## Goals

- Complete useful authenticated website tasks without routine confirmation prompts.
- Keep passwords, OTPs, cookies, and origin storage out of model-visible data.
- Bound a compromised browser worker to one configured site capability.
- Prevent page content from acquiring additional credentials, accounts, tools, or destinations.
- Make accepted prompt-injection outcomes explicit and proportionate to the site's consequences.
- Support unattended operation from an explicitly configured standing automation grant.
- Preserve enough structured audit and result data to inspect and recover from poor choices.
- Ship a small end-to-end household use case before building autonomous credential refresh.

## Non-goals

- Proving that the browser worker followed only trusted instructions.
- Preventing every poor or surprising action within an intentionally delegated low-consequence site.
- Inferring purchases, account-security changes, or other high-consequence actions reliably from
  generic browser clicks.
- Making arbitrary authenticated accounts safe through one universal browser policy.
- Requiring deployment-wide taint enforcement before authenticated browsing is usable.
- Building automatic MFA or SSO handling in the first release.
- Giving the default assistant a persistent, general-purpose logged-in browser profile.

## Threat model and accepted consequences

The realistic adversary remains scalable prompt injection embedded in web content, not a targeted
operator who has compromised the host or an approved site origin.

The design distinguishes **quality failures** from **authority failures**.

### Accepted quality failures

For a low-consequence delegated site, a compromised worker may:

- select meals the household dislikes;
- choose a less desirable appointment slot from an allowed set;
- reorder preferences inside the delegated workflow;
- produce a poor summary or recommendation;
- require the user to undo a reversible site-local change.

Those outcomes are equivalent in kind to ordinary model mistakes. They are recorded and reported,
but they are not treated as security-boundary failures.

### Rejected authority failures

A compromised worker must not be able to:

- load another site's saved session;
- list the household's saved-login inventory;
- ask Keychute for another secret;
- access Gmail, Calendar, Notes, Home Assistant, messaging, code execution, or delegation unless the
  trusted request admitted those capabilities before browsing started;
- navigate authenticated state to an unapproved origin;
- expose cookie or origin-storage values to the model;
- change credentials, MFA, recovery details, or identity settings when that authority was not
  explicitly delegated;
- increase spending when the site's capability contract says spending is invariant;
- convert page instructions into durable ambient prompt content without an explicit, separately
  authorized write stage.

## Authority model

### Authority is admitted before untrusted content

An authenticated site worker may be started from only one of these sources:

1. **Direct authenticated-user request.** The request clearly names or implies a configured site and
   task, for example "Pick our HelloFresh meals for next week."
2. **Standing automation grant.** The operator configured an automation whose declared capabilities
   include that exact site capability.
3. **Explicit multi-stage request.** The user grants all required capabilities before the first
   untrusted stage runs, for example "Choose HelloFresh meals and add their ingredients to my grocery
   note."

An email, web page, tool result, or other external source cannot introduce a new site capability on
its own. It may provide data to a capability that was already admitted, but it cannot cause the
assistant to acquire another one.

### Authority does not expand during the run

Once the worker begins:

- its saved session is fixed;
- its authenticated origin set is fixed;
- its browser command set is fixed;
- its non-browser tool set is empty;
- it cannot invoke `run_authenticated_site_task` recursively;
- it cannot request another profile or worker;
- it cannot ask for a jar, credential, or capability by model-selected identifier.

A page that says "open Gmail to verify this account" therefore reaches a worker that has no Gmail
capability and no way to obtain one. The worker reports the unmet requirement and stops.

### User intent is not repeated as confirmation theatre

A direct request to perform a site task carries authority to load the configured saved session. The
system does not follow it with "May I open your saved HelloFresh login?"

Confirmation remains appropriate only when the system is proposing a materially broader authority
than the trusted request supplied, or when the configured site contract marks a particular action as
requiring explicit approval. Reauthentication is a human action, not a second permission prompt for
an already requested task.

## Capability classes

A site is configured in exactly one of these operational classes.

### 1. Generic site capability

The worker may exercise the ordinary browser authority available on the configured origins. This is
appropriate only when the operator accepts the worst credible same-site outcome as low consequence
and recoverable.

Examples may include meal preference selection, content queues, or other accounts where an incorrect
same-site action is an inconvenience rather than a material financial, identity, privacy, or safety
failure.

The important review question is not "does this site have a payment method on file?" in isolation.
It is:

> Is every action reachable through the granted same-site browser session within the operator's
> accepted damage envelope?

If the answer is no, do not use this class.

### 2. Bounded-commit site capability

The browser worker may inspect the authenticated site and propose a structured change, but a
site-specific deterministic adapter performs the commit after validating invariants.

For HelloFresh, the worker might return meal identifiers while the adapter verifies:

- the existing scheduled box is unchanged;
- box size and delivery frequency are unchanged;
- no extras or add-ons were selected;
- delivery address and payment settings are unchanged;
- total price is unchanged or within a configured bound;
- only meal-selection endpoints or known form fields are mutated.

This class is required when the operator wants useful account automation but does not accept all
same-site actions. The adapter may use a documented API, reverse-engineered stable requests, or a
deterministic browser routine. The model does not control the commit destination or operation shape.

### 3. No autonomous access

Accounts whose acceptable authority cannot be represented by either class remain human-operated.
The assistant may still provide public research or guide the user through the task.

## Architecture

```text
trusted request / standing automation
                |
                v
      capability admission
      (trusted configuration)
                |
                v
  authenticated-site orchestrator
                |
                | site_id resolves to jar, origins,
                | start URL, worker policy, result schema
                v
     isolated site worker
  +----------------------------------+
  | one jar-loaded browser session   |
  | configured origins only          |
  | browser interaction tools        |
  | no jar or credential tools       |
  | no household or egress tools     |
  | structured result only           |
  +----------------------------------+
                |
                +------ generic class: commit in browser
                |
                +------ bounded class: proposed change
                                      |
                                      v
                              deterministic adapter
                                      |
                                      v
                              validated site change
```

### Component responsibilities

#### Family Assistant orchestrator

- Resolves a trusted `site_id` to operator-controlled configuration.
- Determines whether the current trigger has authority to use it.
- Creates a fresh jar-loaded browser session.
- Starts the restricted worker with the user's objective and a capability description.
- Collects a typed result and audit summary.
- Invokes a deterministic adapter for bounded-commit sites.
- Returns `login_required`, `blocked_by_scope`, `needs_human`, or a completed result without silently
  broadening authority.

#### browser-server

The shipped mechanism remains responsible for:

- encrypted, opaque cookie jars;
- no cookie or origin-storage values in API responses, events, or logs;
- load only at fresh session creation;
- immutable reporting of jar origins and generation;
- exact-origin top-level navigation and form confinement;
- `exec` default-deny in jar-loaded sessions;
- no model observation while a human owns the browser;
- revocation and invalidation closing live sessions;
- freshness probes and bounded jar retention.

These are mechanism invariants with negligible normal-use friction and remain hard requirements.

#### Keychute

Keychute is not part of the first useful path. A human creates and refreshes saved logins through the
existing browser handoff.

Later, browser-server may use Keychute's trusted-client autofill release to refresh a first-party
credential jar. The login session remains terminal, the secret never enters Family Assistant, and
only the refreshed jar survives. SSO and unusual MFA continue to use human handoff.

#### Site-specific adapter

A bounded-commit adapter owns the narrow mutation contract for one site. It accepts typed proposed
state, reads any necessary current state, verifies invariants, and performs only its documented
operation. Its interface contains no arbitrary URL, selector, script, request method, or request
body selected by the model.

## Configuration model

Illustrative configuration:

```yaml
authenticated_sites:
  hellofresh:
    display_name: "HelloFresh"
    jar_id: "jar_0123456789abcdef0123456789abcdef"
    start_url: "https://www.hellofresh.com.au/menus"
    authenticated_origins:
      - "https://www.hellofresh.com.au"
    navigation_allowlist: []
    capability_class: "bounded_commit"
    adapter: "hellofresh_meal_selection"
    allowed_triggers:
      direct_user: true
      standing_automations:
        - "weekly_hellofresh_selection"
    result_schema: "hellofresh_meal_selection_v1"
```

The configuration is operator-controlled and not model-generated at runtime. In particular:

- `site_id` is a stable token, not a jar label supplied by the model;
- `jar_id` is never exposed to the site worker;
- the complete origin set is known before session creation;
- the capability class cannot be weakened by page content;
- a bounded adapter is selected by configuration, never by the worker;
- standing automation grants name exact capability IDs.

Multiple household accounts on one site are separate capability IDs, for example
`hellofresh_andrew` and `hellofresh_partner`, each bound to one jar. The user-facing capability name
may be friendly, but resolution is deterministic and never fuzzy.

## Product surface

### Primary tool

The default assistant receives one high-level tool:

```text
run_authenticated_site_task(
    site_id: str,
    objective: str,
    expected_result: str | None = None,
) -> AuthenticatedSiteTaskResult
```

The tool does not accept a jar ID, arbitrary start URL, origin list, profile ID, adapter name, or
browser permissions. Those values come only from trusted configuration.

`site_id` values advertised to the model are filtered to capabilities available to the acting user
and current trigger. An ambient or external-triggered profile sees no interactive site capabilities
unless its standing automation configuration explicitly grants one.

### Management surface

Saved-login creation, inspection, refresh, and deletion remain human management operations in
browser-server's authenticated UI for the first release. They are not general model tools.

Family Assistant may later expose user-local status such as "HelloFresh login is stale" without
returning the full jar inventory or model-controlled labels. A trusted settings page is the primary
management surface.

### Result contract

The worker returns typed data rather than unconstrained prose wherever the site workflow permits it.
A generic result envelope is:

```json
{
  "status": "completed",
  "site_id": "hellofresh",
  "summary": "Selected five meals for the next delivery",
  "actions": [
    {"type": "selected_meal", "id": "meal-123", "label": "..."}
  ],
  "warnings": [],
  "evidence": {
    "final_url": "https://www.hellofresh.com.au/menus",
    "final_state": "selection_saved"
  }
}
```

Strings copied from pages remain untrusted data. They do not become instructions to the caller. A
multi-stage workflow consumes typed fields under capabilities already admitted by the original user
request. It does not inspect the site's prose result and decide which new tools to acquire.

## Worker confinement

The site worker is a dedicated processing profile or delegated runtime whose effective capabilities
are assembled per run.

It receives:

- the trusted user objective;
- a short capability contract stating the site and allowed outcome;
- the already-created jar-loaded browser session;
- browser snapshot, click, fill, wait, navigation, and extraction operations needed for the site;
- no aggregate household context unless explicitly required and admitted;
- no saved-login metadata;
- no credential access;
- no general URL fetch, shell, Python, sandbox network, messaging, notes, calendar, email, Home
  Assistant, task management, delegation, or worker-spawn tools.

`exec` remains disabled by default. A per-site opt-in is considered only after a real workflow proves
that ordinary DOM/browser operations cannot complete it. Enabling it is a security-significant site
configuration change because page JavaScript can expose non-HTTP-only cookie or origin-storage data.

The worker is disposable. The browser session closes after completion or failure; persistence lives
only in the encrypted jar and the site's own server-side account state.

## Navigation and origin handling

Credential and cookie scope remain exact-origin controls. The configured authenticated origin set is
passed to browser-server and cannot be widened by the worker.

`navigation_allowlist` is for explicitly reviewed origins that the workflow must reach without
capturing their storage in the jar. It is not an arbitrary-link permission and is displayed in the
trusted configuration UI. Unexpected navigation produces `blocked_by_scope` rather than an
interactive "allow this origin?" prompt sourced from page content.

A workflow that needs broad public research should run that research in a separate unauthenticated
stage. The authenticated worker does not gain arbitrary web navigation merely because the current
site linked elsewhere.

SSO login redirects are a provisioning concern, not a task-session permission. Human control may
traverse the IdP during login; the resulting task jar should capture only the minimum application
state needed for the configured site. Multi-origin application state is explicitly reviewed when
saved.

## Consequence policy

The capability class, not generic taint state, determines the maximum allowed consequence.

### Routine delegated actions

No additional confirmation is required when the action is within the site capability selected by
the trusted request. This includes state-changing actions such as selecting meals when that is the
purpose of the capability.

### Material commitments

A site may define explicit sub-capabilities or adapter invariants for actions such as skipping a
paid delivery, cancelling a subscription, changing a delivery address, or changing a plan. These are
not discovered through a universal click classifier.

The options are:

- exclude the action from the deterministic adapter;
- require a separate trusted user request naming the action;
- expose a dedicated confirmed tool outside the generic worker; or
- classify the whole account as unsuitable for autonomous access.

### Money, security, and identity

Generic authenticated browsing must not be presented as guaranteeing that these actions are blocked.
If the account exposes them on the same origins and the operator does not accept that authority, the
site must use a bounded adapter or remain human-only.

This design intentionally rejects a generic "purchase detector" based on page text, button labels,
DOM shape, or model judgment. Such a detector would be brittle precisely where a hard guarantee is
required.

## Relationship to taint and provenance

Runtime taint remains useful for audit, provenance, ambient prompt admission, and future adjudication.
It is not the authority mechanism for this feature and is not a prerequisite for enabling it.

The authenticated worker's page observations and outputs remain marked as externally derived. That
provenance may inform later review or durable-artifact policy. It does not cause per-click
confirmation inside the already admitted site capability.

The key enforcement rule is:

> External data may influence computation inside an admitted capability, but it cannot cause a new
> capability to be admitted.

For explicit multi-stage requests, all capabilities are admitted from the trusted request before the
external stage starts. Stages remain separated, and structured data is passed between them. For
example:

1. The user requests HelloFresh selection plus a grocery-note update.
2. The orchestrator admits `site:hellofresh` and `notes:grocery-list` from that request.
3. The site worker returns typed ingredients.
4. A separate deterministic or notes-capable stage writes those ingredients as data.
5. The site worker itself never receives Notes access.

Durable content derived from a site should not become ambient prompt instructions merely because it
was written to a note. Existing prompt-admission and provenance mechanisms remain the appropriate
chokepoint for that separate concern.

## Login lifecycle

### Initial provisioning

1. A human opens a browser-server session.
2. The human signs in under exclusive browser control and completes MFA or SSO.
3. The human saves the login as a cookie jar.
4. The operator binds the jar to an authenticated-site configuration entry.
5. A direct user task may use it without another load confirmation.

### Normal task

1. Capability admission resolves the trusted `site_id`.
2. Family Assistant creates a fresh session with that jar and browser-server confinement enabled.
3. It navigates to the configured start URL.
4. The isolated worker completes the task or returns a bounded failure.
5. The session closes and the user receives a structured action report.

### Expired login

1. A probe or task detects that the jar is stale.
2. The task returns `login_required` with the site name and a trusted handoff action.
3. A human signs in and refreshes the same jar in place.
4. The original task may be retried from its trusted objective.

The agent does not invalidate a jar solely because an untrusted page claims the session expired.
Independent probing or explicit human action remains the source of truth.

### Later autonomous refresh

If measured expiry friction justifies it, browser-server may perform the Keychute-backed first-party
credential fill described in
[PR #1069](https://github.com/werdnum/family-assistant/pull/1069). The resulting flow refreshes
the configured jar; it does not create a general authenticated browser capability or expose
credentials to the worker.

## Failure behavior

The high-level tool returns one of a small set of actionable outcomes:

- `completed`: requested workflow completed and was reported;
- `login_required`: saved session is stale or missing;
- `blocked_by_scope`: the site attempted an unconfigured origin transition;
- `blocked_by_contract`: a bounded adapter rejected the proposed mutation;
- `needs_human`: CAPTCHA, unsupported MFA, SSO, bot detection, or ambiguous workflow;
- `site_changed`: configured selectors, routes, or result evidence no longer match;
- `failed`: browser or worker failure with no claimed completion.

Failures do not trigger capability broadening. A worker cannot solve `blocked_by_scope` by asking for
an arbitrary origin, and it cannot solve `login_required` by requesting a model-visible password.

## Audit and recovery

Every run records metadata sufficient to understand what authority was exercised without recording
secrets:

- acting user and trusted trigger type;
- site capability ID and configuration version;
- jar ID or an opaque stable reference in privileged audit data, never worker-visible;
- jar generation and effective origin set;
- start and end time;
- worker model/profile version;
- major workflow actions and typed result;
- bounded-adapter decision and invariant results;
- final status and failure reason;
- optional final screenshot or sanitized state evidence when configured.

The user-facing completion report names the actions taken. For reversible sites, recovery is
primarily inspect-and-undo rather than pre-action confirmation.

Audit data is not itself an ambient prompt source. Page-derived labels remain data and are escaped or
structured at every display boundary.

## First vertical slice: HelloFresh meal selection

The first release target is:

> "Pick our HelloFresh meals for next week" completes end to end from a manually saved login, with
> no routine confirmation prompt and no authority outside the configured HelloFresh capability.

### Acceptance criteria

1. A human can save and bind one HelloFresh login.
2. A direct authenticated-user request automatically admits the HelloFresh capability.
3. Family Assistant creates a fresh confined browser session from the bound jar.
4. An isolated browser-only worker reads the available meals and account history needed for the
   task.
5. It can select and save ordinary meal choices without asking for jar-load confirmation.
6. It cannot access another jar, Keychute, Gmail, Notes, messaging, Home Assistant, code execution,
   arbitrary network, or delegation.
7. A page instruction asking for any unavailable capability fails without expanding authority.
8. The result names the meals selected and whether the site reported the selection as saved.
9. A stale session produces one clear human reauthentication workflow.
10. Undesirable meal selection is accepted as a quality failure and is visible in the action report.
11. If "no additional spending" is a hard requirement, completion is not claimed until a
    HelloFresh-specific adapter proves the price/box invariants or the workflow is kept human-only.
12. The end-to-end task succeeds without runtime taint enforcement being switched from observe mode.

### Generic browser versus bounded adapter

The first implementation should test the real HelloFresh account surface before deciding the class.
If same-origin navigation exposes paid extras, box-size changes, address changes, subscription
changes, or other unacceptable actions that cannot be removed from the worker's reach, HelloFresh
uses the bounded-commit class from the outset.

The design does not assume that a payment method on file automatically makes generic browsing
unacceptable, nor that a meal-selection UI automatically makes it safe. The decision is made from
the actual reachable authority and the operator's accepted damage envelope.

## Implementation plan

### M1 — Configuration and backend session creation

- Add `authenticated_sites` operator configuration and validation.
- Add a trusted registry resolving `site_id` to jar, origins, start URL, capability class, trigger
  grants, result schema, and optional adapter.
- Add the minimal `RemoteBrowserBackend` jar methods needed to load a known jar into a fresh session
  and probe its status.
- Never send `confine_navigation: false` or `allow_exec: true` by default.
- Verify the created session's jar generation and origin metadata against trusted configuration.

### M2 — Isolated worker and high-level tool

- Add `run_authenticated_site_task` to the trusted interactive profiles.
- Create the restricted site-worker profile/runtime with a per-run browser capability.
- Ensure the worker has no jar-management, credential, household, egress, delegation, or code tools.
- Enforce a structured result envelope and close the browser session on every terminal path.
- Add audit records and user-facing action reports.

### M3 — Human provisioning and stale-session recovery

- Use browser-server's existing human sign-in and saved-login UI for initial provisioning.
- Add a trusted operator mapping from saved jar to site capability.
- Surface `login_required` without exposing the full jar inventory to the model.
- Support human refresh of the same jar ID and retry from the original objective.
- Resolve the existing browser-server refresh hint/UI gaps only as needed by this flow.

### M4 — HelloFresh end-to-end workflow

- Build a site skill or worker guidance for reading menus, history, and household constraints.
- Exercise the real workflow through browser-server, including bot-detection and session-lifetime
  behavior.
- Capture typed meal choices and stable completion evidence.
- Measure reliability, intervention rate, and which browser primitives are missing.

### M5 — Consequence boundary

- Inspect the actual reachable HelloFresh account actions.
- If generic authority exceeds the accepted envelope, implement
  `hellofresh_meal_selection` as a bounded deterministic commit adapter.
- Test that paid extras, plan changes, address changes, subscription changes, and price increases are
  structurally unavailable or rejected.

### M6 — Measured follow-ups

Only after the vertical slice is in regular use:

- add scheduled standing-grant execution;
- add Keychute-backed jar refresh if expiry is a recurring burden;
- add more configured sites based on explicit consequence review;
- add per-site `exec` opt-ins only when required;
- consider a trusted site-management UI rather than general model-facing jar tools;
- use taint/adjudication data to improve audit and cross-stage policies without making it a release
  gate.

## Changes to prior plans

### browser-cookie-jars.md

Keep:

- the browser-server mechanism split;
- encrypted and model-opaque jars;
- human save and refresh;
- load into a fresh confined session;
- freshness probing and revocation;
- default-denied `exec`.

Supersede:

- the four general jar tools as the primary model surface;
- confirmation on every `load_saved_session`;
- jar inventory access as a normal model workflow;
- runtime taint enforcement as the prerequisite or end-state controller for in-site actions;
- per-click or per-submit same-origin escalation inside an already admitted low-consequence
  capability.

### PR #1018

The backend reconciliation in
[PR #1018](https://github.com/werdnum/family-assistant/pull/1018) remains useful, especially its
notes on shipped browser-server semantics. Its proposed Family Assistant product surface and work
ordering should be replaced by this design. The useful backend subset is incorporated into M1 and
M3 above.

### PR #1069

The narrowed Keychute design in
[PR #1069](https://github.com/werdnum/family-assistant/pull/1069) remains valid. Agent-driven login
is still a jar-refresh path, not a live-session transfer. It moves behind the first useful
saved-session workflow and feeds a configured site capability rather than general browser access.

### Runtime taint designs

Keep provenance collection, ambient prompt admission, diagnostics, and research into
risk-adjudicated enforcement. Do not require deployment-wide enforcement for authenticated-site
capabilities. The worker boundary and pre-admitted authority envelope provide the relevant runtime
control for this feature.

## Security properties

The design is successful when all of these are true:

- A browser worker cannot choose which household account or jar it receives.
- Page content cannot cause another capability to be admitted.
- The worker cannot see credentials, cookie values, or origin-storage values.
- The authenticated browser cannot issue top-level navigation or form requests outside configured
  scope.
- The worker cannot invoke unrelated household or egress tools.
- A direct trusted request does not incur redundant jar-load confirmation.
- A standing automation cannot use a capability it did not declare.
- A bounded adapter's invariants are deterministic and cannot be weakened by model arguments.
- Revoking a jar terminates live sessions using it.
- Human control remains exclusive during credential and MFA entry.
- Site-derived output is returned as data and cannot dynamically expand downstream authority.

## Accepted residuals

- An approved site origin can read credentials that a password manager or future Keychute autofill
  places into its own page. This is inherent to autofill and is accepted only for operator-approved
  origins.
- A compromised generic site worker can perform any same-site action the account and configured
  browser scope make reachable. Generic capability use asserts that this damage envelope is
  acceptable.
- Same-origin application JavaScript may make subresource requests not covered by top-level
  navigation confinement. Credentials remain origin-scoped, `exec` remains disabled, and sites with
  an unacceptable residual require stronger browser egress controls or a bounded adapter.
- Browser automation may break when a site changes. The failure must be visible and must not claim
  completion without configured evidence.
- Some sites will block automated or cloud browsers and require human operation.
- Typed result fields may still contain attacker-controlled strings. They remain data, are bounded
  and escaped, and do not confer authority.

## Review questions

1. Is the high-level `run_authenticated_site_task` surface preferable to exposing generic jar
   management tools to the model?
2. Is the authority rule sufficiently clear: capabilities come only from the trusted request or a
   standing grant, never from content encountered during execution?
3. Should the restricted worker be a dynamic processing profile, a delegated agent with an ephemeral
   tool provider, or a separate orchestration primitive?
4. Is the generic-versus-bounded capability distinction the right place to express consequences that
   generic browser actions cannot classify reliably?
5. Should HelloFresh begin as generic browsing for empirical discovery, or should the presence of
   possible paid extras require a bounded adapter before the first commit?
6. Which browser-server follow-ups are truly required for the first slice: refresh-in-place,
   multi-origin human save, stronger subresource egress, or none?
7. Is the audit/result envelope sufficient to make execute-and-report preferable to routine
   pre-action confirmations for low-consequence sites?
8. Does any existing taint or confirmation machinery still need to gate capability admission, as
   opposed to observing provenance after admission?

## Validation plan

This is a design-only change. Before implementation:

- review against browser-server's shipped jar API and confinement semantics;
- review against Family Assistant's profile, delegation, and confirmation architecture;
- threat-model the first HelloFresh configuration from the actual reachable account UI;
- verify that the proposed worker can be assembled without inheriting global tools or aggregate
  context;
- decide the HelloFresh capability class and document its accepted damage envelope;
- mutation-test the future tool registry so adding a global tool cannot silently reach the worker;
- add end-to-end tests for capability non-expansion, stale login, scope block, worker cleanup, and
  bounded-adapter rejection before enabling unattended execution.

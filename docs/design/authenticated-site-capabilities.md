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

Authenticated browsing is exposed as an operator-configured, bounded **site capability**, not as
general model-facing cookie-jar management.

The implementation uses Family Assistant's existing processing-profile and delegation system:

- a trusted profile such as `default_assistant` invokes a high-level authenticated-site tool;
- the tool creates a fresh browser-server session from the configured jar;
- the task runs under static authenticated variants of the existing `browser_profile` and
  `browser_visual_profile`, sharing the same browser session through the existing delegation path;
- the browser profiles receive the normal browser tools, but no jar-selection or credential tools;
- browser-server confines the session to the configured authenticated origins.

There is no new per-site worker type, dynamically synthesized profile, or universal read-only
browser mode.

The governing principle is **bounded damage**:

> When the operator enables a site, they are choosing to let the model exercise the authority that
> the authenticated session exposes on the configured origins. The hard security boundary limits
> where that authority can go. Everything that tries to make the model behave correctly inside the
> boundary is defence in depth, not a proof.

The model may make mistakes or follow malicious page instructions within the configured site. The
design is acceptable when the worst credible result is within the operator's declared damage
envelope. If the full same-site browser authority is unacceptable, the operator must use a narrower
deterministic integration or leave the site human-operated.

## Why this replaces the earlier policy direction

The earlier saved-session design made loading a jar an always-confirmed high-sensitivity operation
and expected eventual turn-level taint enforcement to govern actions inside an authenticated
browser. [Operational findings](runtime-taint-enforcement-operational-findings.md) show that the
shipped taint matrix remains in observe mode because enforcing it would interrupt ordinary tasks too
often. The follow-up [risk-adjudication design](risk-adjudicated-taint-enforcement.md) identifies
the structural problem: source tier and sink class alone do not express whether an action follows
the trusted user's request.

Authenticated browsing magnifies that mismatch. Useful tasks necessarily read page content and then
perform browser actions, many of which may mutate state. A policy that treats every page-derived
turn as unable to act prevents the feature from doing its job. A policy that asks before every jar
load or same-site action trains confirmation rubber-stamping without presenting the user with a new
choice.

At the same time, there is no generic browser primitive that means "understand and navigate this
site, but never change anything":

- clicks may commit immediately;
- fields may autosave before submission;
- navigation can trigger state-changing endpoints;
- applications often use POST, GraphQL, or background requests for both reads and writes;
- page JavaScript can issue requests independently of the model's explicit action;
- per-site URL, selector, or endpoint allowlists are brittle and expensive to maintain.

This design therefore makes a smaller and honest claim. It hardens the credential and cross-site
boundaries, exposes the remaining same-site authority to the operator, and treats semantic action
review as an imperfect mitigation.

### Market context

This is a practical product assumption rather than an unprecedented security posture. As of August
2026, mainstream consumer agent products operate authenticated browser sessions under the same
bargain this design makes explicit — a few mechanical boundaries, probabilistic action review, and
residual risk accepted by the user:

- **ChatGPT's cloud browser** (which replaced the Atlas local-browser agent) pauses at login walls
  for a human to enter credentials into a secure form that bypasses the model, persists the
  resulting cookies across tasks, screens sign-in destinations with a phishing-review model, gates
  site access behind per-site approval, and separately confirms consequential actions. That is a
  remote confined browser, human login handoff, opaque saved sessions, standing per-site grants, and
  confirm-consequential — the same architecture as this design.
- **Claude in Chrome** runs inside the user's real browser with every logged-in session ambient, and
  compensates with trained injection and per-action classifiers, confirmations for sensitive
  actions, and category blocklists — while stating that the residual risk is not zero and that the
  user remains responsible for actions taken in their authenticated sessions.
- **Perplexity Comet** shipped agent access to the user's full logged-in browser with neither
  confinement nor a comparable classifier stack, and is the demonstrated cautionary case:
  page-content injection driving actions across the user's authenticated sessions.

No vendor claims a read-only or injection-proof authenticated browser. Where this design differs
from the commercial offerings it is mostly tighter: per-site jars, exact-origin confinement, a fresh
session per task, an explicit damage envelope, and caller-profile gating have no consumer
equivalent. What the vendors have that a self-hosted deployment lacks is the trained classifier
layer; this design's analogues (native computer-use safety decisions, the optional action judge) are
thinner, which is one reason the confinement layer carries more of the weight here.

Tighter confinement may also prove less capable than a general logged-in browser. Whether any
boundary should be relaxed is a question to answer from operational experience with real workflows
(M4/M6), not in advance.

## Goals

- Complete useful authenticated website tasks without routine confirmation prompts.
- Use the existing processing-profile, delegation, browser-backend, and confirmation architecture.
- Keep passwords, OTPs, cookies, and origin storage out of model-visible data.
- Bind each browser run to one operator-configured jar and origin set.
- Prevent the browser processing profile from directly selecting another jar or retrieving a
  credential.
- Keep unrelated household capabilities out of the browser profile's tool set.
- Make the operator's accepted same-site damage envelope explicit.
- Preserve ordinary multi-step reasoning after browser results rather than requiring every browser
  task to terminate the caller's model loop.
- Preserve untrusted provenance on browser observations and results for the existing taint and
  future adjudication machinery.
- Add practical mitigations without representing them as a complete read/write barrier.
- Ship a small end-to-end household use case before building autonomous credential refresh.

## Non-goals

- Proving that the model followed only trusted instructions.
- Creating a generic read-only authenticated browser.
- Reliably classifying every browser action as read-only or state-changing.
- Maintaining fine-grained per-site endpoint, selector, or button allowlists.
- Guaranteeing that a general browser session cannot purchase, cancel, agree, submit, or alter
  account state.
- Requiring authenticated-site tools to be terminal in the caller's model loop.
- Treating typed JSON strings as trusted merely because they have a schema.
- Inferring purchases, security changes, or legal commitments reliably from generic UI semantics.
- Making arbitrary authenticated accounts safe through one universal browser policy.
- Requiring deployment-wide taint enforcement before authenticated browsing is usable.
- Building automatic MFA or SSO handling in the first release.
- Giving the default assistant direct access to cookie jars or a persistent general-purpose
  logged-in browser profile.

## Threat model

The realistic adversary remains scalable prompt injection embedded in content the model reads, plus
ordinary model error. A targeted attacker who has compromised the host, browser-server, Keychute, or
the operator-approved first-party origin is outside the mechanism's protection.

### Same origin is a damage boundary, not a trust label

Once a browser profile is operating on an authenticated origin, content from that origin may
influence what it does on that origin. This is intentionally accepted when the site is configured as
a general browser capability.

For a service such as HelloFresh, malicious instructions in the site's own UI generally imply that
HelloFresh, or code executing with HelloFresh's origin authority, is compromising its own account
surface. The design does not attempt to defend the service from itself while still giving the model
general control of that service.

This does **not** mean same-origin content is trustworthy. It means that same-origin effects are
inside the damage envelope the operator accepted. Sites that display attacker-controlled third-party
content while also exposing valuable mutations have a larger envelope. Gmail, customer-support
queues, social networks, and admin consoles may therefore be unsuitable as general browser
capabilities even though all interaction occurs on one origin.

### Accepted failures

For a configured general browser capability, the model may:

- choose meals the household dislikes;
- select a poor appointment slot;
- change a reversible preference;
- follow a misleading page instruction that causes an unwanted same-site action;
- produce an incorrect summary or recommendation;
- require the user to inspect and undo a site-local change;
- agree to or submit something the user did not intend, if that action is reachable inside the
  accepted same-site authority.

The last item can still be serious. It is accepted only for sites whose reachable actions the
operator has decided are tolerable. Semantic authorization and confirmation can reduce the
probability; they do not turn a general browser into a hard-limited adapter.

### Rejected boundary failures

The browser processing profile must not be able to:

- inspect or choose arbitrary cookie jars;
- retrieve a password, OTP, cookie value, or origin-storage value;
- load another authenticated site from within the browser task;
- call Keychute for another secret;
- invoke Gmail, Calendar, Notes, Home Assistant, messaging, code execution, task management, or
  other household capabilities directly;
- navigate authenticated top-level documents or forms outside the configured scope;
- observe the browser while a human is entering credentials or MFA;
- survive revocation of the jar that created its session.

These are mechanism and processing-profile boundaries. Unlike semantic action review, they are
intended to hold even when page content completely controls the browser model.

## Hard boundaries and imperfect mitigations

The design separates properties that can be enforced mechanically from properties that can only be
improved probabilistically.

### Hard boundaries

- **Configured jar resolution:** the high-level tool resolves an operator-controlled `site_id` to a
  jar. The browser profile never sees the jar inventory or supplies a jar ID.
- **User binding:** each site configuration names the users authorized to act on the bound account.
  Caller profiles are shared across household members, so per-user authorization comes from the site
  configuration, not the profile: resolution fails closed for any acting user not listed.
- **Fresh session creation:** authenticated state is attached only when browser-server creates the
  browser context.
- **Origin confinement:** jar-loaded sessions retain browser-server's exact-origin top-level
  navigation and form confinement.
- **Opaque session state:** cookie and origin-storage values are not returned through model-facing
  APIs or logs.
- **Profile tool policy:** the browser processing profile has browser tools, not unrelated
  household, messaging, credential, or general network tools. The boundary is the *effective*
  surface, not the profile-local policy: globally granted tools (`read_text_attachment`, `jq_query`,
  `report_technical_problem`) land in a layer a profile's own policy cannot refuse and are withheld
  via `excluded_global_tools`, and ambient household context providers (notes, calendar, known
  users, weather, Home Assistant) are excluded via `excluded_context_providers`, so the browser
  worker sees the objective and task-scoped facts, not the household's data.
- **No recursive site acquisition:** `run_authenticated_site_task` is not available to the browser
  profiles.
- **No `exec` in jar-loaded sessions:** arbitrary page evaluation reads non-HttpOnly cookies and
  origin storage, which would collapse the opaque-session-state boundary. A leaked session token is
  not same-site damage: it can be replayed from attacker infrastructure, outside every mitigation,
  audit, and confinement path, until revocation. Jar-loaded sessions therefore never expose `exec`;
  a workflow that needs scripted page access is the deterministic-adapter path, not an opt-in.
- **Exclusive human control:** snapshots and commands fail while the human owns the browser.
- **Revocation:** deleting or invalidating a jar terminates sessions loaded from it.

### Imperfect mitigations

- model adherence to the user's objective;
- prompt-injection detection;
- Gemini computer-use safety decisions;
- a separate action-review judge;
- human confirmation for actions judged consequential;
- before/after or postcondition checks;
- typed result schemas;
- user-visible action summaries;
- audit, notification, and easy revocation;
- site-specific deterministic adapters.

The first eight reduce risk but do not prove that an unintended same-site mutation cannot occur. A
site-specific adapter can provide a hard narrow interface only when the model does not also retain a
general mutation-capable browser path for that operation.

## Processing-profile architecture

Authenticated browsing fits into the existing profile system rather than introducing a second agent
runtime.

```text
trusted caller profile
(default_assistant, complex_tasks, or an explicitly configured automation profile)
                |
                | run_authenticated_site_task(site_id, objective)
                v
   high-level authenticated-site tool
                |
                | resolve configured jar + origins
                | create fresh browser-server session
                v
   authenticated_browser_profile
  semantic snapshots / click / fill / wait
                |
                | existing delegation when visual action is needed
                v
   authenticated_browser_visual_profile
  Gemini native computer use + safety decisions
                |
                v
       result returns to caller
  with browser/external provenance preserved
```

### Caller profile

The high-level tool is granted through the ordinary `tools_policy` system only to profiles the
operator trusts to select configured site capabilities. It is not granted to `browser_profile`,
`browser_visual_profile`, `event_handler`, externally triggered profiles, or other profiles that
should not acquire authenticated sessions.

The default design does not introduce a request-bound admission token. Configuring a site and making
the tool available to a caller profile is a standing operator grant: that profile may ask to use the
site when reasoning about a task.

This means untrusted context in the caller may influence which configured site it chooses. That is
the existing cross-capability prompt-injection problem addressed by runtime taint, risk
adjudication, profile segregation, and confirmations. It is not solved by the browser-session
mechanism and remains an accepted residual while deployment-wide taint enforcement stays in observe
mode.

Operators who need a narrower activation rule can use the mechanisms Family Assistant already has:

- grant a site-specific wrapper tool only to a dedicated static processing profile;
- expose the capability only through a slash command or configured automation profile;
- put the high-level tool behind existing tool-policy confirmation — one confirmation per task
  invocation, unlike per-action prompting, stays cheap and is a reasonable default for a newly
  configured site until the operator relaxes it;
- require a model adjudication step once that design is implemented.

The design does not require a new capability-admission object or another policy engine.

### `browser_profile`

The existing semantic browser profile remains the main actor. The authenticated-site tool creates
and binds the jar-loaded remote browser session before delegating the objective to the profile. The
profile uses its current accessibility-snapshot browser tools.

The shipped `browser_profile` is not usable as-is for authenticated runs: its tool policy allows
`browser_exec`, and its system prompt actively directs the model to reach for it (shadow DOM,
iframes, fetching JSON endpoints). That conflicts with the no-`exec` boundary above and would steer
authenticated runs into a tool that must always fail. Nor does profile-local policy alone define the
boundary: `global_tools_policy` grants tools in a layer the profile's own policy cannot refuse, and
context providers inject household data into every profile's prompt by default.

The implementation therefore adds a static `authenticated_browser_profile` variant defined by its
effective surface, as `media_analyst` and `coder` already are: `exec` removed from both the tool
policy and the prompt, the globally granted tools withheld via `excluded_global_tools`, ambient
household context excluded via `excluded_context_providers`, and the remaining tool list reviewed
against the intended damage envelope. That is standard processing-profile configuration, not a
site-specific runtime or a dynamically generated worker.

### `browser_visual_profile`

The visual path keeps its current delegation mechanism and shared browser tab, through an
`authenticated_browser_visual_profile` variant carrying the same effective-surface exclusions as the
semantic variant: the shipped visual profile also receives globally granted tools and ambient
household context by default, and an authenticated run must not pair either with page-controlled
input. The native Gemini computer-use integration already provides:

- screenshot-level prompt-injection detection;
- `safety_decision=require_confirmation` on selected proposed actions;
- the existing Family Assistant confirmation callback;
- fail-closed handling when confirmation is unavailable or the decision is malformed.

Those controls are useful defence in depth. They do not create a comprehensive no-write guarantee,
and DOM-based actions performed by `browser_profile` do not automatically receive the same semantic
review.

### Session binding

The remote browser backend already keys ordinary sessions to a conversation. Authenticated-site
execution needs explicit plumbing so the delegated browser profiles use the newly created jar-loaded
session rather than opening a separate plain session. That binding should travel in trusted
execution context or backend state, not through model-visible arguments.

The browser session closes at the end of the delegated run unless the user takes human control. The
saved jar remains the only durable browser capability.

## Configuration model

Illustrative operator configuration:

```yaml
authenticated_sites:
  hellofresh:
    display_name: "HelloFresh"
    jar_id: "jar_0123456789abcdef0123456789abcdef"
    start_url: "https://www.hellofresh.com.au/menus"
    authenticated_origins:
      - "https://www.hellofresh.com.au"
    navigation_allowlist: []
    authorized_users:
      - "andrew"
    caller_profiles:
      - "default_assistant"
    browser_profile: "authenticated_browser_profile"
    visual_profile: "authenticated_browser_visual_profile"
    damage_envelope: >-
      The model may change ordinary meal selections and reversible preferences. It must not be
      treated as guaranteed unable to add extras, change plan settings, or create charges.
    mitigations:
      native_computer_use_safety: true
      action_review: "observe"
      postcondition_check: "hellofresh_account_summary"
```

The configuration is operator-controlled and not generated by the model. In particular:

- `site_id` is a stable configured token, not a jar label;
- `jar_id` is never exposed to either browser profile;
- the complete origin set is fixed before session creation;
- `authorized_users` binds the site to the household identities allowed to act on the bound account,
  and resolution fails closed for anyone else;
- caller profiles are explicit processing-profile IDs;
- browser and visual execution use existing static processing-profile IDs, and startup validation
  fails closed if a configured profile's effective surface violates the authenticated-run
  constraints — `exec` reachable, a globally granted tool not withheld, or ambient household context
  providers not excluded — so pointing a site at the shipped `browser_profile` is a configuration
  error, not a silent widening;
- the damage envelope is operator documentation, not a policy promise the runtime cannot enforce;
- mitigation settings describe best-effort review rather than a read/write allowlist.

Multiple household accounts on one site are separate site IDs, for example `hellofresh_andrew` and
`hellofresh_partner`, each bound to one jar.

## Product surface

### High-level tool

The caller profile receives one high-level tool:

```text
run_authenticated_site_task(
    site_id: str,
    objective: str,
) -> AuthenticatedSiteTaskResult
```

The tool does not accept a jar ID, arbitrary start URL, origin set, profile ID, adapter name,
browser permissions, or credential name. Those come only from trusted configuration.

Available `site_id` values are filtered by the active caller processing profile and by the acting
user against the site's configured `authorized_users`. The browser profiles themselves do not
receive this tool, so page content cannot recursively load a second authenticated session from
inside the site task.

A direct request to perform a configured site task carries enough user intent to run it without a
second generic "open saved login?" prompt. Configuration and profile policy are the standing grant.
A site may still opt into confirmation or action review when its damage envelope warrants it.

### Management surface

Saved-login creation, inspection, refresh, and deletion remain human management operations in
browser-server's authenticated UI for the first release. They are not general model tools.

Family Assistant may expose user-local status such as "HelloFresh login is stale" without returning
the full jar inventory or model-controlled labels. A trusted settings page remains the primary
management surface.

### Result contract and continued reasoning

The delegated browser profile should return typed data where a workflow permits it, for example:

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

A schema constrains shape, size, and auditability. It does **not** make copied page strings trusted.
Browser-derived labels, summaries, warnings, and evidence retain the same external provenance as the
page observations that produced them.

The result normally returns to the caller's existing model loop so the assistant can explain the
outcome, answer follow-up questions, or continue a user-requested multi-stage task.
Authenticated-site tools are not universally terminal.

Consequently, page-derived output may influence later reasoning by a caller profile with broader
tools. That risk is latent and orthogonal to the authenticated-session mechanism. The existing taint
tracker should preserve it, and the risk-adjudication design is the intended place to distinguish a
legitimate follow-up from an injected cross-capability instruction.

A profile or workflow may choose deterministic terminal rendering or a more restricted continuation
when warranted, but that is a policy option rather than the baseline architecture.

## Browser authority inside the session

### No generic read-only mode

A general authenticated browser profile is mutation-capable by design. `click`, `fill`, `select`,
`type`, `navigate`, and page-controlled JavaScript may all change account state. The implementation
must not claim that a proposal or inspection phase is technically read-only merely because the model
was instructed not to commit.

Blocking HTTP methods or maintaining lists of write endpoints is explicitly out of scope. Such a
system would both break legitimate sites and miss mutations expressed through unexpected routes,
background requests, or application state.

### Same-origin operation

Inside the configured origin set, the browser profile may continue responding to page content and
performing actions needed for the task. Runtime taint does not impose per-click confirmation inside
that already delegated session.

If HelloFresh places malicious instructions in its own UI, those instructions may influence the
HelloFresh session. As a security matter that is a compromise of the configured first-party site
within its own accepted damage envelope, not a cross-site authority escape.

The operator must consider what content a site renders. A mailbox or issue tracker can display
attacker-authored text under the service's own origin; granting general browser authority there may
allow that text to drive meaningful same-site actions. Same-origin confinement bounds the damage but
does not make the content benign.

### Agreements, submissions, and purchases

A general browser capability cannot guarantee that the model will not click an agreement, submit a
form, add a paid extra, change a plan, or otherwise act on the user's behalf. Those are model and UI
semantics, not properties browser-server can generally enforce.

Where practical, Family Assistant may ask for confirmation or run an action judge before an
apparently consequential action. The guarantee remains probabilistic. If the operator requires a
hard narrow operation, the model must use a deterministic adapter without simultaneous access to a
general mutation-capable browser path.

## Defence in depth

### Native Gemini computer-use authorization

When the task delegates to `browser_visual_profile`, Gemini's native computer-use protocol may mark
a proposed action with `safety_decision=require_confirmation`. Family Assistant already routes this
through the normal confirmation callback and refuses the action when approval is unavailable.

Prompt-injection detection is also enabled for that profile. These controls are useful, especially
for actions such as confirming a payment or accepting a consequential dialog, but they apply only
when the native visual action path emits the decision and must not be treated as complete coverage.

### Optional action-review judge

A later mitigation may evaluate:

- the trusted user objective;
- the configured site's damage envelope;
- the current snapshot or screenshot;
- recent browser actions;
- the next proposed action.

The output can be `allow`, `ask`, or `block`, using a provenance-shielded judge similar to the
risk-adjudication design. It should escalate to confirmation for obvious plan changes, purchases,
subscription changes, address changes, credential changes, or task-unrelated actions.

This judge is not a hard policy boundary. It can misunderstand the UI, miss autosave, or be
attacked. It exists to catch obvious misuse cheaply, not to replace the operator's damage-envelope
decision.

Native safety decisions cover only the visual delegation path; the primary semantic-snapshot path
has no action review beyond model adherence. For the HelloFresh envelope that is accepted. Before a
site with a materially larger envelope is configured, an observe-mode judge on the DOM path stops
being optional.

### Postcondition checks

A site may define a cheap before/after check such as:

- total price;
- selected box size;
- delivery address;
- subscription status;
- number and type of extras;
- site-reported completion state.

These checks improve detection and recovery. Unless implemented through a narrow deterministic
adapter, they do not prove that no transient or hidden mutation occurred.

### Deterministic adapters

A site-specific adapter is optional and justified only when the value of a hard narrow operation
exceeds the maintenance cost. Its model-facing interface contains structured domain arguments rather
than arbitrary URLs, selectors, methods, or request bodies.

An adapter can enforce a property such as "change only these meal IDs and do not alter box size or
price" only if the model cannot bypass it through a simultaneous general browser path. The design
does not require an adapter for every site and explicitly rejects maintaining fine-grained endpoint
lists for general browsing.

### Audit and recovery

Each run records enough metadata to inspect what happened without recording secrets:

- acting user and caller processing profile;
- site ID and configuration version;
- jar generation and effective origin set;
- browser and visual profile IDs and model versions;
- start and end time;
- major actions and typed result;
- safety confirmations or judge decisions;
- optional before/after checks;
- final status and failure reason;
- optional final screenshot or sanitized evidence.

For reversible sites, execute-and-report is preferable to routine pre-action confirmation. The user
should be able to inspect the result, revoke the jar, and undo site-local changes through the site's
normal controls.

## Relationship to taint and provenance

Runtime taint remains useful and is deliberately orthogonal to whether the browser session may act
on its own origin.

### Intra-session

Browser observations are external input, but the browser profile is expected to continue operating
inside the already delegated origin set. Turning every same-origin action into a taint gate would
recreate the unusable confirm-everything policy this design replaces.

### Return to the caller

Browser results retain external provenance when returned to a broader caller profile. Typed fields
remain untrusted strings. The current `LLMLoop` may continue with the caller's ordinary tools, so an
injected result can attempt to influence later cross-capability actions.

That is a genuine risk, but it is not specific to authenticated sessions: ordinary web search,
email, documents, notes, and tool outputs already create the same transition. Runtime taint and the
risk-adjudication design are the appropriate shared control. Until enforcement is enabled, this
remains an accepted residual rather than a reason to make every browser tool terminal.

### Durable artifacts

Content derived from a site does not become trusted ambient instruction merely because it is written
to a note, task, or other artifact. Existing provenance propagation and ambient prompt-admission
controls remain the chokepoints for that separate concern.

## Login lifecycle

### Initial provisioning

1. A human opens a browser-server session.
2. The human signs in under exclusive browser control and completes MFA or SSO.
3. The human saves the login as a cookie jar.
4. The operator binds the jar to an authenticated-site configuration entry and documents the damage
   envelope.
5. The configured caller profiles may use it under their normal tool policy.

### Normal task

1. A caller profile invokes `run_authenticated_site_task` with a configured `site_id` and objective.
2. The tool creates a fresh browser-server session from the configured jar with confinement enabled.
3. It navigates to the configured start URL.
4. The objective is delegated through the existing processing-profile system to
   `authenticated_browser_profile`.
5. The browser profile may delegate visual steps to `authenticated_browser_visual_profile` using the
   shared session.
6. The result returns with browser provenance preserved.
7. The session closes unless a human handoff is active.

### Expired login

1. A probe or task detects that the jar is stale.
2. The tool returns `login_required` with the site name and a trusted handoff action.
3. A human signs in and refreshes the same jar in place.
4. The task may be retried from its original objective.

The agent does not invalidate a jar solely because a page claims the session expired. Independent
probing or explicit human action remains the source of truth.

### Later autonomous refresh

If measured expiry friction justifies it, browser-server may perform the Keychute-backed first-party
credential fill described in [PR #1069](https://github.com/werdnum/family-assistant/pull/1069). The
flow refreshes the configured jar; it does not expose credentials to either browser profile. SSO and
unusual MFA remain human handoff paths.

## Failure behavior

The high-level tool returns a small set of actionable outcomes:

- `completed`: the browser profile reported task completion;
- `login_required`: the saved session is stale or missing;
- `blocked_by_scope`: browser-server blocked an unconfigured origin transition;
- `review_blocked`: a native safety decision or optional judge refused an action;
- `needs_human`: CAPTCHA, unsupported MFA, SSO, bot detection, or ambiguous workflow;
- `site_changed`: expected site structure or completion evidence no longer matches;
- `failed`: browser or delegated-profile failure with no claimed completion.

A failure does not cause the browser profile to receive jar-management or credential tools. It may
ask the user for help through the normal caller response or human-handoff flow.

## First vertical slice: HelloFresh meal selection

The first release target is:

> "Pick our HelloFresh meals for next week" completes end to end from a manually saved login, with
> no routine jar-load confirmation and no direct authority outside the configured HelloFresh
> session.

### Initial risk posture

HelloFresh begins as a **general authenticated browser capability**, not as a purported read-only or
bounded-commit browser.

The operator accepts that the browser model may exercise any action reachable on the configured
HelloFresh origins. The first implementation should make obvious unwanted actions less likely with:

- `browser_visual_profile`'s native prompt-injection detection and safety decisions when the visual
  path is used;
- explicit user confirmation when the model or an optional reviewer identifies a consequential
  action;
- an action summary naming selected meals and other detected account changes;
- cheap before/after checks for price, box size, extras, delivery address, and subscription state
  where the site exposes them reliably;
- immediate visibility and ordinary site-level undo/recovery.

None of those is represented as a hard "no additional spending" guarantee. If experience shows that
the reachable account authority is unacceptable, the choices are to build a maintained narrow
HelloFresh adapter or keep the task human-operated.

### Acceptance criteria

01. A human can save and bind one HelloFresh login.
02. The tool is available only to configured caller processing profiles, and only the site's
    configured authorized users can act on the bound account.
03. Family Assistant creates a fresh confined browser session from the bound jar.
04. The task runs through the authenticated browser profile variants, whose effective surface
    excludes `exec`, globally granted tools, and ambient household context.
05. It can select and save ordinary meal choices without asking for jar-load confirmation.
06. Neither browser profile can list or choose another jar, call Keychute, or invoke unrelated
    household tools.
07. Browser-server blocks top-level navigation and forms outside the configured origin set.
08. The result names the meals selected and retains external/browser provenance when returned to the
    caller.
09. A stale session produces one clear human reauthentication workflow.
10. Undesirable meal selection and other same-site mistakes are accepted residuals and are visible
    in the action report.
11. Optional action review and postcondition checks can be enabled without claiming complete write
    prevention.
12. The end-to-end task succeeds while runtime taint remains in observe mode.

## Implementation plan

### M1 — Configuration and backend session creation

- Add `authenticated_sites` operator configuration and validation.
- Resolve configured site IDs to jar, start URL, origin set, authorized users, caller profile IDs,
  browser profile ID, visual profile ID, damage-envelope text, and mitigation settings.
- Add the minimal `RemoteBrowserBackend` jar methods needed to load a known jar into a fresh session
  and probe its status.
- Never send `confine_navigation: false` or `allow_exec: true` for jar-loaded sessions.
- Verify the created session's jar generation and origin metadata against trusted configuration.

### M2 — High-level tool and processing-profile wiring

- Add `run_authenticated_site_task` through the existing local tool registry.
- Grant it only to configured caller profiles through the ordinary tool-policy system, and enforce
  the site's `authorized_users` check fail-closed before session creation.
- Add the `authenticated_browser_profile` and `authenticated_browser_visual_profile` variants: no
  `exec` in policy or prompt, globally granted tools withheld via `excluded_global_tools`, ambient
  context providers excluded via `excluded_context_providers`, remaining tools reviewed against the
  envelope. Authenticated runs delegate to them rather than the shipped profiles, and startup
  validation rejects a site configuration naming a profile that violates these constraints.
- Bind the created jar-loaded browser session into the delegated execution context.
- Delegate the objective to the authenticated browser profile.
- Preserve the existing shared-session delegation mechanism for the visual variant.
- Ensure neither browser profile receives jar-management, credential, or recursive
  authenticated-site tools.
- Preserve browser result provenance when the result returns to the caller.
- Close the browser session on completion and failure paths.

### M3 — Human provisioning and stale-session recovery

- Use browser-server's existing human sign-in and saved-login UI for initial provisioning.
- Add a trusted operator mapping from saved jar to site configuration.
- Surface `login_required` without exposing the full jar inventory to the model.
- Support human refresh of the same jar ID and retry from the original objective.
- Resolve browser-server refresh/UI gaps only as required by this flow.

### M4 — HelloFresh end-to-end workflow

- Add profile guidance or a site skill for reading menus, history, and household constraints.
- Exercise the real workflow through browser-server, including bot detection and session lifetime.
- Measure reliability, interventions, same-site mutations, and missing browser primitives —
  including where origin confinement or the absence of `exec` blocks a legitimate workflow step,
  since tighter-than-market confinement is a capability trade-off to evaluate from experience.
- Add action summaries and cheap postcondition checks where reliable.
- Document the actual accepted damage envelope from production use.

### M5 — Optional semantic review

- Evaluate whether Gemini's native computer-use safety decisions cover the useful visual path.
- If needed, prototype an action-review judge modelled on risk adjudication.
- Run it in observe mode first and measure false positives before enabling confirmation or blocking.
- Do not introduce per-site endpoint or selector allowlists as a prerequisite.

### M6 — Measured follow-ups

Only after the vertical slice is in regular use:

- add scheduled execution under a dedicated existing processing profile;
- add Keychute-backed jar refresh if expiry is a recurring burden;
- add more sites after an explicit damage-envelope review, which must revisit whether an
  observe-mode DOM-path action judge is a prerequisite for that envelope;
- build a narrow deterministic adapter only for a workflow whose hard guarantee justifies ongoing
  maintenance;
- improve cross-capability taint adjudication without making it a release gate for same-site browser
  work.

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
- runtime taint enforcement as a prerequisite for authenticated browsing;
- per-click or per-submit same-origin escalation;
- any implication that generic browser automation can be made reliably read-only.

### PR #1018

The backend reconciliation in [PR #1018](https://github.com/werdnum/family-assistant/pull/1018)
remains useful, especially its notes on shipped browser-server semantics. Its generic jar-management
product surface and work ordering are replaced by the configured-site tool and existing-profile
delegation described here.

### PR #1069

The narrowed Keychute design in [PR #1069](https://github.com/werdnum/family-assistant/pull/1069)
remains valid. Agent-driven login is a jar-refresh path, not a live-session transfer. It moves
behind the first useful saved-session workflow and feeds configured browser sessions rather than
exposing credentials to a model.

### Runtime taint designs

Keep provenance collection, ambient prompt admission, diagnostics, and risk-adjudicated enforcement.
Do not require deployment-wide enforcement before authenticated-site capabilities ship.

Browser output returning to a broader caller is a real taint transition. It is shared with ordinary
web, email, document, and tool-output flows and should be solved once in the taint/adjudication
system, not by making this one tool terminal.

## Security properties

The hard design is successful when all of these are true:

- The model cannot read cookie or origin-storage values through normal browser APIs.
- Jar-loaded sessions expose no arbitrary page evaluation.
- Only a site's configured authorized users can act on its bound account.
- The browser profiles cannot inspect or choose arbitrary saved jars.
- A configured jar loads only into a fresh browser-server context.
- The session's authenticated origin set is immutable and visible to trusted orchestration code.
- Top-level navigation and forms are confined to configured origins.
- The browser profiles have no direct credential, jar-management, unrelated household, messaging,
  code-execution, or recursive authenticated-site tools on their effective surface, globally granted
  tools included.
- Authenticated browser runs receive no ambient household context in their prompts.
- Human control remains exclusive during credential and MFA entry.
- Revoking a jar terminates live sessions using it.
- A direct user request does not incur redundant jar-load confirmation.
- Browser-derived results preserve external provenance when they return to a broader caller.

The following are deliberately **not** security properties of a general browser capability:

- that the model will follow the user's objective;
- that it will not agree, submit, purchase, cancel, or change preferences;
- that page content cannot influence its same-site behavior;
- that typed results cannot influence later caller reasoning;
- that a semantic judge will identify every consequential action.

## Accepted residuals

- A configured browser profile can perform any same-origin action reachable through the account and
  browser UI. Enabling the site asserts that this damage envelope is acceptable.
- Same-origin content may include attacker-authored third-party material. Origin confinement bounds
  where it can act; it does not make the content trustworthy.
- Browser results may influence later actions by a broader caller profile. Taint/adjudication is the
  shared intended control and remains observe-only in the current deployment.
- Native Gemini safety decisions and any later action judge may miss dangerous actions or request
  unnecessary confirmations.
- Same-origin page JavaScript may make subresource requests not covered by top-level navigation
  confinement. Credentials remain origin-scoped and `exec` is unavailable in jar-loaded sessions.
- A future autofill path necessarily exposes the entered credential to JavaScript running on the
  approved login origin, as any password manager does.
- Browser automation may break when a site changes. Failure must be visible and must not claim
  completion without evidence.
- Some sites will block automated or cloud browsers and remain human-operated.

## Review questions

1. Is the high-level `run_authenticated_site_task` surface preferable to generic model-facing jar
   management?
2. Does the bounded-damage principle state the real trust assumption clearly enough?
3. Can the feature be implemented by binding a jar-loaded session into the existing
   `browser_profile` and `browser_visual_profile` delegation path without a new runtime abstraction?
4. Should the default caller grant be limited to `default_assistant`, with scheduled use getting a
   dedicated static processing profile later?
5. Which hard browser-server boundaries are still missing for the first HelloFresh slice?
6. Is native Gemini computer-use safety sufficient initial defence in depth, or is an observe-only
   action-review judge worth prototyping immediately?
7. Which HelloFresh postcondition checks are cheap and stable enough to provide useful detection
   without becoming a brittle endpoint-maintenance project?
8. Are there any configured sites whose same-origin third-party content makes the accepted damage
   envelope unexpectedly large?
9. Does the current taint tracker preserve browser-result provenance across delegated-profile return
   correctly, or is plumbing required before the result can safely participate in later
   adjudication?

## Validation plan

This is a design-only change. Before implementation:

- review against browser-server's shipped jar API and confinement semantics;
- review against Family Assistant's existing profile, delegation, confirmation, and browser-session
  architecture;
- verify the authenticated profile variants' *effective* tool surface (global grants included) and
  injected context, and remove any direct cross-capability path from authenticated runs;
- verify that a delegated profile can be bound to the intended jar-loaded remote session without
  model-visible session identifiers;
- threat-model the first HelloFresh configuration from the actual reachable account UI;
- document the operator-accepted damage envelope before enabling the site;
- test native computer-use safety decisions and any optional judge in observe mode;
- add end-to-end tests for jar opacity, origin confinement, human-control exclusivity, stale login,
  session cleanup, revocation, result provenance, and inability of browser profiles to acquire a
  second authenticated session.

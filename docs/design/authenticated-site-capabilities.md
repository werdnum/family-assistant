# Bounded Authenticated-Site Capabilities

## Status

Accepted — implemented for M1–M3 and the autofill section.

This design supersedes the **Family Assistant policy and product surface** in
[browser-cookie-jars.md](browser-cookie-jars.md) and the implementation direction in
[PR #1018](https://github.com/werdnum/family-assistant/pull/1018). It does not replace
browser-server's cookie-jar mechanism. It also now contains the **Keychute credential autofill**
design (its own section below) as the second, per-site login-acquisition path of the same capability
— folded in from [PR #1069](https://github.com/werdnum/family-assistant/pull/1069), superseding the
[PR #833](https://github.com/werdnum/family-assistant/pull/833) credential-broker draft and the
separate `agentic-credential-autofill.md` that earlier revisions of PR #1069 carried.

## Decision

Authenticated browsing is exposed as an operator-configured, bounded **site capability**, not as
general model-facing cookie-jar management.

The implementation uses Family Assistant's existing processing-profile and delegation system:

- a trusted profile such as `default_assistant` invokes a high-level authenticated-site tool;
- the tool creates a fresh browser-server session from the configured jar;
- the task runs under static authenticated variants of the existing `browser_profile` and
  `browser_visual_profile`, sharing the same browser session through the existing delegation path;
- the browser profiles receive the normal browser tools, but no jar-selection or
  credential-management tools (the one credential-adjacent addition is `browser_autofill` — see the
  autofill section — which exposes no credential material and whose every use is a Keychute release
  decision);
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
- obtain a Keychute release that no policy row or human approved (it may *request* a fill by alias
  through `browser_autofill`; Keychute decides — autofill section);
- invoke Gmail, Calendar, Notes, Home Assistant, messaging, code execution, task management, or
  other household capabilities directly;
- delegate to any processing profile other than the site's configured visual profile;
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
  worker sees the objective and task-scoped facts, not the household's data. Delegation is pinned
  the same way: `delegate_to_service` is allowed only toward the site's configured visual profile
  via an argument-level rule, because an open delegation grant reaches household-capable profiles
  indirectly and would undo everything else on this list. The admissible tool list itself is
  mechanical, not a judgment call: validation admits only browser-server-mediated tools plus the
  pinned delegation, rejecting fail closed any tool that reaches the network on its own — the
  shipped profile's UCP shopping tools, which act on a model-supplied business URL outside
  browser-server's confinement, are the standing example.
- **No recursive site acquisition:** `run_authenticated_site_task` is not available to the browser
  profiles.
- **No `exec` in jar-loaded sessions:** arbitrary page evaluation reads non-HttpOnly cookies and
  origin storage, which would collapse the opaque-session-state boundary. A leaked session token is
  not same-site damage: it can be replayed from attacker infrastructure, outside every mitigation,
  audit, and confinement path, until revocation. Jar-loaded sessions therefore never expose `exec`;
  a workflow that needs scripted page access is the deterministic-adapter path, not an opt-in.
- **Exclusive human control:** snapshots and commands fail while the human owns the browser, and
  they fail closed. The shipped backend transparently replaces a lost-lease or expired session with
  a fresh one on navigate so ordinary conversations are never wedged; for an authenticated-site run
  — jar-bound or jarless — that recovery would be an unconfined escape hatch opened mid-handoff by
  page-controlled input. The backend chokepoint therefore marks every authenticated-site session,
  and never re-provisions one on its own: a lost lease or expired session fails closed, and recovery
  is handback or the run ending, never a fresh unconfined session.
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
household context excluded via `excluded_context_providers`, delegation restricted at argument level
to the site's configured visual profile, and the tool list reduced to the mechanically defined
browser-server-mediated set. "Reviewed" is not the mechanism: the shipped profile's UCP shopping
tools act on a model-supplied business URL through service-side requests outside browser-server's
confinement, and validation rejects them — like any other tool that reaches the network on its own —
fail closed. That is standard processing-profile configuration, not a site-specific runtime or a
dynamically generated worker.

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

The remote browser backend already keys ordinary sessions to a conversation. That key is not the
binding mechanism here: a caller can emit concurrent tool calls, and two authenticated runs sharing
one conversation-keyed slot could overwrite each other's jar-loaded session or tear down the other's
on cleanup. The binding is therefore keyed to the individual run and propagated through both
delegation hops in trusted execution context, not through model-visible arguments or the
conversation-keyed registry. The first release additionally serializes authenticated runs within a
conversation — a second concurrent invocation waits or fails cleanly rather than racing.

Authenticated delegation may hand off to the background as usual. Browser tasks are exactly the
long-running workload the async delegation system exists for, and forcing the caller's loop to block
inline for a whole browser run would defeat it. What the handoff must carry is **session
ownership**: the jar-loaded session belongs to the delegated run, not to the high-level tool call.
Ownership does not fan out with nested delegation: the semantic-to-visual hop executes inline within
the semantic worker's run — async handoff is disabled for that hop, since the worker needs the
visual result to continue and the outer run is already backgroundable — so exactly one run owns the
session for its whole lifetime and no backgrounded child can outlive its parent's cleanup. A run
that completes within the inline window returns its result and the session closes; a run that hands
off to the background takes the session with it, the caller receives a typed `running` result
carrying the run's opaque handle, and the run closes the session when it reaches a terminal state —
unless that terminal state is a resumable parked outcome (`handoff_pending`, `approval_pending`), in
which case the session parks until the resume handle is consumed or the bounded park window or
lifetime backstop expires. The background completion machinery persists only text and attachments
and its notification is advisory, so the typed `AuthenticatedSiteTaskResult` — any resume handle
included — is persisted durably on the delegation run's record at terminal state, wherever the run
executed; the caller retrieves it by presenting the run's opaque handle back to the high-level tool,
never by reconstructing it from notification text. The terminal row is what wakes a waiting caller,
so the session is settled and that result persisted **before** the row becomes terminal — otherwise
a caller woken by the row reads a run that has already finished or parked as still `running`, and a
parked session's resume handle is not there to read. Exactly one owner closes the session. An
idle/maximum-lifetime backstop reclaims a session whose owning run dies without reaching a terminal
state, and jar revocation still terminates the session immediately regardless of owner.

Across a human handoff, the durable object is the session, not the run. A backgrounded run has no
input channel, so the design does not pretend the worker can wait live for the handback token: when
the worker requests takeover it shares the one-time link and ends in a resumable terminal state, and
the session parks under exclusive human control — still origin-confined, still fail-closed for agent
commands, still subject to revocation and the lifetime backstop. Delegation already supports
resuming a terminal run for the same caller; after handback, a follow-up invocation resumes that run
and rebinds the parked session. Resumption follows the existing sanitized-recovery semantics: the
human-controlled page is closed and a fresh page opens at the approved origin after redaction
checks, so the authenticated session and the worker's context survive the handoff while exact page
and in-progress form state do not. Losing mid-form progress to a challenge is accepted as reasonable
behaviour for reversible household sites; a workflow that needs same-page resumption is a future
explicit policy, not the default. The handback token that reclaims the lease is minted by
browser-server only when the human finishes, so it cannot ride in the resume handle and must not
ride in the conversation: it is exchanged server-side between browser-server and Family Assistant's
trusted orchestration, bound to the parked session's record — whether by callback or by polling the
session's handover state is construction detail for the implementing PR — so that consuming the
resume handle finds the lease already reclaimable. `needs_human` remains the fully terminal outcome
for steps a human cannot unblock mid-session (an SSO redirect out of the confined origin set, hard
bot blocks, a bad password on an autofill site), where the human path depends on the acquisition
path — refresh the jar where one exists, correct the Keychute secret for an autofill-only site — and
then retry from the original objective. A challenge the human *can* complete in the parked session —
an MFA code, a captcha — is `handoff_pending`, not this; the one rule is stated in the autofill
section's bounded-retries boundary and applied everywhere.

The browser session closes when its owning run ends, unless the run ended in a resumable parked
outcome — `handoff_pending` (the user has taken human control) or `approval_pending` (a Keychute
decision is outstanding) — in which case it parks, still origin-confined and fail-closed for agent
commands, until resumption or expiry. The saved jar remains the only durable browser capability.

## Configuration model

Illustrative operator configuration:

```yaml
authenticated_sites:
  hellofresh:
    display_name: "HelloFresh"
    jar_id: "jar_0123456789abcdef0123456789abcdef"   # optional when credential_alias is set (autofill section)
    start_url: "https://www.hellofresh.com.au/menus"
    authenticated_origins:
      - "https://www.hellofresh.com.au"
    navigation_allowlist: []
    credential_alias: "hellofresh"  # optional routing hint: a Keychute secret exists for this site;
                                    # which secret, where it may be filled, and with what approval
                                    # is decided by the Keychute policy row, not here
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
- `credential_alias` is routing, not release authority: it tells the tool that a probe-stale or
  absent jar (never a revoked one — see Expired login) should lead to a jarless session and a login
  attempt rather than `login_required`, and it **pins** the one secret this site's runs may ask for
  — trusted orchestration passes it to browser-server at session creation, `browser_autofill` takes
  no alias argument, and a site without one has no autofill at all. Whether that secret is released,
  for which page origins, and with what approval outcome lives entirely in the Keychute policy row
  for it (autofill section);
- the complete *effective* origin set is fixed before session creation: browser-server confines
  navigation to the jar's origins plus any saved `jar_nav_allowlist` (an SSO origin, say), so
  session creation compares that full set against the site configuration and rejects a jar whose
  saved allowlist reaches origins the configuration does not declare;
- `authorized_users` binds the site to the household identities allowed to act on the bound account,
  and resolution fails closed for anyone else;
- caller profiles are explicit processing-profile IDs;
- browser and visual execution use existing static processing-profile IDs, and startup validation
  fails closed if a configured profile's effective surface violates the authenticated-run
  constraints — `exec` reachable, a globally granted tool not withheld, delegation reachable beyond
  the site's configured visual profile, a tool outside the mechanically defined
  browser-server-mediated set granted, or ambient household context providers not excluded — so
  pointing a site at the shipped `browser_profile` is a configuration error, not a silent widening;
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
    resume: str | None = None,
) -> AuthenticatedSiteTaskResult
```

The tool does not accept a jar ID, arbitrary start URL, origin set, profile ID, adapter name,
browser permissions, or credential name. Those come only from trusted configuration. The one
additional input is `resume`, an opaque handle minted by a previous invocation's `handoff_pending`,
`approval_pending`, or `running` result. Presenting a parked run's handle resumes it — the run
continues its own objective, and a `site_id` that does not match the parked run's site fails closed;
presenting a completed background run's handle returns its stored typed result. The handle names a
prior run, is resolved and authorized server-side under the existing same-caller resume rules, and
grants nothing the caller did not already hold — the model never assembles delegation IDs, session
IDs, or handback tokens from free text. A handle is not a durable grant: every resume re-resolves
the current site configuration and re-enforces `authorized_users` and `caller_profiles`, and
withdrawn authorization closes the parked session rather than rebinding it.

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

This judge is no longer purely hypothetical. The shipped tool-call reviewer prompt and structured
verdict contract were evaluated on Gemini 3.7 Flash over 2,654 adapted public browser-injection
cases ([2026-08-31 report](../development/eval-results/2026-08-31-gemini-3.7-flash/README.md)): all
1,312 attack trials denied, all 1,342 benign trials allowed with zero friction, and verdicts
invariant to exposing the quarantined attack text. The honest bound is weaker than the raw counts:
the attacks collapse to 62 independent evidence units, supporting only a ~4.84% 95% upper bound on
false allows — inconclusive against a 1% target. That is measured, promising defence in depth with
no observed workflow cost, not a proven barrier.

The judge is still not a hard policy boundary. It can misunderstand the UI, miss autosave, or be
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
normal controls. Revoking the jar is a real kill switch: it terminates live sessions loaded from it,
and for an autofill site it also disables autofill until a human deliberately re-provisions (Expired
login); the Keychute standing row is the separate kill switch for the credential itself.

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

For a jar-backed site:

1. A human opens a browser-server session.
2. The human signs in under exclusive browser control and completes MFA or SSO.
3. The human saves the login as a cookie jar.
4. The operator binds the jar to an authenticated-site configuration entry and documents the damage
   envelope.
5. The configured caller profiles may use it under their normal tool policy.

For an autofill-backed site (first-party password login — see the autofill section), no human login
happens at all: the operator stores the password in Keychute, sets `credential_alias` on the site
entry, and the first run logs itself in — the one human touch is Keychute's approval of the first
release, which is also where the operator creates the standing policy row (secret, page origin,
notify-only) that makes later releases silent.

### Normal task

1. A caller profile invokes `run_authenticated_site_task` with a configured `site_id` and objective.
2. The tool creates a fresh browser-server session from the configured jar with confinement enabled.
3. It navigates to the configured start URL.
4. The objective is delegated through the existing processing-profile system to
   `authenticated_browser_profile`.
5. The browser profile may delegate visual steps to `authenticated_browser_visual_profile` using the
   shared session.
6. The result returns with browser provenance preserved.
7. The session closes unless the run parked in a resumable outcome (`handoff_pending`,
   `approval_pending`).

### Expired login

For a site **without** `credential_alias`:

1. A probe or task detects that the jar is stale.
2. The tool returns `login_required` with the site name and a trusted handoff action.
3. A human signs in and refreshes the same jar in place.
4. The task may be retried from its original objective.

For a site **with** `credential_alias` (the autofill section), the routing depends on *why* there is
no usable jar, and the jar mechanism already tells the two cases apart:

- **Probe-detected or in-use expiry** (the session simply lapsed): the run does not take the
  `login_required` exit — that exit would wait for a human before any delegated session exists, and
  `browser_autofill` lives only inside a session. Instead the tool creates the session **jarless**
  (stale state is not worth carrying), with the same confinement supplied explicitly from the
  complete configured origin set (`authenticated_origins` plus `navigation_allowlist`), and the run
  begins at the login form. A site whose entry names no jar at all is the same path.
- **Explicit revocation** (a human invalidated or deleted the jar — browser-server's
  `invalidated_at` and tombstone, as distinct from a probe result): the kill switch must mean what
  it says. A run that could silently log back in through the standing Keychute grant would defeat
  the recovery promise that a user can revoke a jar after an unwanted action. So a revoked jar
  **disables autofill for that site** as well: the run returns `login_required`, and the capability
  re-enables only by a deliberate human act — saving a fresh jar, or refreshing the invalidated one
  through the human login flow. The credential itself has its own kill switch in Keychute — expiring
  or revoking the standing row — which is the right lever when the concern is the password rather
  than the session. If the login raises a challenge a human can complete in the live session (an MFA
  code, a captcha), the run takes the existing `handoff_pending` path — the human finishes it in the
  parked session and handback resumes the task; `needs_human` is the exit only for what no one can
  finish mid-session — a bad password, an SSO redirect out of scope, a hard bot block.

The agent does not invalidate a jar solely because a page claims the session expired. Independent
probing or explicit human action remains the source of truth.

## Keychute credential autofill

The second login-acquisition path, folded in from
[PR #1069](https://github.com/werdnum/family-assistant/pull/1069) (which supersedes the
[PR #833](https://github.com/werdnum/family-assistant/pull/833) credential-broker draft): when a run
on a configured site hits a login form — expired session, password re-prompt, a site whose sessions
never persist — the agent can have the operator's stored password filled into that form and continue
the task, with the value never entering LLM context, tool results, message history, or logs.

**Jars and autofill are per-site alternatives, not a sequence.** A site entry may carry `jar_id`,
`credential_alias`, or both. A jar is the only path for SSO and MFA-per-login sites — a human
performs that login and the jar persists it — and the quieter path everywhere else: a persisted
session cookie is how real browsers behave, where a fresh password login from datacenter automation
on every task is exactly the shape bot defenses, step-up challenges, and "new sign-in" alerts are
tuned to catch, and it puts a Keychute release plus the flakiest page on the site into every task's
critical path. Autofill is the only path for a site whose sessions never persist, removes the human
provisioning step entirely for first-party-password sites, and repairs expiry without a human. Which
combination a site gets is an operator choice per entry; autofill-only for a password site is a
legitimate configuration, and running one such site that way is the cheap experiment that answers
the fresh-login-tolerance question with data.

### Three risks, separated

1. **Giving a site its own password is the intended operation** — not a risk to design against.
2. **The password reaching the model transcript, tool results, or logs is a credential leak**,
   prevented mechanically: Keychute's delivery model plus the read-back protections below.
3. **The authenticated agent doing something unwanted inside the already-authorized account is the
   bounded-damage risk this document already accepts.** It does not justify a login-only principal
   whose authority ends at login, a terminal login session, or a post-fill lock — earlier revisions
   of PR #1069 built exactly that, and it was architecture-generated complexity: the very next
   useful operation after login is operating the account, which the operator authorized when they
   enabled the site. Autofill is therefore a primitive on the ordinary authenticated session, and
   the same agent continues the task afterwards.

What Keychute contributes as-is (no server changes): approval UX, standing grants, idempotent
request/wait, origin constraints, single-use grant reads, non-secret grant metadata, and audit.
browser-server registers as the Keychute `trusted-client` (`mechanisms: [autofill]`) and receives
the credential bytes directly; Family Assistant requests the operation and receives status and
metadata, never plaintext.

### Where authorization lives

**Release authority lives in Keychute, not in Family Assistant configuration.** A Keychute policy
row already encodes exactly the decision that matters — this secret, to client `browser-server`,
mechanism `autofill`, for these page origins, with this outcome (auto-approve, notify-only, require
approval, deny) until this expiry — and it is shown on the approval page, audited, and adjustable
without an FA redeploy. Duplicating any of that into the site entry (a bound secret name, a
fill-origin list) would create a second source of truth that drifts. So the site entry carries only
`credential_alias`: a routing hint (an absent or probe-stale jar — never a revoked one — leads to a
jarless session and a login attempt rather than `login_required`) and the default alias the browser
profile asks for. It authorizes nothing.

**Each request is tied to the actual page origin.** `browser_autofill` makes browser-server create
the Keychute access request with page origins = the origin of the document actually on screen — not
a configured set. A standing row that covers that origin releases silently or with a notify-only
push; anything else lands on the approval page showing the real origin beside the secret name, where
a prompt-injected page's request for `bank-login` on a HelloFresh origin is visible for what it is
and denied. Keychute's per-client pending caps bound the spam that a persistent injection could
generate. The operator draws the line between "origins this session may navigate" and "origins that
may receive this password" on the row, where it belongs; an auxiliary or SSO origin in the
confinement set receives the password only if the row says so.

**There is no credential-enabled session type.** The read-back protections below are uniform across
every authenticated-site session, and a jarless session — confinement supplied explicitly from the
complete configured origin set (`authenticated_origins` plus `navigation_allowlist`), validated by
browser-server, confined identically to a jar-loaded one, and under the same never-re-provision rule
— can be created for any site whose entry sets `credential_alias`. The one thing bound at session
creation is *which* alias this run may request: trusted orchestration pins the site entry's alias
onto the session, `browser_autofill` takes no alias argument, and a session for a site with no alias
has no autofill. That pin is routing, not release authority — but it is load-bearing. Two household
accounts on one origin are two site ids with two secrets, each with its own standing row for the
*same* origin, and Keychute cannot tell sites or acting users apart; without the pin, a
page-influenced model in a run authorized for account A could name account B's alias and have B's
row release silently, bypassing the site's `authorized_users`. There is still no fill-time
transition and nothing to validate at startup; a site whose alias has no policy row simply gets
`approval_pending` on first use, and Keychute fails closed.

Credential identifiers need **authorization, not secrecy**: the alias and site id are ordinary
model-visible data, and a household with two accounts on one site disambiguates by site id, which
pins the alias. What the model cannot do is request any alias but the run's, obtain a release that
neither a policy row nor a human approved, browse the Keychute store, or fill outside the granted
origins — the pin, Keychute, and browser-server enforce that, not identifier hiding.

### The tool

```text
browser_autofill(field_refs?) ->
    filled | approval_pending | refused(reason)
```

A browser-server primitive (`POST /v1/sessions/{id}/autofill`, service-token auth) surfaced to the
authenticated browser profiles. It **fills — it is not a login engine**: the agent navigates to the
login form, requests the fill, clicks Sign in, inspects the (redacted) result, and continues.
Multi-page (username-first) logins happen in steps — one call fills the identifier, the agent clicks
Continue, a second call fills the password. The account email is usually legitimate model-visible
data already; nothing forces the sequence into one atomic deterministic operation.

- `filled`: eligible field(s) on the checked page were filled; metadata names field kinds, never
  values.
- `approval_pending`: Keychute needs an operator decision. browser-server long-polls the wait
  endpoint within the tool-call budget; an approval that outlasts the run parks the session under
  the `handoff_pending` pattern (Session binding above) — the typed resume handle retries the same
  per-step request against the same session, with the fill's target re-validation still applying. A
  lapsed park window means a fresh session and request, possibly a fresh approval: the cost of a
  very slow first approval, moot once the standing grant exists.
- `refused(reason)`: wrong origin, no eligible field, target invalidated by navigation, policy
  denial, no alias pinned to this session, or a bad-password outcome already recorded in this
  session.

Keychute caps releasing-tier grants at one read, so the unit of release is the **fill step**: each
call creates its own access request (page origins = the actual origin of the checked document;
context passed through from FA: site, objective snippet, acting user, step) and reads that request's
single-use grant. Because the request names the real destination, the row's origin constraint
decides each fill on its own merits — an origin the session may *navigate* is not thereby an origin
the fill may *target*, and no session-wide origin set is ever requested. The per-step idempotency
key is reused only across `approval_pending` retries of the same step. Under a standing grant a
two-step login is two auto-approved or notify-only releases seconds apart; the first release per
site gets Keychute's approval page. Every release is audited with the `secret_version_id` decrypted.

### Load-bearing boundaries

Enforced in browser-server, the component that owns the page:

1. **Check the real destination.** The model saying `origin=hellofresh.com` is not evidence: at fill
   time the actual target document's origin is verified against the constraints on the **granted**
   capability (Keychute exposes grant metadata because an approval may narrow the request),
   including after an approval wait — and that granted set derives from the policy row and the
   request's actual origin, never from the session's wider confinement set. Main-frame-only fills in
   V1 — no iframes.
2. **Bind the fill to the checked page and element.** Resolve, validate, and fill as one serialized
   operation; a navigation or document replacement in between — the site can navigate itself while
   an approval is pending — fails the fill rather than filling whatever is there now.
3. **Element sanity checks, honestly labelled.** Password only into `input[type=password]`; explicit
   `autocomplete=new-password`/confirm fields rejected; identifier into a text/email/tel input,
   `autocomplete=username` preferred. Cheap and worthwhile — not proof the approved site cannot read
   its own field, and heuristic ambiguity is a `refused`, not a reason to build a
   login-classification subsystem.
4. **Containment is this document's existing boundaries, unchanged.** No jar selection, no Keychute
   browsing, no other credential, no cross-origin reach; `browser_autofill` is admissible under the
   mechanical tool validation because it is browser-server-mediated.
5. **Bounded retries.** One fill per grant read, and a read is not a login submission: on a clear
   bad-password outcome the session records it, further fills are refused, and the run returns
   `needs_human`. No automatic re-request loop; a persisted needs-attention latch is a later upgrade
   only if unattended retry workflows become real. Challenges follow one rule, the same one the
   failure contract already draws: a challenge a human **can complete in the live session** — an MFA
   code, a captcha, a "verify it's you" click — is the existing `handoff_pending` path (the session
   parks under human control, the human finishes it, handback resumes the task with the
   authenticated cookies intact under the sanitized-recovery semantics in Session binding); what
   **no one can finish mid-session** — a bad password, an SSO redirect out of the confined origin
   set, a hard bot block — is terminal `needs_human`. Autonomous completion of MFA and IdP/SSO
   credentials are deferred for blast radius, not declared impossible.

### Read-back protection

The prerequisite for the session continuing after a fill is that the model cannot read the secret
back out of the page through its own tools. This is a **best-effort, fundamentally leaky boundary**,
and the design says so rather than pretending otherwise: a browser is an open-ended input and
rendering surface, and there is no confidence that every channel by which a filled value could reach
a model-visible observation has been spotted in advance. What the design commits to is the property,
the place it is enforced, and how holes are handled:

- **The property.** A value the fill placed in a protected control does not reach the model through
  browser-server's observation channels (snapshots, screenshots, extraction) or through any
  model-driven input that moves it somewhere those channels can see. The secret appears in no tool
  argument, result, event, exception text, trace, or log.
- **The enforcement.** Deterministic controls in browser-server, at its observation and input
  chokepoints, applied uniformly to every authenticated-site session from creation: protected
  controls (every `input[type=password]`, plus the exact elements a fill touched) are tracked by
  element — surviving type changes such as a "show password" toggle — masked in observations, and
  fenced against value transfer by model-driven input; `exec`, raw-DOM extract, and equivalent
  escape hatches are absent, jar or no jar. Other form values stay visible, because ordinary
  authenticated work needs them and blanket masking buys nothing against the residual below.
- **The known channels are the initial test set, not the specification.** Today's list — the
  snapshot walker copying `el.value`, screenshots of a revealed field, copy/paste into a visible
  text control, `drag_and_drop` of a selection into one — is what the first implementation must
  close and test. Session-wide clipboard denial and withholding `drag_and_drop` from the
  authenticated visual profile are cheap conveniences on top, not the boundary.
- **How holes are handled.** Further channels will be found. They are implementation findings for
  the browser-server PR that builds these controls, where the specific mechanism can be debated and
  tested, not reasons to reopen this design. A newly found channel is closed at the same chokepoints
  under the same property; it does not change the architecture.

The accepted residuals, stated plainly: the boundary is best-effort against a determined
prompt-injection campaign, not proof; and once filled, the approved origin's own JavaScript can read
the field and place its value anywhere — the same exposure as any password manager. The controls
that matter against that are which sites the operator wires up and the destination checks above, not
masking.

### Jars, refresh, and what is deferred

Jars are persistence, not a login prerequisite. Automatic jar refresh after a successful in-session
login is deferred until expiry friction demands it; when added, the correctness rules from the
earlier PR #1069 revisions remain binding — refresh targets the exact configured `jar_id`, preserves
stored scope and probe, and saves only probe-verified state. Also deferred or cut: the login-only
broker profile and post-fill lock (withdrawn), deterministic multi-page login orchestration, a
generic login-vs-signup classifier, TOTP-seeds-in-Keychute, and magic-link login (a separate
workstream touching mailbox taint — `sensitive_read_broadening` in
[runtime-taint-machinery.md](runtime-taint-machinery.md) — never a scope expansion here).

### Autofill build order

1. **browser-server:** Keychute client (in-cluster URL, internal CA); the pinned alias and explicit
   confinement origins (the complete configured set) on `create_session`; the autofill endpoint with
   requests tied to the checked document's origin, granted-constraint destination checks, serialized
   target binding, element checks, one request + one read per fill step; uniform read-back
   protection in authenticated-site sessions. Regression tests: plaintext in no
   response/event/log/exception; fills refused off the granted origin, in iframes, on new-password
   fields, after target invalidation, and outside authenticated-site sessions; masking, transfer
   interception, and `exec` denial active before the first fill; a jarless session confined
   identically to a jar-loaded one; a fill for any alias but the pinned one refused.
2. **family-assistant:** `credential_alias` on `authenticated_sites`, pinned onto the session at
   creation; the `browser_autofill` tool (no alias argument) in the authenticated profiles;
   `approval_pending` as a parked resumable outcome; probe-stale-jar runs for alias-bearing sites
   routed (revoked jars disable autofill) to the jarless session; every authenticated-site run,
   jarless included, excluded from the backend's transparent re-provisioning; docs.
3. **Keychute / kube-config:** register the `browser-server` client; per-secret autofill policy rows
   carry the permitted page origins and outcome — no server changes.
4. **Prove it** on a real password login, then extend only in response to observed failures.

Open questions: the mechanical form of the "clear bad-password outcome" (agent report plus a small
per-session fill cap as deterministic backstop — leaning both); notify-only cadence for standing
autofill grants (every release, until volume says otherwise); secret format (one structured
`{"username": …, "password": …}` secret per site account); whether `credential_alias` survives at
all once Keychute offers request-time discovery of "which secrets may I request for this origin" —
it is one line of routing today, not a design commitment.

## Failure behavior

The high-level tool returns a small set of actionable outcomes:

- `running`: the run handed off to the background; the result carries the opaque handle for later
  retrieval of the typed result or resumption;
- `completed`: the browser profile reported task completion;
- `login_required`: the saved session is stale or missing;
- `blocked_by_scope`: browser-server blocked an unconfigured origin transition;
- `review_blocked`: a native safety decision or optional judge refused an action;
- `handoff_pending`: the worker handed the browser to the human and parked the session; the result
  carries the takeover link and a typed resume handle that a follow-up invocation consumes after
  handback;
- `approval_pending` (autofill sites only): a Keychute release needs an operator decision that
  outlasted the run; the session parks exactly as for `handoff_pending`, and the resume handle
  retries the same fill step after approval (autofill section);
- `needs_human`: SSO, hard bot blocks, a bad password on an autofill site, or an ambiguous workflow
  a human cannot unblock mid-session — fully terminal, with jar refresh (or a corrected Keychute
  secret) and retry as the human path. A challenge the human *can* complete in the parked session,
  MFA codes included, is `handoff_pending`, not this;
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
    excludes `exec`, globally granted tools, and ambient household context, and whose delegation
    reaches only the configured visual profile.
05. It can select and save ordinary meal choices without asking for jar-load confirmation.
06. Neither browser profile can list or choose another jar, obtain an unapproved Keychute release,
    or invoke unrelated household tools.
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
- Disable the backend's transparent lost-lease and gone-session re-provisioning for every
  authenticated-site session, jar-bound or jarless: every command fails closed until handback or run
  end, never a fresh unconfined session.
- Never send `confine_navigation: false` or `allow_exec: true` for jar-loaded sessions.
- Verify the created session's jar generation and its complete effective origin set — jar origins
  plus any saved navigation allowlist — against trusted configuration, rejecting mismatches fail
  closed at the session-creation chokepoint.

### M2 — High-level tool and processing-profile wiring

- Add `run_authenticated_site_task` through the existing local tool registry.
- Grant it only to configured caller profiles through the ordinary tool-policy system, and enforce
  the site's `authorized_users` check fail-closed before session creation.
- Add the `authenticated_browser_profile` and `authenticated_browser_visual_profile` variants: no
  `exec` in policy or prompt, globally granted tools withheld via `excluded_global_tools`, ambient
  context providers excluded via `excluded_context_providers`, delegation pinned at argument level
  to the configured visual profile (which itself delegates to nothing), and tools restricted to the
  mechanically defined browser-server-mediated set (UCP shopping tools rejected fail closed).
  Authenticated runs delegate to them rather than the shipped profiles, and startup validation
  rejects a site configuration naming a profile that violates these constraints.
- Bind the created jar-loaded browser session into the delegated execution context, keyed per run
  rather than per conversation, and serialize authenticated runs within a conversation.
- Tie session lifetime to the delegated run's terminal state: ownership transfers with a background
  handoff, exactly one owner closes the session, and an idle/maximum-lifetime backstop reclaims a
  session whose owning run dies.
- Delegate the objective to the authenticated browser profile.
- Preserve the existing shared-session delegation mechanism for the visual variant, running that hop
  inline (async handoff disabled) within the owning semantic run.
- Ensure neither browser profile receives jar-management, credential-management, or recursive
  authenticated-site tools (`browser_autofill`, the one credential-adjacent tool, is gated by
  Keychute's release policy — autofill section).
- Preserve browser result provenance when the result returns to the caller.
- Persist the typed `AuthenticatedSiteTaskResult` on the delegation run record at terminal state,
  retrievable via the run's opaque handle, so backgrounded runs deliver typed results without text
  parsing.
- Close the browser session on completion and failure paths.

### M3 — Human provisioning and stale-session recovery

- Use browser-server's existing human sign-in and saved-login UI for initial provisioning.
- Add a trusted operator mapping from saved jar to site configuration.
- Surface `login_required` without exposing the full jar inventory to the model.
- Support human refresh of the same jar ID and retry from the original objective.
- Park a handed-off session under exclusive human control, and rebind it when a follow-up invocation
  resumes the terminal delegated run after handback, receiving the handback token from
  browser-server server-side rather than through the conversation.
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
- Prototype the action-review judge on the evaluated tool-call reviewer prompt and verdict contract
  rather than from scratch; the 2026-08-31 Gemini 3.7 Flash run measured zero benign friction, so
  observe mode is the confirmation step, not a feasibility question.
- Run it in observe mode first and measure false positives before enabling confirmation or blocking;
  expanding independent attack units toward the 1% false-allow target (at least 300 clean units) is
  the measurement path.
- Do not introduce per-site endpoint or selector allowlists as a prerequisite.

### M6 — Measured follow-ups

Only after the vertical slice is in regular use:

- add scheduled execution under a dedicated existing processing profile;
- add Keychute credential autofill per the autofill section's own build order, for sites where a
  password login is the better acquisition path or jar expiry is a recurring burden — it is a
  per-site alternative to a saved jar, not a jar-refresh mechanism; automatic jar refresh after an
  in-session login comes after that, and only if measured;
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

The Keychute design from [PR #1069](https://github.com/werdnum/family-assistant/pull/1069) is folded
into this document (the autofill section). Agent-driven login is a fill primitive on the ordinary
authenticated session — a per-site alternative to a saved jar, never a live-session transfer — and
never exposes credentials to a model.

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
  tools included, and their delegation reaches only the site's configured visual profile.
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
- The autofill path necessarily exposes the entered credential to JavaScript running on the approved
  login origin, as any password manager does.
- Browser automation may break when a site changes. Failure must be visible and must not claim
  completion without evidence.
- Some sites will block automated or cloud browsers and remain human-operated.

## Deliberate simplifications

What the implementation left out on purpose, so a reviewer does not have to rediscover each one:

- **The session lifetime backstop is browser-server's, not Family Assistant's.** A run whose worker
  dies without settling leaves a session that FA no longer tracks; it is reclaimed by the service's
  own idle and maximum-lifetime limits rather than by a sweep on this side. A sweep here would be a
  second, weaker copy of a reaper that already exists.
- **The run-scoped session binding lives in process.** It is keyed by the delegated run's
  subconversation and resolved one parent link deep, which is exactly the two hops an authenticated
  run has. A restart loses the binding; the session is then reclaimed by the backstop above and the
  run fails closed rather than picking up a session it can no longer account for. Making the binding
  survive a restart would mean trusting a session id across a process the operator may have
  redeployed.
- **`login_required` is surfaced without any jar inventory.** The model is told the saved login
  needs attention and nothing more -- no jar id, no list, no freshness detail. Refreshing one
  remains a human operation in browser-server's own UI, as M3 scopes it.
- **One authenticated run per conversation.** A second concurrent invocation is refused rather than
  queued. Queuing would mean holding a browser session open for a task nobody has started yet.
- **`postcondition_check` is a configuration stub.** A site may name a check; nothing runs it yet.
  It is recorded so the first real check does not need a configuration change, and it makes no
  promise in the meantime.
- **The action-review judge, jar refresh, TOTP and SSO are not built**, as the design defers them.
  `mitigations.action_review` accepts `observe` but changes nothing today.
- **The outcome is derived, not declared.** There is no structured status the worker returns. The
  run's status is read off the autofill latches and the session's own handover state, because a
  model that forgets to mention an outstanding approval must not be able to turn a parked run into a
  completed one. The cost is that outcomes the orchestration cannot observe -- `blocked_by_scope`,
  `site_changed`, `review_blocked` -- are defined in the contract but are not yet distinguished from
  `completed` or `failed` at runtime.
- **`start_delegation` duplicates part of `delegate_to_service`'s setup** rather than the tool being
  refactored onto the new seam. The tool's confirmation gate runs between steps the seam merges, so
  unifying them would move a confirmation prompt relative to target resolution; that is a change to
  existing behaviour this work did not need to make.

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
  session cleanup, revocation, result provenance, inability of browser profiles to acquire a second
  authenticated session, and the complete handoff-and-resume cycle: takeover-link delivery,
  server-side receipt of the handback token, and resumption after sanitized fresh-page recovery with
  the authenticated session and worker context preserved.

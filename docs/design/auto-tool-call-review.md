# Auto Tool-Call Review

## Status

Implemented through the shared reviewer, taint/static runtime integrations, deterministic signals,
audit diagnostics, and the environment-inclusive browser review contract. Production remains in the
designed M2 shadow posture (`taint_policy.mode: observe`); the evidence-gated M5 enforcement
decision is operational work, not a code default. The browser contract is ready for its DOM-path
call site when PR #1136's authenticated-session runtime lands.

This design promotes the adjudicator described in
[risk-adjudicated-taint-enforcement.md](risk-adjudicated-taint-enforcement.md) from that document's
contingent tier to the primary deliverable, and amends its rollout ordering and default floor
posture. It also supplies the shared "action-review judge" that
[authenticated-site-capabilities](https://github.com/werdnum/family-assistant/pull/1136) names as a
later mitigation, and the `adjudicate` integration that the taint machinery and tool policy engine
describe as future work.

Everything this document does not amend in the risk-adjudication design remains as written there:
the adjudicator's injection posture, the verdict-rank and merge semantics, deny-and-continue,
escalation counters, the friction budget, and the lean-core sink corrections all carry over. This
document changes *when* the judge is built, *what its default authority is*, and *where the
deterministic floor lives* — and adds the static-policy and browser-action integration points.

## Decision

Build one shared **tool-call reviewer**: a non-agentic, single-shot model invocation that examines a
proposed tool call together with deterministically selected trusted context and returns a structured
verdict — `allow`, `confirm` (escalate to the existing durable confirmation machinery), or `deny` (a
structured refusal the agent can route around). It is invoked from three places:

1. **Runtime taint policy** — a new `adjudicate` matrix outcome, replacing most of the `confirm` and
   `deny` cells in the shipped default matrix.
2. **Static tool policy** — a new `review` rule decision alongside `allow`/`deny`/`confirm`, so
   operators and profiles can put judgment anywhere a static confirmation sits today.
3. **Browser action review** — a direct API used by the authenticated-site work (PR #1136) to review
   a proposed browser action against the delegated objective and the site's configured damage
   envelope.

The default posture is deliberately judge-forward: in the shipped configuration the reviewer's
verdict space is complete (it may `allow`) in every cell that engages it, and **verdict floors are
operator configuration, not shipped defaults**. The deterministic layer of this system is the one
that already exists and already works — processing-profile confinement and static tool policy — not
a new set of confirm floors inside the matrix.

## Why the ordering inverts

The risk-adjudication design ships floors first and gates the judge behind a measured friction
budget: enforce a lean matrix of confirm floors, run for 30 days, and build the adjudicator only if
the numbers demand it. That ordering optimizes for a deployment that is about to turn enforcement
on. The actual deployment is not in that position:

- **The production baseline is zero enforcement.** `taint_policy.mode` is `observe`,
  `operator_minimum` is empty, and there are no matrix overrides. Every would-gate call in the audit
  data executes silently. Against that baseline, a probabilistic judge is not a weakening of
  anything — it is the first control that can say no.
- **Floors-without-judgment is the configuration that has already stalled.** The shipped matrix has
  had confirm cells for a year; enforcement never turned on, because the projected interruption
  volume was intolerable. The judge is not an optimization of a working enforcement regime; it is
  the thing that makes enforcement deployable at all. Building it last means enforcement arrives
  last.
- **The lean core has a long prerequisite chain** (PR #1111, the epoch, sink splits, serialization
  envelopes, the binding condition) that is valuable but has not landed. The reviewer is
  independently buildable against machinery that exists today — `TurnTaintState`, per-row taint
  metadata, `TaintTrackingToolsProvider`, `taint_audit_events`, the durable confirmation system —
  and improves the security posture the day it reaches shadow mode.
- **The evidence favors judgment over interactive review.** The auto-mode numbers the risk document
  itself cites: a two-stage classifier at 0.4% false positives catching 89% of dangerous commands,
  against 13.6% for interactive human review. The risk document also records the maintainer position
  that a clean-context judge should be allowed to *approve*, auto-mode style, rather than only
  choosing between confirm and deny. This document makes that the shipped default rather than a
  per-cell promotion unlocked by shadow evidence.

The result is the mirror of the risk document's disposition: **judge first, floors on evidence**,
instead of floors first, judge on evidence. Both keep the same invariants — judgment never writes
provenance, probes only escalate, errors fail closed, and tighten-only merging is preserved.

### Where the deterministic floor actually lives

The Codex decomposition the risk document surveys — sandbox mode (what is technically possible)
versus approval policy (when to ask) — already exists in this codebase, and the sandbox half is the
strong half:

- Processing profiles are the hard capability boundary. The browser profiles hold no household
  context (browser reads are tracked separately as potentially sensitive); `media_analyst` reaches
  no tools at all; `coder` runs in a throwaway sandbox with no FA tool surface; the engineer profile
  confirms every side effect.
- Static tool policy is deny-by-default per profile, with tighten-only operator override layering.
- Server-side validation makes sinks safe by construction where it matters most
  (`send_message_to_user` recipient validation, browser-server origin confinement, opaque cookie
  jars).

The reviewer sits on top of that surface as the *when-to-ask* dial. Doubling the deterministic layer
inside the judgment layer — confirm floors on every externally authored tier at every egress sink,
before any judge data exists — is what the risk document's own PR #1121 lesson warns against: a gate
that decides ahead of measurement, without a satisfiable path for ordinary use, gets turned off and
then protects nothing. Floors stay available (see below) and become cheap to add exactly where
shadow data or an operator's risk appetite says the judge is not enough.

## Design principles

Adopted from the risk-adjudication design, with one amendment each where marked:

1. **Provenance stays deterministic.** Nothing probabilistic ever writes provenance, lowers a tier,
   removes a source, or persists a verdict as trust.
2. **Judgment never relaxes an explicit floor.** *(Amended.)* Where the operator has configured a
   floor — via `operator_minimum` or a floored `adjudicate` cell — the verdict space in that cell
   excludes `allow` and no verdict, probe result, or provenance computation adds it back. Where no
   floor is configured, the verdict space is complete. Floors are a configuration surface, not a
   built-in property of sink classes.
3. **The attacker never addresses the judge as conversation.** Untrusted conversation content is
   never rendered to the reviewer; it appears only as provenance stubs. The one deliberate exception
   is the payload under review itself — see the input contract below.
4. **Fail closed, degrade to today or stricter.** Reviewer unavailable, timing out, or returning a
   malformed verdict resolves to the delegating cell or rule's **fallback outcome**: for an
   `adjudicate` matrix cell that defaults to the outcome the cell had before adjudication (so a
   deployment whose reviewer is absent runs exactly today's matrix — including the `deny` cells);
   for a static `review` rule it defaults to `confirm`. No fallback is ever `allow`.
5. **Friction is a budget.** The risk document's budget stands: ≤ 1 interactive confirmation per day
   p50, ≤ 3 p95, measured over 30 days from `taint_audit_events`.

## The reviewer

A dedicated, non-agentic model invocation — one call, structured verdict, no tools, no context
providers, no agent loop. Configured like a processing profile (provider, model, retry) but it is
not a profile: its prompt is assembled by code from a closed set of inputs, and it cannot be
delegated to or addressed from conversation. A flash-class model is the intended default; the
production audit baseline (~48 would-gate calls/day) makes per-call cost negligible, and latency
lands only on gated calls.

### Input contract

The reviewer's context is a typed structure assembled deterministically:

- **Trusted conversation content, selected by taint metadata, never by message role.** Rows and
  trigger parts whose stored tier is `trusted_user` render; everything else renders as a one-line
  provenance stub ("tool result from `gmail_get_message`, tier unknown_external"). This rule is
  load-bearing: in email intake the sender-controlled body arrives represented as a `UserMessage`,
  so role-based selection would hand the judge the attacker's email verbatim. The per-row taint
  metadata this selection needs is already persisted.

- **The tool call under review**: name, resolved sink class, and the tool description for local
  tools only (an MCP tool's description is remote-server content, so MCP tools render as server id
  plus annotation-derived tags).

- **The arguments, rendered in full inside fenced data boundaries** with boundary neutralization
  (the `_neutralize_untrusted_evidence_boundaries` pattern). This is a deliberate divergence from
  the risk document's provenance-filtered rendering, discussed below.

- **A provenance digest of the turn**: which sources are present, their tiers, types, and ordering
  (the sensitive-read records and fresh-taint sequence, where available in the current context).
  Closed-vocabulary fields render verbatim; free-text fields (reasons, artifact titles, sender
  addresses) render only for trusted-tier sources and are otherwise replaced by type-and-tier stubs,
  because reasons already interpolate artifact-controlled text today.

- **The delegating policy cell or rule**, including its verdict space, so the reviewer knows which
  verdicts are available to it.

- **Operator review guidance**: trusted deployment- and profile-level free text describing what the
  household considers routine, plus — for action review — the site's configured damage envelope.
  This is operator configuration, the same trust class as a system prompt, and it is what makes the
  reviewer tunable without code changes.

- **The trigger definition, for unattended runs.** An event-handler or scheduled turn has no trusted
  user message; its trusted intent is the human-authored definition that created the run — the
  listener's instruction, the automation's prompt, the scheduled task's objective. The definition
  renders under the same taint-metadata rule as everything else: its stored provenance decides, and
  **absent provenance fails closed to a stub**, per the taint design's standing missing-tier rule.
  The absent case is currently every case — the listener and schedule-automation tables persist only
  creator identity (`processing_profile_id`, `created_by_user_id`), not taint provenance — so until
  the risk document's artifact-provenance work stamps automation definitions at their authoring
  chokepoint, trigger definitions render only as stubs and the reviewer for unattended runs works
  from operator guidance and the delegating rule's context, with ambiguity taking the deferral path.
  That is weaker signal, never a laundering path: an automation authored from a tainted turn (the
  executable-persistence concern the risk document covers) cannot render as trusted intent, because
  no definition can until stored provenance proves it. The trigger *payload* — the event data,
  deliberately untrusted — is always represented as a provenance stub, never rendered.

- **The originating request, for delegated runs.** A delegated subconversation is unattended in the
  same mechanical sense — no trusted user row of its own — but unlike an event or a schedule, a
  human did ask for something; they asked it in the conversation that delegated. The delegating
  turn's active user row travels with the run as the trigger's *originating request*, and renders
  under the same taint-metadata rule as everything else: its own stored provenance decides, so an
  email-intake turn (whose sender-controlled body is represented as a user row) propagates nothing.
  A chain of delegations passes on what it inherited rather than re-deriving it, so nesting answers
  to the same human message. The delegation *goal* is a separate field and stays what it always was
  — model-composed text carrying the delegating turn's taint — so a turn that read untrusted content
  before delegating stubs its goal while its human request still renders. That distinction is the
  point: it is what lets the reviewer tell a faithful delegation from a smuggled one on exactly the
  turns where smuggling is possible, where previously both rendered as stubs and `confirm` and
  `deny` collapsed into `deny`.

  Propagation is built at one chokepoint (`build_delegation_review_trigger`), which every delegation
  boundary — synchronous, worker-run, and the completion wake — routes through, so a new boundary
  gets the behaviour by construction rather than by remembering to. The rows are read at
  trigger-build time rather than snapshotted at hand-off, so the propagated request carries the
  provenance the message actually has, and only for a turn a human actually spoke in — see the
  nested-delegation residual below for why a subconversation's own rows are withheld.

  Missing authoring provenance also affects the runtime taint state, not just what the reviewer can
  render. At present **every unattended callback** enters as `unknown_external`, including event
  listeners, script wakes and failures, schedule automations, `schedule_future_callback`, and
  reminders. Its user-role trigger row and taint-derived assistant rows persist in conversation
  history, so a later interactive turn that reloads that history can inherit the elevated tier and
  incur adjudication, confirmation, or denial friction. This is the conservative interim behavior
  until automation-definition provenance is stored at authoring time and threaded into callbacks;
  creator identity alone is not enough to suppress it.

### On rendering arguments: the auto-mode position, not the stub position

The risk-adjudication design requires argument values to render verbatim only when provably derived
from trusted-tier content, with everything else replaced by provenance stubs — and defers the judge
until that matching machinery exists, on the argument that a judge reading attacker text is an
injection target.

This document takes the position auto mode actually ships: the classifier sees the user's words and
**the full executable payload**, fenced as data, and the defense is that an injected payload must
produce a call that looks benign *in isolation* against what the user asked — at which point the
attack has been constrained into approximately the actions the user wanted anyway. The alternative
available today, stub-everything-unproven, blinds the reviewer on exactly the calls it most needs to
judge (a free-form message body would always stub, because the matching machinery that could prove a
user dictated it does not exist yet), and building that machinery first is how the judge stays
unbuilt for another year.

The residual — untrusted natural language inside argument values lobbying the verdict that gates it
— is real and accepted, with three mitigations: the reviewer's template treats every rendered value
as data and is prompted to treat in-payload instructions as evidence *against* the call; boundary
forging is neutralized; and the escalate-only probe (contingent, unchanged from the risk document)
is the intended content-level tripwire. **Argument provenance matching remains the planned
hardening** — when it lands, proven-trusted values keep rendering, unproven free-form values degrade
to stubs-with-provenance, and the reviewer's operating point improves without changing its contract.
It is an upgrade, not a prerequisite.

### Verdicts

- `allow` — execute; the structured verdict and resolution status are written to the audit trail.
- `confirm` — escalate to the existing durable confirmation machinery. The reviewer's reason is
  included in the rendered confirmation so the human sees *why* — this is what turns a generic
  "approve this tool call?" into "this message quotes your notes and goes to an address that appears
  nowhere in your request". A live confirmation for a non-tool named sink (a profile or brokered
  request) also renders the complete request payload; if that payload cannot be rendered or does not
  fit the confirmation channel, the request is refused rather than offering a truncated approval.
- `deny` — a structured deny-and-continue tool result stating what was blocked, why, and what safer
  route exists. The model continues; hard errors are reserved for explicit floors.

Malformed output, timeout, or provider error resolves to the delegating cell or rule's fallback
outcome, per design principle 4 — the pre-adjudication matrix outcome for taint cells, `confirm` for
static `review` rules. Every verdict — including shadow-mode verdicts — writes a
`taint_audit_events` row with structured verdict, status, latency, and delegating context, and the
diagnostics endpoint grows verdict counts. Durable rows use a fixed trusted reason and deliberately
omit the reviewer's free-form rationale; that rationale remains available only to the live
confirmation or deny result. Argument summaries retain names declared by a trusted local tool schema
and pseudonymize unexpected keys; MCP schema keys remain untrusted. All argument values are omitted
regardless of origin. Source provenance retains closed-vocabulary type and tier, while externally
authored identifiers, labels, and reasons are omitted. The audit row's `turn_id` and `tool_call_id`
locate the canonical assistant message for later reconstruction when the call came from stored
message history, without duplicating the conversation or arguments in the audit table. Direct
named-sink and other non-message-originated authorizations can have no corresponding message row;
their structured audit evidence is intentionally the complete durable record.

### Escalation and cost bounds

Adopted from the risk document: after 3 consecutive denied calls or a configured per-turn total, the
pattern converts to a single summarizing human confirmation, or ends the turn with an explanation in
unattended contexts. A per-turn review budget caps model spend; exceeding it resolves remaining
gated calls at their fallback outcomes.

**Deliberate v1 simplification:** escalation counters are turn-local and in-memory. A delegated run
restarts them, so repeated fan-out can duck the threshold until the serialized-taint-schema
extension (risk document M6) lands. Accepted because the counters are a UX bound, not a security
boundary — every individual call is still judged — and because blocking the reviewer on the
serialization work would invert the priority this document exists to set.

### Deterministic short-circuits and signals

One deterministic check runs before the model call and can resolve the evaluation without spend or
latency:

- **Confined-profile egress exemption.** A profile that excludes aggregated context by construction
  and whose current turn has recorded no sensitive reads has nothing from the assistant context to
  exfiltrate; disclosure-sink reviews short-circuit to `audit`. This is the computable core of the
  risk document's binding condition (its protected-history clause needs the M6 serialization work
  and joins the check when that lands; until then, absence of the history signal fails toward
  invoking the reviewer, not toward exemption). The exemption applies at the taint layer to
  browser-tagged actions too; any independent static/action-review rule still invokes the reviewer.
  Browser tools that return page content are tagged as sensitive reads, so their successful return
  prevents later disclosures from exempting. Any input the check cannot determine falls through to
  the reviewer. The short-circuit is valid only when the effective execution minimum permits
  `audit`: a configured `confirm` or `deny` verdict floor, or a matching `operator_minimum`,
  disables the exemption and invokes the reviewer within that tighter verdict space.

  After every successful `read_only` + `sensitive_data` call, the tool-execution chokepoint retains
  an explicit corpus scope recorded by the tool or adds a conservative tool-level sensitive read if
  the live state did not change. Failed reads add nothing. This deliberately avoids a parallel
  reservation protocol: an egress formed before a concurrent read returns cannot contain the read's
  result, while a disclosure after return sees the recorded read and invokes the reviewer.

A second deterministic computation is a **signal, never a bypass**:

- **Trusted-destination echo.** Whether the destination-bearing argument of an egress call is an
  exact whole-value match (post-normalization) for a destination in the current request's
  trusted-tier user text — the small, checkable subset of argument provenance matching. Non-URL
  values retain case-insensitive text normalization; for parsed URLs only the scheme and host are
  case-insensitive, while path, query, and fragment case remain exact. Per the risk document,
  mention is not authorization: a request can name a destination while *forbidding* it, and no
  string match can tell the difference, so a passing match never skips the reviewer and never
  produces `allow` on its own — the payload still has to look benign against the request. The match
  result feeds the reviewer as strong evidence ("destination appears verbatim in the current trusted
  request" versus "destination appears nowhere the user wrote") and appears in verdict reasons and
  confirmation prompts.

## Integration 1: runtime taint policy

`TaintPolicyOutcome` gains `adjudicate`, optionally carrying a **verdict floor** — the weakest
verdict the reviewer may return in that cell. The rank and merge semantics are exactly the risk
document's: a bare `adjudicate` merge-ranks strictly above `audit` (a profile cannot swap the judge
out for a log line), `adjudicate` with a `confirm` floor ranks equal to plain `confirm`, and
`operator_minimum` applies to the *verdict* after adjudication on the strictness axis only — `deny`
verdicts always stand, and adjudication is disallowed in cells whose minimum is `redact`. In
`observe` mode the evaluator still returns `adjudicate` and the provider still invokes the reviewer;
only the verdict's effect downgrades to `audit`. And because the effect is `audit` whatever the
verdict says, the observe-mode invocation runs off the critical path: the call proceeds immediately
and the verdict is recorded asynchronously, so shadow adds no latency anywhere — including the
browser hot path, whose every tool call resolves to an egress sink and would otherwise pay a
blocking reviewer round-trip per action until the M3 exemption lands. That is the free shadow phase:
real verdicts against real traffic at zero user-visible cost, starting the day the defaults change,
while the deployment's `mode` stays `observe`. Under `enforce` the invocation necessarily blocks,
which is why the M5 gate includes p95 reviewer latency.

The shadow property belongs to `observe` mode, not to the defaults change itself. For a deployment
already running `mode: enforce`, adopting the new default matrix is a real posture change — cells
that gated unconditionally become judged — and must not happen silently: the configuration
reference's migration note tells enforce deployments to pin every previous outcome before upgrading
or to adopt the judged posture deliberately. The literal cell-for-cell pin is an `operator_minimum`
of `confirm` on the egress cells, on `unknown_external × known_user_message`, and on
`unknown_external × sensitive_read_broadening`, plus `deny` on `unknown_external × sandbox_network`.
Absent authoring provenance now puts every unattended callback at `unknown_external`, so that
literal pin also makes reminder delivery through `send_message_to_user` defer for confirmation
instead of delivering. A deployment that relies on automatic reminders may deliberately omit only
the `known_user_message` minimum, accepting a reminder-compatible exception to the old posture
rather than calling it a cell-for-cell pin. Verdict floors and `operator_minimum` remain
tighten-only against the judge just as against profiles. No known deployment runs `enforce` today —
the maintainer's is the only known deployment, and it runs `observe` — so this is defence in depth
for third-party deployments of a public codebase: a documented pin and a config test, not migration
machinery.

### Default matrix changes

Relative to the currently shipped `_default_taint_matrix()`:

| tier × sink                                               | today   | proposed   |
| --------------------------------------------------------- | ------- | ---------- |
| externally authored tiers × `arbitrary_external_message`  | confirm | adjudicate |
| externally authored tiers × `attacker_addressable_egress` | confirm | adjudicate |
| `known_contact`/`recognized_machine` × `sandbox_network`  | confirm | adjudicate |
| `unknown_external` × `sandbox_network`                    | deny    | adjudicate |
| `unknown_external` × `known_user_message`                 | confirm | audit      |
| `unknown_external` × `sensitive_read_broadening`          | confirm | audit      |

Rationale for the rows that are not simply confirm→adjudicate:

- **`known_user_message` becomes `audit` permanently**, adopting the maintainer decision recorded in
  the risk document: household messaging reaches only server-validated recipients, so the attacker
  gains an attributable in-channel voice and no reach. Not worth any friction, judged or human.
- **`sensitive_read_broadening` becomes `audit`**, the audit-the-reads-gate-the-egress choice: a
  read only broadens exposure, actual loss requires a subsequent egress call — which is adjudicated
  — and the reviewer sees the read history and its ordering in the provenance digest at exactly that
  moment. Spending a judge call on the noisiest cell buys nothing the egress-time review does not
  already see.
- **`sandbox_network` moves from hard deny to adjudicate**, superseding PR #1111's
  confirm-with-fail-closed correction in the same direction it was already moving. The reviewer can
  approve an interactive coding task *and* an unattended one whose call is benign relative to its
  trigger — which confirmation never could, because unattended contexts have no one to ask. This
  cell's fallback outcome stays `deny`: a reviewer that is absent, erroring, or over budget does not
  soften the cell to a confirmable action — only an actual verdict does. The cell without its judge
  is exactly today's cell.

**No cell in the shipped default carries a verdict floor.** This is the deliberate answer to the
floor question: the shipped baseline is judge-with-full-authority everywhere the matrix gates,
because the alternative shipped baseline — observed reality — is nothing at all. The floors the risk
document specifies (egress at every externally authored tier, high-impact actuation, destructive
writes, executable persistence) become the **recommended hardening set** documented in
`CONFIGURATION_REFERENCE.md`: one `operator_minimum` block an operator pastes to get the
floors-plus-judge posture, per cell, when their damage tolerance or their shadow data says so. The
sink splits those floors depend on (actuation, destructive writes, executable persistence) remain
lean-core work from the risk document and are unchanged by this design; when they land, their new
cells default to `adjudicate` like their neighbors, and the recommended hardening set gains their
floors. Deployments registering Gmail/Drive still meet `require_taint_enforcement` per the risk
document's updated check: an `adjudicate` cell satisfies it only with a `confirm` floor, so that
integration's registration gate is one of the places floors are not optional.

### Provider integration

`TaintTrackingToolsProvider.execute_tool` and `authorize_taint_sink` handle the new outcome: on
`adjudicate`, assemble the review input from the execution context and taint state, invoke the
reviewer, then apply the verdict through the same paths that exist today — `confirm` enters
`_request_taint_confirmation` with the reviewer's reason attached, `deny` returns the structured
deny-and-continue result rather than raising, and `allow` proceeds with an audit row. The
deny-and-continue result and its escalation counters follow the risk document's semantics.

## Integration 2: static tool policy

`ToolPolicyDecision` gains `review`. A policy rule may now say:

```yaml
tools_policy:
  rules:
    - match: { tags_any: ["destructive"] }
      decision: "review"
      priority: 20
      description: "Judge destructive operations against the user's request"
```

Semantics:

- **Advertisement**: `review` behaves like `allow` — the tool is advertised regardless of
  `can_confirm`, because the reviewer can resolve to `allow` without a human. This fixes a standing
  gap where confirm-gated tools vanish from channels that cannot confirm (voice, some API contexts);
  under `review` they remain usable and only genuinely suspicious calls fail there.

- **Execution**: the reviewer is invoked with the same input contract, the matched rule as the
  delegating context, and the static layer's guidance. A `confirm` verdict uses the ordinary
  confirmation flow; in a context with no live confirmation channel it becomes a deferred durable
  confirmation where the call is independent and terminal (the fire-and-forget sends and filings the
  risk document's deferral rules already scope), and degrades to deny-and-continue otherwise.

- **Layering**: `review` is an ordinary decision value in the existing priority system — profiles
  and operators place it with the same offsets and tie-breaking as today. No new lattice is needed
  at the static layer; an operator who wants a hard gate keeps `confirm` or `deny`, exactly as now.

- **One judgment per call.** When static policy says `review` and the taint layer says `adjudicate`
  for the same call, the reviewer runs once with both delegating contexts, and one verdict satisfies
  both layers — the same single-payload rule that already governs double confirmation. The effective
  verdict space is the intersection (the tighter of the two floors), and the no-verdict paths merge
  the same tighten-only way: the combined fallback is the strictest of the layers' fallbacks, so a
  static `confirm` fallback never softens a taint-cell `deny` fallback.

  Browser environment/action review is separate from this ordinary static-plus-taint judgment. It
  has different evidence and authority, so PR #1136 invokes the environment-inclusive contract as a
  second judgment rather than folding hostile page evidence into the conversation reviewer. Its
  verdict and fallback merge tighten-only with ordinary authorization: either judgment can require
  confirmation or block, and the browser judgment cannot loosen an ordinary policy result.

What this buys, concretely: today's static `confirm` on `delete_calendar_event` and
`modify_calendar_event` interrupts the user on every deletion they just asked for, which is
rubber-stamp training; a `review` decision approves the aligned case and asks only when deletion
appears from nowhere. In the other direction, a profile like `event_handler` that today gets a
binary allow-or-nothing on `send_message_to_user` can hold it at `review` — judged against the
trusted listener definition per the input contract (the event payload itself is untrusted and
renders only as a provenance stub), so a send the listener's instruction plainly anticipates is
allowed without a human, and an invented one is not. An ambiguous `confirm` verdict in this
unattended context lands in the deferred-confirmation tray rather than a live prompt — still a
middle ground the binary decision lacks. This is the flexibility the static engine misses: a
decision between "always ask" and "never ask".

## Integration 3: browser action review (PR #1136)

The authenticated-site design needs a judge with different inputs: there is no conversation to
select from inside a delegated browser run, and the action is uninterpretable without the page
state, which is untrusted by definition. The reviewer therefore exposes a second, explicitly
environment-inclusive input contract:

- the delegated **objective** (trusted: composed by the caller before the browser saw hostile
  content — the delegation boundary is the chokepoint, per the risk document);
- the site's configured **damage envelope** and mitigation guidance (trusted operator config);
- a digest of **recent actions** in the session;
- the **proposed action**;
- the current **snapshot or screenshot, rendered fenced as untrusted environment**.

Verdicts map onto PR #1136's `allow` / `ask` / `block`. Rendering the untrusted page to this
reviewer is sound where it is unsound for the conversation reviewer because the verdict's authority
is bounded by construction: it governs only same-site actions already inside an operator-accepted
damage envelope, with origin confinement, jar opacity, and profile tool policy holding the tails.
The reviewer here is defence in depth against *obvious* misuse — plan changes, purchases, address or
credential changes, task-unrelated actions — exactly as PR #1136 scopes it, never a hard boundary.

Per PR #1136's M5, this integration ships in observe mode first on the DOM path (the visual path
already has native Gemini safety decisions), and an observe-mode DOM-path reviewer is the stated
prerequisite for configuring any site with a materially larger envelope than HelloFresh. Building it
as a second input contract on the shared reviewer — same verdict schema, same audit rows, same
config — is the point of having one component: the authenticated-site work configures a reviewer
instead of building one.

## Configuration

Implemented configuration:

```yaml
tool_call_review:
  enabled: true
  provider: "google"
  model: "gemini-3.7-flash"
  retry_config: { ... }
  timeout_seconds: 30
  max_reviews_per_turn: 25     # past this, gated calls resolve at their fallback outcomes
  escalation:
    consecutive_denials: 3
    total_denials_per_turn: 20
  guidance: >-
    Deployment-wide trusted guidance for the reviewer, e.g. which workflows the
    household considers routine.
```

- Profiles may add `review_guidance` text (trusted, additive).
- The taint matrix accepts `adjudicate` as a cell outcome, with an optional verdict floor; the
  floored form satisfies a `confirm` `operator_minimum` per the verdict-floor rank.
- Static policy rules accept `decision: "review"`.
- Authenticated-site configs reference the reviewer through their existing `mitigations` block
  (`action_review: "observe" | "enforce" | "off"`).
- Errors, timeouts, budget exhaustion, and `enabled: false` (or an unconfigured block) all resolve a
  judged decision at its **fallback outcome**: for an `adjudicate` matrix cell, the outcome the cell
  had before adjudication (`confirm` for the egress cells, `deny` for
  `unknown_external × sandbox_network`); for a static `review` rule, `confirm`. The fallback is
  per-cell precisely so that the system without its judgment layer is the system shipped today,
  never weaker — a blanket confirm fallback would quietly soften the deny cells.

`CONFIGURATION_REFERENCE.md` documents the block, the recommended hardening set (the floors an
operator adds to reach the risk document's posture), and the shadow-phase metrics to consult before
flipping `taint_policy.mode` to `enforce`.

## What the reviewer is not

Unchanged from the risk document: not a floor; not a provenance writer (no verdict lowers a tier,
removes a source, or persists trust); not a probe (content screening at ingestion remains the
escalate-only probe's job, still contingent); not a second confirmation surface (verdict escalations
flow into the existing durable confirmation machinery under the single-merged-prompt rule); not
per-token information flow.

## Rollout and work plan

Each milestone is a PR-sized, independently shippable unit. Verification is named per milestone;
construction detail is deferred to the PRs.

**M1 — Reviewer core.** The reviewer component: config model, input assembly (taint-metadata row
selection, fenced argument rendering, provenance digest, guidance), verdict schema and parsing,
fallback-outcome handling, audit rows. No call sites yet. *Verify:* unit tests with a fake LLM
covering verdict parsing, malformed-output and timeout resolution to the per-cell fallback (a
`deny`-fallback cell never softens to `confirm`), and — via the same assembly functions the runtime
uses — that no untrusted-tier conversation row or free-text digest field renders.

**M2 — Taint `adjudicate` in shadow.** The new outcome with verdict-floor and rank semantics,
provider invocation, deny-and-continue result, turn-local escalation counters, and the default
matrix change, with observe-mode invocations running off the critical path (see the shadow-phase
section). Production `mode` stays `observe`, so this lands as a pure shadow phase there: real
verdicts on real traffic in `taint_audit_events`, nothing user-visible. The milestone ships the
enforce-deployment migration note alongside the defaults change (pin previous outcomes via
`operator_minimum`, or adopt the judged posture — see the shadow-phase section), so no deployment's
enforcement weakens without an explicit configuration decision. *Verify:* merge-semantics unit tests
(profile cannot replace `adjudicate` with `audit`; floored form satisfies a `confirm` minimum;
post-verdict `operator_minimum` never weakens a verdict); a config test that the documented pin
reproduces the pre-change matrix outcomes cell-for-cell; replayed email-intake injection fixtures
produce verdicts, and none of them `allow` at the fixture's target sink; observe-mode evaluations
invoke the reviewer without blocking the gated call and downgrade effect to `audit`.

**M3 — Deterministic short-circuit and echo signal.** The confined-profile egress exemption
(computable clauses, fail-toward-review) and the trusted-destination echo signal as reviewer input.
*Verify:* a confined disclosure, including a taint-only browser disclosure, generates no reviewer
call; the same call from a context-bearing profile does; an independent static review still reviews
a confined browser action; a user-pasted URL reaches the reviewer with a positive echo signal and an
appended-query-parameter variant with a negative one; the negation fixture ("never send to X" in the
request, injection targeting X) reaches the reviewer rather than short-circuiting.

**M4 — Static `review` decision.** The new decision value, advertisement semantics, and the
one-judgment-per-call merge with the taint layer. *Verify:* policy-engine tests for layering and
priority with the new value; a call gated by both layers produces exactly one reviewer invocation
and at most one confirmation; an eligible independent terminal call without a live channel creates
one deferred durable confirmation, while an ineligible no-channel call degrades the same `confirm`
verdict to deny-and-continue.

**M5 — Enforce on evidence.** Review the shadow data against the gates: reviewer false-allow ≈ 0 on
the replayed injection fixture set, projected interactive confirmations within the friction budget,
p95 reviewer latency acceptable. Then flip `taint_policy.mode: enforce` and record the decision in
this document's status. Operators wanting floors apply the recommended hardening set at the same
moment. *Verify:* the standing metrics (verdict counts by cell and profile, escalation-counter
trips, deny-and-continue recovery rate, confirmations per week) over 30 days.

**M6 — Browser action review.** The environment-inclusive input contract, wired to PR #1136's
mitigation settings, observe mode first, alongside that design's M5. This is a separate invocation
from the ordinary static-plus-taint judgment, with results merged tighten-only at the browser action
chokepoint. *Verify:* per PR #1136 — observe-mode verdicts recorded for a real HelloFresh run; a
fixture with an in-page instruction to change plan settings yields `ask` or `block`; a stricter
ordinary verdict or fallback is never loosened by the browser verdict, and vice versa.

**Later, on evidence:** argument provenance matching (upgrading fenced rendering to
proven-or-stubbed); counter serialization across the delegation boundary (with risk-document M6);
capability-scoped approval reuse; the escalate-only ingestion probe. Each attaches to the
measurement that indicts it, per the risk document's complexity-budget rule.

## Security properties

When this design is working:

- Every call the matrix or a policy rule gates is judged, confirmed, or denied — under `enforce`,
  nothing gated executes on silence.
- No reviewer error, outage, or absence ever resolves to `allow`: every no-verdict path resolves at
  the per-cell fallback outcome, which is never weaker than the cell's pre-adjudication default —
  the `deny` cells stay `deny` without a verdict.
- No deterministic signal (the destination echo included) skips the reviewer or produces `allow` on
  its own; the confined-profile exemption resolves only to `audit`, only where the verdict space
  permits it.
- No reviewer output writes provenance, lowers a tier, removes a source, or relaxes a configured
  floor or `operator_minimum`.
- Untrusted conversation content is never rendered to the conversation reviewer; selection is by
  stored taint tier, not message role.
- Profile and operator policy merging remains tighten-only, with `adjudicate` ranked so it cannot be
  silently weakened to `audit`.
- Every verdict is auditable after the fact with its reason, cell, and latency.

## Accepted residuals

- **A reviewer false-negative on an unfloored egress cell can authorize exfiltration.** This is the
  headline trade, accepted explicitly: the comparison point is the current deployment, where the
  same call executes with no evaluation at all. Operators for whom this residual is unacceptable
  apply the recommended hardening set, which restores the risk document's floor posture per cell.
- **Untrusted text inside argument values reaches the reviewer, fenced.** Persuasion through the
  payload is possible until argument provenance matching lands; mitigations are template posture,
  boundary neutralization, and the fact that the injected call must still look benign against the
  trusted request.
- **Escalation counters reset across delegation in v1.** Bounded-UX regression, not a security
  boundary; fixed by the serialization extension.
- **The browser action reviewer reads the hostile page.** Bounded by the damage envelope and the
  mechanical confinement layer, per PR #1136's model.
- **A disclosure concurrent with an unfinished sensitive read can still take the confined
  exemption.** The disclosed call cannot causally contain a result that has not returned. Once the
  read returns, its recorded scope disables the exemption for later disclosures. This avoids a
  turn-wide pending-read reservation mechanism solely for an impossible data dependency.
- **A nested worker-run delegation propagates no originating request.** A subconversation holds no
  human message — only a goal its parent's model composed — so its rows are never read for
  originating intent. (A clean parent stamps that goal row `trusted_user`, which is exactly why the
  rule is "not a human's turn", not "not trusted": reading it would launder model-authored text,
  including any recipient the model added, into the field that claims to be the human request and
  into the destination echo that reads it.) A chain therefore carries the request forward only on
  the delegating turn's live trigger, which a run claimed later by a worker does not have. So a
  second-level *asynchronous* delegation reviews as it did before propagation existed — stubs, with
  ambiguity taking the deferral path — while synchronous chains and every first-level delegation
  carry the request. Closing it means either persisting the request with the run or resolving the
  parent chain at claim time, and neither is worth the storage or the recursion for a rare shape
  whose fallback is the previous, safe behaviour.
- **Shadow-then-enforce means a window where verdicts are recorded but nothing blocks.** Identical
  to today's posture; the window ends at M5.

## Review questions

1. Is judge-with-full-verdict-space the right shipped default for the egress cells, with floors as
   documented operator hardening — or should any cell keep a floor out of the box?
2. Is `sandbox_network` at bare `adjudicate` (superseding both the shipped `deny` and PR #1111's
   `confirm`) acceptable for unattended contexts, given fail-closed error handling?
3. Is the fenced-full-arguments input contract (auto-mode style) the right v1, with provenance
   stubbing as later hardening — or is stub-first worth the blinding cost?
4. Should static `review` be adopted immediately for the existing destructive-tool confirms, or held
   until the taint-side shadow data validates the reviewer?
5. Does the one-judgment-per-call merge across static and taint layers need anything beyond
   intersecting the verdict spaces?
6. Is a per-turn review budget resolving at fallback outcomes the right cost bound, or should
   exhaustion escalate to a human instead?
7. For the browser action reviewer, is objective-plus-envelope-plus-fenced-environment the complete
   trusted-context set, or should recent verdicts in the same session feed back in?

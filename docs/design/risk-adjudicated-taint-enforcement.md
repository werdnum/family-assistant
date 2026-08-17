# Risk-Adjudicated Taint Enforcement

## Status

Proposed. Research review and design direction, for discussion before any implementation. Builds on
the operational findings in [PR #1111](https://github.com/werdnum/family-assistant/pull/1111)
(`docs/design/runtime-taint-enforcement-operational-findings.md`, which lands with that PR and is
not yet on this branch's base); PR #1111 remains the prerequisite work. This document addresses the
strategic question that PR #1111 deliberately did not: whether the enforcement model itself — a
context-free tier-times-sink matrix whose only outcomes are allow, confirm, and deny — is the right
shape, given what has been learned in production and what the rest of the industry has shipped since
the taint design was written.

## Summary

Runtime taint enforcement has stalled in `observe` mode because the policy's false-positive cost is
structural, not incidental. The matrix decides from two inputs — the turn's maximum source tier and
the sink class — and cannot see the one thing that separates a benign action from an exfiltration:
whether the action serves the user's stated request. A week of production audit data showed 75% of
policy evaluations would require confirmation under enforcement, dominated by ordinary `get_note` /
`search_calendar_events` reads after any external content. No amount of matrix tuning fixes this,
because the discriminating signal is absent from the inputs.

The industry has converged on a different decomposition in the past year. Claude Code's auto mode
and OpenAI Codex's approval policies both split enforcement into a **deterministic floor** (hard
rules and sandbox boundaries that no model judgment can relax) plus a **judgment layer** (a
classifier that sees the user's own words and the executable payload — and nothing the attacker
wrote — and adjudicates the middle of the risk spectrum). Anthropic reports the two-stage classifier
at a 0.4% false-positive rate on real traffic while catching 89% of dangerous commands in controlled
testing, against 13.6% for interactive human review — confirmation fatigue is not just annoying, it
is *less safe* than a well-built classifier.

This document proposes adapting that decomposition to Family Assistant's existing taint machinery:

- Add an `adjudicate` outcome to the taint matrix, sitting between `audit` and `confirm` in the
  strictness lattice. Cells that today would confirm-fatigue the operator become `adjudicate`.
- An adjudicator — a small, fast model invocation outside the agent loop — decides `allow`,
  `confirm` (escalate to human), or `deny_and_continue` per gated call. It sees only trusted-tier
  conversation content (deterministically selected using the provenance we already store), the tool
  call, and a provenance digest. Untrusted content never argues its own case.
- The worst cells keep a deterministic floor the adjudicator cannot relax, consistent with the
  existing tighten-only clamp philosophy. In those cells the adjudicator only chooses *between*
  confirm and deny; skipping confirmation requires a prior human approval bound to the same
  capability — tool, destination, and payload scope — never a model verdict. **Argument provenance
  matching** — whether a destination provably originates from trusted-tier content — informs the
  judge and the confirmation UX but relaxes nothing.
- Denials become **deny-and-continue**: a structured tool result the model can route around, with
  human escalation after repeated blocks, instead of a hard error.
- An **escalate-only injection probe** screens untrusted content at ingestion and can only raise
  scrutiny, never lower it.

The intended end state: the taint machinery keeps doing what deterministic systems are good at —
provenance, routing, and floors — and stops doing what they are bad at: guessing intent. Measured
against the production audit data, the expected interactive friction drops from ~200 would-confirm
events per day to an explicit budget of roughly one confirmation per day, while the exfiltration
sinks gain protection they do not have today (observe mode blocks nothing). Crucially, most of the
mechanism in this document is **contingent**: a lean core (floors, sink corrections, standing grants
— see the complexity-budget section) ships first and may already meet the budget, in which case the
adjudicator and everything after it stays unbuilt.

## Why enforcement actually stalled

The friction documented in [taint-history-epoch-amnesty.md](taint-history-epoch-amnesty.md) and PR
#1111 has three distinct causes, and only two of them are fixable within the current model:

1. **Ambient taint floors.** Legacy history poison (addressed by `history_taint_epoch`) and
   prompt-included high-tier notes plus skill-catalog metadata (addressed by PR #1111's
   prompt-admission rule). These are bugs in taint *hygiene*, not in the enforcement model, and
   their fixes should land regardless of anything in this document.
2. **Uncalibrated tiers and tags.** The middle tiers (`known_contact`, `recognized_machine`) have
   never fired in production because the sender allowlists are empty, and several pure-transform
   tools are tagged `OUTPUT_UNTRUSTED`. Also fixable in place.
3. **Context-free policy.** After both fixes, a genuinely mixed turn — "summarize this email and add
   the dentist appointment to the calendar" — still gates every sensitive read and every write
   through the same outcome, because `unknown_external` content is legitimately present. The matrix
   cannot distinguish that turn from "email tells the agent to forward the calendar to a stranger."
   Both are `unknown_external × sensitive_read_broadening` followed by egress. The signal that
   separates them is *alignment between the action and the trusted user's request*, which is not an
   input to the evaluator. This is the structural residue that keeps `enforce` unshippable even
   after PR #1111, and it is the part this document addresses.

The project already named the failure mode before it happened: the taint design warns that a
too-coarse policy "trains rubber-stamping and makes ambient ingestion unusable"
([runtime-taint-machinery.md](runtime-taint-machinery.md)), and the 2026-07 assessment recommended
"audit the middle of the risk spectrum instead of confirm-gating it." Adjudication is the upgrade of
that "audit" from *record and hope* to *decide, cheaply, with the right inputs*.

## Threat model check

Is the threat worth any of this? An honest accounting for a single-family deployment:

- **Targeted attacks are unlikely; commodity attacks are rising.** Nobody is spearphishing this
  household's assistant today, but injection payloads in ordinary email, calendar invites, and web
  pages are now commodity — sprayed, not aimed — and the deployment ingests email and browses.
- **Model-level resistance is real but not a boundary.** Current frontier models resist known
  injection patterns far better than the models the taint design was written against; Anthropic's
  adversarial training reports on the order of 1% attack success for recent Claude models, and its
  auto-mode evaluation reports 0 of 720 attack attempts succeeding against the full stack. Vendors
  themselves say the residual is meaningful, and the "Attacker Moves Second" line of work showed
  adaptive attacks bypassing 12 published defenses at >90% — so improving base rates justify
  *thinner* deterministic floors, not their removal.
- **The valuable deterministic wins are cheap here.** The deployment's genuinely dangerous sinks are
  few and mostly typed already: `attacker_addressable_egress`, `arbitrary_external_message`,
  `sandbox_network` — with one known mis-typing, high-impact home actuation (locks, alarm panels)
  currently hiding inside the always-allowed `home_local`, which the proposal splits out.
  `send_message_to_user` already server-validates recipients (`KNOWN_USER_COMM`), which is exactly
  the capability-style constraint that makes a sink safe by construction. Keeping hard floors on a
  handful of cells costs almost no friction because benign traffic rarely lands there — the
  production audit shows the friction lives in the *middle* cells.
- **The realistic loss is bounded but not trivial:** exfiltration of household notes, calendar, and
  mail; unwanted outbound messages; home actuation. Worth defending; not worth an unusable
  assistant. A defense the operator disables protects nothing — PR #1111's framing, and the
  governing constraint for everything below.

## State of the art, briefly

Surveyed March–August 2026; references at the end.

**Provable architectures (CaMeL, FIDES).** DeepMind's CaMeL extracts control flow from the trusted
query alone and runs it in a custom interpreter, attaching capabilities to every value; untrusted
data can never alter what the program does, and policies gate tool calls on data provenance. It
solves 77% of AgentDojo tasks with provable guarantees versus 84% undefended. FIDES propagates
integrity/confidentiality labels with deterministic invariants. These are the gold standard for
security properties and a poor fit for retrofit onto a conversational assistant: they require the
task to be expressible as a plan derived before untrusted data arrives, which is the opposite of a
chat loop. The transferable ideas are **value-level provenance** (we already store it on attachments
and artifacts) and **argument-origin checks** (adopted below), not the interpreter.

**Judgment layers in production (Claude Code auto mode).** Auto mode's decomposition, per
Anthropic's engineering writeup: a tiered funnel (allowlisted safe tools → in-project file writes
auto-permitted because git makes them reviewable → only real-risk actions reach the classifier); a
two-stage classifier (single-token screen at 8.5% FPR, reasoning pass on flags dropping it to 0.4%);
hard block categories (destroy/exfiltrate, degrade security, cross trust boundaries, bypass review)
that are rules, not judgment; and — the structurally important part — the classifier sees **only
user messages and the executable payload**. Assistant prose and tool outputs are stripped, so an
injected payload cannot lobby the judge; it must produce a tool call that looks benign *in
isolation* against the user's actual words. Blocks are survivable: the agent gets a denial
explanation and finds another path, with human escalation after 3 consecutive or 20 total blocks.
Published operating points: 0.4% FPR on 10k real actions, 17% FNR on a hand-curated overeager set,
and the human-comparison numbers above.

**Independent dials (OpenAI Codex).** Codex separates *what is technically possible* (sandbox mode:
read-only / workspace-write / full, network off by default) from *when to ask* (approval policy:
untrusted / on-request / never). The two compose; neither substitutes for the other. Family
Assistant's analog is profile capability confinement (static tool policy, the actual live gate
today) versus the runtime taint layer — the design should keep them orthogonal rather than merging
them.

**Input screening probes.** Auto mode also runs a server-side injection probe over tool outputs
before they enter context, adding a warning when hostile content is detected. Classifier-based
screens (PromptGuard-class models, Gemini's native detection — already enabled on our computer-use
path) measurably cut attack success but degrade badly under adaptive attack (>85% bypass reported
for state-of-the-art preprocessors). The honest role for a probe is **escalate-only**: a detection
raises scrutiny; the absence of a detection proves nothing and must not relax anything.

**What does not work.** System-prompt instructions as a security boundary; output filtering alone;
fine-tuned resistance alone; any single probabilistic layer an adaptive attacker can study. Simon
Willison's critique of auto mode — "prompt injection protections relying on AI are non-deterministic
by nature" — is the right constraint to design under: probabilistic layers may *reduce* expected
loss in the middle of the risk spectrum, but the tails must be held by deterministic floors and
capability boundaries.

The synthesis every serious deployment has landed on is the same layering: deterministic capability
floors at the edges, model judgment in the middle, probes as tripwires, and human confirmation
reserved for the rare escalations — because human attention is the scarcest resource in the system
and burns out fastest.

## Design principles

1. **Provenance stays deterministic.** Taint tracking, tier derivation, sink typing, and audit are
   already built and are the part CaMeL-class systems prove valuable. Nothing probabilistic ever
   *writes* provenance.
2. **Judgment never relaxes a floor.** The adjudicator operates only where the matrix delegates to
   it, and in floor cells may only tighten — the same lattice discipline the evaluator already
   enforces against profiles.
3. **The attacker never addresses the judge.** Adjudicator context is assembled deterministically
   from trusted-tier sources; untrusted content appears only as the payload being judged, inside
   fenced data boundaries.
4. **Fail closed, degrade to today.** Adjudicator unavailable or erroring ⇒ the cell behaves as
   `confirm`. The system without its judgment layer is the system as currently designed, never
   something weaker.
5. **Friction is a budget, not a vibe.** Enforcement ships against an explicit target measured in
   approval episodes per week, using the audit pipeline that already exists.

## Complexity budget: the lean core, and everything else on evidence

Every mechanism in this document is a liability as well as a defense: more moving parts, more
interactions, more edge cases for a future change to break without noticing. The review history of
this very document demonstrates the failure mode — repeated rounds of hole-patching on the clever
parts, while the simple invariants never needed a patch. Two rules follow.

**Additive mechanism is gated on measured need; substitutive mechanism is preferred.** Some
proposals here *replace* complexity that already exists in its most dangerous form — the
someone-must-remember form. Standing grants replace bespoke per-automation profiles (the pattern
that cost ~2,100 lines for one cron job). Proven provenance replaces per-store trust audits. Closed
schemas replace filter enumeration. Those earn their place by subtraction. Purely additive mechanism
— the adjudicator, the probe, artifact healing — must instead be justified by a measured number, not
an anticipated one.

**The lean core ships first, and may be enough.** The production friction data was dominated by
ambient poison (fixed by PR #1111 and the epoch) and by confirm-gating sensitive *reads* (fixed by
policy choice: audit the reads, gate the egress — the loss event is what leaves, not what is read).
After both corrections, the residual confirm volume is turns that contain genuinely external content
*and* attempt egress, actuation, destruction, or executable persistence. In a household that is
plausibly a handful of episodes per week, not 200 per day. The lean core is therefore: PR #1111 and
the epoch; the sink corrections (home actuation split, destructive-write split,
executable-persistence split, calendar de-trusting, tag hygiene); a matrix of confirm floors on
egress, actuation, destructive writes, and executable-artifact creation with audit everywhere else;
task-scoped standing grants; then flip to `enforce` and measure. Only if the measured confirm volume
exceeds the friction budget does the contingent tier get built — the adjudicator first, and only for
the cells the data indicts; probe, content-derived stamping, and healing on the same terms. The good
outcome is that most of the contingent tier is never built.

The sections below therefore describe two kinds of material: the lean core, and a fully designed
contingent tier that exists on paper so that, if the numbers demand it, it is built coherently
rather than improvised — but whose default disposition is *unbuilt*.

## Proposed design

### The `adjudicate` outcome (contingent tier; floors are lean core)

In the lean core, the cells shown as `adjudicate` below are simply `audit`, and the cells shown with
a `confirm` floor are plain `confirm` — the floors ship first, the judge only if the friction gate
trips. Everything in this section describes the contingent upgrade.

Extend `TaintPolicyOutcome` with `adjudicate`, parameterized by a **verdict floor** — the weakest
verdict the adjudicator may return in that cell. Two distinct comparisons need two distinct ranks,
and conflating them breaks the merge in both directions:

- **Configured-outcome rank** (tighten-only profile merging):
  `merge_rank(adjudicate(F)) = max(rank between audit and confirm, rank(F))`. A bare `adjudicate`
  therefore ranks strictly *above* `audit` — a profile cannot replace it with `audit` and skip the
  judge (ranking it at its `allow` floor would permit exactly that weakening) — while replacing a
  base `audit` cell with any `adjudicate` is accepted as the tightening it is, and
  `adjudicate(floor: confirm)` ranks at `confirm`.
- **Verdict-floor rank** (verdict bounding, `operator_minimum`, `require_taint_enforcement`): the
  floor itself, so `adjudicate(floor: confirm)` satisfies a `confirm` minimum and a bare
  `adjudicate`'s verdicts are bounded only by its own floor. Evaluation order matters: matrix cell →
  adjudicator verdict (bounded below by the cell's floor) → `operator_minimum` applied to the
  *verdict*. Running adjudication before the operator clamp is what lets the evaluator hand
  `adjudicate` to the provider at all — applying `operator_minimum` first, as `evaluate()` does
  today, would convert the cell to `confirm` before the judge ever ran. Applying it after keeps its
  semantics exactly as strong as today: an operator-configured minimum can never be weakened by a
  judge verdict, and never by argument provenance matching either. In `observe` mode the evaluator
  still returns `adjudicate` and the provider still invokes the judge — only the verdict's *effect*
  is downgraded to `audit`, so it is logged and nothing blocks. Downgrading the outcome itself
  before the provider saw it would silently skip the judge and leave nothing to calibrate;
  preserving the outcome while suppressing enforcement is what gives the free shadow phase: real
  verdicts against the exact traffic that stalled the rollout, at zero user-visible cost. A
  deployment that wants observe mode without judge latency or spend can set the cell to plain
  `audit` explicitly.

The default matrix change, relative to [runtime-taint-machinery.md](runtime-taint-machinery.md)'s
shipped defaults:

| max_tier × sink                                  | today (would-be)        | proposed                              |
| ------------------------------------------------ | ----------------------- | ------------------------------------- |
| `unknown_external × sensitive_read_broadening`   | confirm                 | adjudicate                            |
| `unknown_external × known_user_message`          | confirm                 | adjudicate                            |
| `unknown_external × artifact_write`              | audit                   | adjudicate                            |
| `unknown_external × arbitrary_external_message`  | confirm                 | adjudicate, floor: confirm            |
| `unknown_external × attacker_addressable_egress` | confirm                 | adjudicate, floor: confirm            |
| `unknown_external × sandbox_network`             | deny                    | per PR #1111: confirm                 |
| external tiers × high-impact home actuation      | allow (in `home_local`) | adjudicate, floor: confirm (new sink) |
| middle tiers × egress sinks                      | confirm                 | adjudicate, floor: confirm            |
| middle tiers, non-egress gated cells             | audit/confirm           | adjudicate                            |

Every externally authored tier keeps a `confirm` floor on the egress sinks
(`arbitrary_external_message`, `attacker_addressable_egress`, `sandbox_network`): a DMARC-passing
allowlisted sender is still an external author — a compromised family mailbox or a hostile
newsletter supplies attacker-controlled input at a friendlier tier — so a classifier false-negative
must never be able to authorize outbound disclosure on its own at *any* external tier. Floorless
adjudication is reserved for the noisy non-egress cells, where the floors downstream still hold.
What the middle tiers buy is a gentler experience everywhere else, not a softer exfiltration path;
and since those tiers have never fired in production (empty allowlists), keeping their egress floors
costs nothing observed today.

Egress is not the only irreversible sink hiding in a soft cell. `call_home_assistant_action`
classifies `lock`, `alarm_control_panel`, `valve`, and `siren` as `home_local`, and the default
matrix allows `home_local` at every tier — so today (and under the table above, unamended) an
injected email in an ordinary mixed turn can unlock a door with no gate at all; the existing
configuration reference already warns operators about exactly this cell. The sink resolver already
switches on the `domain` argument, so the fix is a resolver split, not new machinery: high-impact
actuation domains (locks, alarm panels, valves, sirens, garage-class covers) move to their own sink
class with `adjudicate(floor: confirm)` at every externally authored tier, while ordinary
`home_local` (lights, media, climate) stays `allow`. Physical-safety actuation gets the same rule as
egress: a classifier false-negative must never be able to do it alone.

Destructive artifact writes are the third occupant of a soft cell. The resolver maps deletes and
rewrites (`delete_note`, `delete_script`, `delete_automation`, calendar deletion) into
`artifact_write`, which the lean core sets to `audit` — so an injected email in a mixed turn could
permanently destroy household data with no gate; the configuration reference already warns that
"`delete_note` runs unconfirmed on that path." Tools already carry a `DESTRUCTIVE` tag
(`email_intake` denies on it), so this too is a resolver split rather than new machinery:
`DESTRUCTIVE`-tagged calls resolve to a `destructive_artifact_write` sink with a `confirm` floor at
every externally authored tier, joining egress and actuation in the lean core's floored set. Static
tool policy already confirms calendar deletes profile-wide; the taint floor extends the same
protection to the rest of the destructive surface, but only on tainted turns.

Executable persistence is the fourth floored family, and the sneakiest: `create_automation`,
`update_automation`, script saves, and `wake_llm`-style scheduled callbacks resolve to
`artifact_write` today, yet what they store is *future execution* — and `task_worker.py`
reconstructs a fired automation as a system trigger with no memory of the creation turn's taint, so
an injected email could plant a callback under an audit-only cell and have its payload run later,
outside every floor, in a clean-looking turn. Creation of executable or scheduled artifacts
therefore joins the floored set (`confirm` at externally authored tiers). Keying this on the
incidental existing tags is not enough — `schedule_future_callback` carries only `STATE_CHANGING`
and `SCHEDULING`, so an `AUTOMATION`/`STATE_PERSISTING` key would miss one-time callbacks, which are
executable persistence all the same. The class gets its own explicit tag (`EXECUTABLE_PERSISTENCE`,
on `create_automation`, `update_automation`, script saves, `schedule_future_callback`,
`schedule_action`, `modify_pending_callback`), and — because a tag list is exactly the kind of
enumeration that rots — a conformance rule ties it to the mechanism: a tool whose implementation
enqueues an LLM-waking task must carry the tag, checked by the existing ast-grep conformance
machinery, so a future scheduling tool fails lint rather than silently joining the audit-only cell.
The durable fix, which belongs with the artifact-provenance work in the contingent tier, is the same
rule delegation runs already follow: persist the creation turn's taint on the automation and seed
every later firing with it until a human attests the automation — persistence must never launder
taint through time. The floor is the lean-core stopgap that makes the laundering impossible before
that machinery exists.

The tag alone does not draw the line correctly, because destruction hides in overwrites too:
`add_or_update_note` *replaces* an existing note's content when not appending, and
`modify_calendar_event` replaces event fields — neither carries `DESTRUCTIVE`. Gating them per call
is possible (the resolver already switches on arguments for Home Assistant domains), but the cheaper
and substitutive fix is to make overwrites *actually reversible*: retain the prior revision on
replace at the repository layer, so an overwrite is an additive operation with an undo rather than a
destruction. Then the floored class is genuinely "operations that cannot be undone," the tag stays
honest, and truly additive writes (`add_or_update_note` with revision retention, event creation)
stay `audit` — reversible, visible, and the overwhelmingly common case. Where revision retention is
impractical for a store, that store's overwrite operations resolve to the destructive sink by
argument instead.

"Floor: confirm" means the adjudicator's verdict space in that cell is {confirm, deny} — it chooses
how hard to gate, never whether to gate, with no exceptions: no model verdict, probe result, or
provenance computation ever adds `allow` to a floor cell. The only path past a floor-cell
confirmation is a prior *human* approval covering the same capability — and capability means the
full tuple PR #1111 enumerates (tool/operation, destination, payload or data scope), never the
destination alone. Destination-only binding would let an approved benign message to X authorize a
later, materially different payload to X composed under injected instructions. Default reuse scope
is therefore the exact tool-and-argument fingerprint: retries and concurrent duplicates coalesce
into one confirmation; anything else re-confirms. A broader grant — "further messages to X for the
rest of this task" — is an explicit human choice that the confirmation UI states plainly, is bounded
to the task, and is suspended for probe-labeled turns. That is positive authorization by an
authenticated decision, not by mention. And because `operator_minimum` applies after adjudication,
an operator minimum can only ever tighten a floor-cell verdict further.

The two cells that dominated the production friction data (`sensitive_read_broadening`,
`known_user_message` — i.e. reading notes/calendar and messaging the household after external
content entered the turn) become adjudicated with a full verdict space, because their benign rate is
overwhelming and their worst case is bounded: `known_user_message` reaches only server-validated
household recipients, and a sensitive read only *broadens exposure* — actual loss still requires a
subsequent egress call, which is exactly where the floors sit. This is defense in depth used
deliberately: soften the cheap, noisy, inner gate because the outer gate is hard.

### Task-scoped standing grants (lean core)

Authorization should attach to human-authored intent at whatever altitude that intent actually
exists. Interactive requests have per-call judgment and confirmation. Ambient always-on ingestion
has no covering intent, so it gets structural confinement. Between them sits the altitude the
current system cannot express at all: the recurring workflow the operator authored once. A scheduled
task's creation *is* an attended human decision with full context; nothing captures it, so today
such tasks either fail closed (confirm with no channel) or demand a bespoke profile.

A **standing grant** is that captured decision: attached to the task definition, granted at creation
through trusted chrome, revoked with the task, absent-fails-closed. It names the full capability
tuple — tool/operation, destination, payload scope, rate — and the chokepoint enforces **every
field** of that envelope; calls inside it proceed unattended, calls outside fall back to today's
behavior. This requires a dedicated serialized grant record. The existing
`TurnTaintState.approved_sinks` is precedent for how approvals serialize and travel across
delegation, but it must not be the implementation: it is a `frozenset[str]` of sink-class names, and
`is_sink_approved()` admits *every* call in the class — a grant for one operation would authorize
unrelated operations sharing its sink. The grant record carries the tuple explicitly and the
chokepoint validates tool, destination, payload conformance, and rate per call, denying on any field
mismatch.

Worked example — the error-triage automation ("scan error logs nightly, file issues for real
problems"): an engineer-profile scheduled task whose grant is `create_github_issue`, this repository
only, three per day, **server-rendered body**. The repository is public, so a free-form issue body
is genuine egress twice over — an exfiltration channel for injected content and a privacy leak for
log excerpts on a perfectly clean run. Schema *typing* alone does not close that: a string field
named `component` is still free-form if the model fills it, and an injected log entry could encode
whatever it likes there. So the model's authority shrinks to **selection**: it names the error group
to file (by id), and the server renders the public body entirely from the referenced record —
component from the known-component enum, exception class as parsed from the log record, fingerprint
computed server-side as a hash, counts and timestamps from the store. No model-composed string
reaches the public body at all, which is what makes it a `low_bandwidth_external` sink by
construction (the model's channel is its choice among error groups — a few bits). Free-form detail
(stack traces, log excerpts) goes to a private artifact the issue references. Free-form text
crossing a trust boundary is where both injection and exfiltration live; server-derived data
crossing it is boring in both directions — and "typed" must always cash out as *derived or validated
against the bounded source*, never as "a string field with a reassuring name." The same grant shape
covers interactive browsing sessions (an origin-scoped grant confirmed once at session start), which
is what keeps browser workflows to one confirmation per task instead of one per navigation.

### The adjudicator (contingent tier)

A dedicated, non-agentic model invocation — one call, structured verdict, no tools — run from
`TaintTrackingToolsProvider` when evaluation yields `adjudicate`. Configured like a processing
profile (provider, model, retry) but it is not a profile: it has no tool surface, no context
providers, and its prompt is assembled by code, not configuration.

**Inputs, assembled deterministically — as a closed schema, not a filtered stream.** The judge's
context is a typed structure whose fields are enumerated here exhaustively; a field is either
closed-vocabulary or carries a provenance proof, and nothing outside the schema can render by
construction. This is deliberately an allow-list posture: the review history of this document shows
that specifying what to *filter out* loses — every round found another unfiltered channel
(arguments, then reasons, then titles, then tool descriptions). A future field leaks only if someone
affirmatively adds it to the schema, and one property test — no rendered string without a trust
proof — holds structurally forever.

- Trusted-tier conversation content, current turn included — selected **by taint metadata, never by
  message role**. Message position confers nothing: in email intake the sender-controlled email body
  arrives as trigger content represented as a `UserMessage`, so "render the current turn's user
  messages" would hand the judge the attacker's email verbatim. The same per-part tier selection
  applies uniformly — rows and trigger parts whose stored tier is `trusted_user` render; anything
  else is summarized as a one-line provenance stub ("\<tool result from `gmail_get_message`, tier
  unknown_external>", "\<email trigger, sender unverified, tier unknown_external>"). The attacker's
  text is *represented* to the judge but never *rendered* to it.
- The tool call: name, resolved sink class, tool description — rendered only for local tools, whose
  descriptions are deployment-controlled; an MCP tool's description is remote-server content
  (`_format_mcp_definitions_to_dicts` copies it verbatim), so MCP tools render as server id plus
  annotation-derived tags instead — and a **provenance-filtered rendering of the arguments**.
  Structural fields — destinations, recipients, entity ids, enums, numbers, short identifiers —
  render inside fenced boundaries after validating against their declared grammar (a URL must parse
  as a URL, an address as an address, identifiers within length caps); a value that fails its
  grammar is stubbed like free-form text. Grammar validation bounds the channel but does not close
  it — an attacker-influenced URL is still attacker-chosen text, and the judge must see it to judge
  it; that irreducible exposure is shared with auto mode's classifier and is why the judge's
  template treats every rendered value as data, never as instruction. A free-form value renders
  verbatim only when it provably originates from trusted-tier content (the same machinery as
  argument provenance matching below, minus its stricter request scoping — rendering trusted text is
  safe wherever in the conversation it came from); otherwise it is replaced by a stub carrying its
  provenance, length, and content type ("body: 4 kB, derived from `gmail_get_message`
  (unknown_external) and three `get_note` reads"). Attacker-authored prose never renders to the
  judge.
- A provenance digest of the turn: which sources are present, their tiers, types, and reasons — the
  `TurnTaintState.sources` tuple we already carry — with digest fields tier-filtered like everything
  else. `source_type`, tier, labels, sink classes, and tool names are closed-vocabulary and render
  verbatim; free-text fields — `reason`, artifact titles, sender addresses, arbitrary identifiers —
  render only for trusted-tier sources and are replaced by type-and-tier stubs otherwise, because
  reasons already interpolate artifact-controlled text today (a tainted note's title reaches
  `TaintSource.reason` via the note tools' provenance merging). The digest includes the temporal
  fields (`fresh_high_taint_seen_at_sequence`, `sensitive_reads`) that are currently recorded but
  consumed by nothing. Ordering matters and the judge should see it: a sensitive read *after* fresh
  untrusted content is the dangerous shape; the same read before it is not. Sensitive-read records
  obey the same schema discipline as everything else: the judge sees kind, tool name, and sequence
  position — closed fields — while free-text qualifiers are stubbed unless provenance-proven,
  because qualifiers already embed derived text today (`SensitiveReadScope.qualifier` stores the raw
  `search:{query}` string, and a search query composed from an email is that email's author
  speaking). Today the temporal fields do not survive the metadata round trip —
  `TurnTaintState.to_metadata()` omits `sensitive_reads`, and `from_metadata()` does not restore
  `fresh_high_taint_seen_at_sequence` — so an adjudicator in a delegated run would see no ordering
  evidence. Extending the serialized schema with the temporal records and sequence, with merge
  semantics that continue the parent's sequence monotonically across the delegation boundary, is
  part of implementing this input.
- The profile id and the matrix cell that delegated the decision, including its floor.

**Verdicts:** `allow` (with one-line reason, audited), `confirm` (escalate to the existing durable
confirmation machinery, judge's reason included in the rendered prompt so the human sees *why*),
`deny_and_continue` (structured refusal tool result). Malformed output, timeout, or provider error ⇒
`confirm`. Every verdict writes a `taint_audit_events` row with the verdict, reason, and latency;
the existing diagnostics endpoint grows verdict counts.

**The judge is itself an injection target, so it never reads the attacker.** The naive design — show
the judge the full arguments and rely on fencing — would hand untrusted natural language a second
audience whose verdict gates exactly the sinks it wants opened; boundary neutralization (the
`_neutralize_untrusted_evidence_boundaries` pattern) stops forged tags, not persuasion. Hence the
filtered rendering above: the judge decides on structure and alignment — does this destination,
recipient, capability, and scope follow from what the trusted user asked? — plus provenance stubs
whose signal survives redaction: a 14 kB free-form body assembled from a dozen `get_note` reads and
headed for an external destination is exactly as alarming as its contents, without reading them. An
injection must therefore produce a call whose *structure* is benign relative to what the user
actually asked — at which point it has been constrained into approximately the actions the user
wanted anyway. The accepted cost is that the judge cannot do content-level payload inspection; that
job belongs to the escalate-only probe at ingestion (below), not to the gate.

**Cost and latency.** Production audit data: 337 would-gate calls over 7 days (~48/day). At one
flash-class call each, cost is negligible; latency lands only on gated calls, not the hot path. A
two-stage screen (single-token fast pass, reasoning pass on flags) is an optimization to add only if
verdict quality or latency demands it.

### Argument provenance matching (contingent tier)

A deterministic computation, not a policy relaxation: whether the destination-bearing argument of an
egress call — URL, address, recipient, entity id — is an exact whole-value match
(post-normalization) for a destination in the **current request's** trusted-tier user messages. "The
user pasted this URL in the request I am executing" is checkable without model judgment. An earlier
revision of this design let a passing match unlock `allow` inside floor cells; that is withdrawn,
because mention is not authorization — a request can name a destination while *forbidding* it
("never send anything to attacker@example.com"), and no string-level check can tell the difference.
Floor cells therefore never soften on a match (see above); positive authorization is only ever a
human approval bound to the destination.

What the match is for:

- **Judge input.** "Destination appears verbatim in the current trusted request" versus "destination
  appears nowhere in anything the user wrote" is a strong, deterministic, audit-loggable signal for
  the adjudicator in non-floor cells and for verdict *reasons* everywhere.
- **Rendering.** The same machinery decides which argument values may render verbatim to the judge
  (trusted-tier origin) versus as stubs.
- **Approval scoping.** The normalized destination value supplies the destination component of the
  reuse fingerprint (tool/operation + destination + payload scope, per the floor-cell rules above) —
  whole-value matching (not substring containment) prevents an attacker smuggling exfiltrated data
  around an approved destination, e.g. query parameters appended to an approved URL, while the
  payload component prevents an approved benign payload from covering a different one.

This keeps CaMeL's value-provenance insight at the altitude this codebase can afford — per-argument,
per-sink, exact-match, not per-token information flow (a stated non-goal in PR #1111, and still one)
— while leaving every floor deterministic. The existing `_merge_argument_taint_into_context`
machinery, which already resolves attachment ids in arguments against stored provenance, is the
precedent and likely the home for it. Sink resolvers declare which argument is destination-bearing;
a sink with no declaration gets no match, so its floor-cell confirmations never coalesce.

### Deny-and-continue (contingent tier)

Today a taint denial raises `ToolPolicyDeniedError` and the turn effectively dead-ends; under
enforcement this converts injected *and* false-positive gates alike into failed tasks, which is the
friction mode that gets systems turned off. Adopt auto mode's semantics: a denied or judge-rejected
call returns a structured tool result stating what was blocked, why, and what safer route exists
("ask the user", "use the household recipient", "drop the external destination"). The model
continues. Escalation counters — 3 consecutive gated-and-rejected calls, or a configured per-turn
total — convert to a single human confirmation summarizing the pattern, or end the turn with an
explanation in unattended contexts. Counters live in `TurnTaintState`, which already threads through
the loop and serializes across delegation.

Hard `deny` floors (unattended `sandbox_network`, malformed-payload refusals from
`confirmation_payload_block_reason`) keep raising: those exist precisely so no continuation pressure
erodes them.

### Escalate-only injection probe (contingent tier)

At the two ingestion chokepoints that already exist — `derive_tool_result_taint_source` for
`OUTPUT_UNTRUSTED` tool results, and email intake — run a cheap injection screen (options:
PromptGuard-class local model, the Gemini detector we already enable for computer use, or a
flash-model check; selection is an implementation detail). A detection:

- adds a `suspected_injection` provenance label to the source (labels already exist on
  `TaintSource`),
- injects an auto-mode-style advisory into context ("the following content attempted to issue
  instructions; anchor on the user's request"), and
- hardens adjudicated cells for labeled turns: bare `adjudicate` gains a `confirm` floor, and
  *broad* grants ("further messages to X for this task") are suspended for the labeled turn —
  mechanically, an escalate-only `matrix_overrides` variant selected by label. `confirm` cells stay
  `confirm`: the human sees the probe's warning in the confirmation prompt rather than the probe
  manufacturing a denial, because a fallible detector must not become an availability boundary —
  otherwise a sender who *wants* to jam the assistant just includes an obvious injection phrase.

The same jamming logic bounds the confirmation side. Exact-fingerprint coalescing survives probe
labeling (identical concurrent calls collapsing into one card never authorizes anything new), and
after K interactive confirmation requests on a labeled turn, further probe-induced escalations
**defer rather than deny**: each remaining gated call becomes a deferred durable confirmation — the
single-call records and deferred-execution path email intake already uses, surfaced together in the
existing pending-confirmations tray rather than interrupting live. This is deliberately *not* a
coalesced multi-item approval card: `confirmation_requests` stores one tool call per record, which
is the right granularity anyway, since each deferred call must stay individually decidable. A true
batch card would need new schema, UI, and executor, and is at most a contingent UX refinement if
tray volume ever warrants it. Conversion to denial would make the probe the sole reason a valid
workflow fails, which is exactly the availability boundary it must never become; deferral keeps the
invariant intact while capping *live* interruptions at K. Per-turn confirmation counts on labeled
turns are a standing metric so the bound is measured rather than assumed.

No probe verdict ever relaxes anything, so its adaptive-attack failure mode (missing a novel
payload) degrades to exactly the system without a probe; no probe verdict ever denies or causes a
denial on its own; and no probe false positive can raise more than K live interruptions in a turn.
Cheap detections of commodity spray attacks get deterministic hardening plus a visible audit trail.

### Persistent artifacts: content-derived provenance and attested healing

*(Tier split: the store corrections below — calendar de-trusting, the externally-mutable-store audit
question — are lean core; the stamping, healing, attribution, and attestation mechanics are
contingent tier.)*

Notes and other stored artifacts are the second engine of the production friction, and in the audit
data they fired *first*: the two poisoned prompt-included notes put every turn at `unknown_external`
before any email or web page was touched, and one of them had been re-stamped from the other — a
pure artifact-to-artifact feedback loop. PR #1111's prompt-admission rule governs where high-tier
artifacts may *land* (ambient context); this section addresses how artifacts *acquire* taint, why
they never lose it, and how to shrink both without weakening what artifact provenance is for.

Four mechanisms combine into the current behavior:

1. **Whole-turn stamping.** A write persists the *turn's* maximum tier, not the *content's* origin.
   An unrelated web search earlier in the turn poisons an evergreen preference note the user
   dictated verbatim.
2. **Sticky provenance.** Clean-turn edits preserve earlier stored provenance — deliberately, since
   new content may derive from old — so one poisoned write is permanent absent deletion.
3. **Global restore-on-read.** `get_note` (and listing, and prompt inclusion) merges stored
   provenance into the turn, raising `max_tier` for everything after it. The escalation is correct
   in direction but undiscriminating in effect: a grocery list saved from a recipe site months ago
   gates the turn exactly like this morning's unread stranger email.
4. **Destroyed attribution.** `to_metadata()` truncates to twelve sources and round-trips the rest
   as anonymous `manual` entries, which is why the epoch-amnesty design found production attribution
   "already destroyed" — per-artifact forensics or amnesty cannot be reconstructed after the fact.

The fixes follow the same philosophy as the rest of this design — deterministic provenance,
human-only trust promotion, judgment only where it is cheap to be wrong:

**Stamp writes from content, not turns.** The artifact-write path (`add_or_update_note`,
`workspace_write`, document ingestion) runs the same trusted-tier matching built for the judge's
rendering filter over the *content argument*: content that provably originates from the current
turn's trusted-tier text is stamped `trusted_user` even in a tainted turn; content that matches a
specific untrusted source inherits *that source's* provenance, specifically rather than anonymously;
content that matches nothing — model-composed, paraphrased, or derived from stored artifacts — falls
back to today's turn-maximum stamp. The fallback is what makes this safe against laundering: a model
paraphrase of an injected email fails the verbatim match and stays high-tier, so an attacker cannot
wash content by asking the model to reword it. What the rule kills is exactly the observed
false-positive class — collateral stamping of user-dictated content by unrelated taint, including
the note-to-note feedback loop, because editing note B never matches tainted note A's content unless
it actually copies it. Writes through the authenticated Notes UI (no model in the loop) remain
trusted by construction, and stay the zero-friction path.

**Heal per revision, deterministically.** Sticky provenance exists because an edit may derive from
the tainted prior content — but derivation is checkable by the same matcher. If a clean-turn edit
fully re-authors the content (new content matches current trusted-tier text, no overlap with the
stored tainted content beyond trivial length), the revision's provenance replaces rather than
merges. If any part fails the match, stickiness applies as today. Frequently edited evergreen notes
heal through normal use instead of needing repeated review.

**Make attestation the amnesty.** PR #1111's content-hash-bound review clears an artifact for
*ambient* use; extend the same authenticated operation to rewrite stored provenance to
`trusted_user` for the attested revision, so an explicit `get_note` of a reviewed note stops
re-tainting turns too. Same invariants: bound to the canonical hash of every prompt-visible field,
set only by an authenticated human through trusted chrome, invalidated by any change, untouchable by
model-influenced write paths. Human attestation — not tier decay, not age, not a model verdict — is
the only way stored provenance ever moves toward trusted.

**Persist attribution losslessly.** Move artifact and row provenance to structured, deduplicated
source records (an artifact-provenance table referenced by id, rather than twelve inline truncated
sources). This is what keeps every other mechanism honest: per-artifact amnesty, the feedback-loop
diagnosis, and the judge's provenance digest all need attribution that survives storage. The
epoch-amnesty postmortem is the cautionary tale — by the time the policy questions were asked, the
data needed to answer them had been rounded away.

**Calendar events are artifacts too — and today they launder.** `search_calendar_events` is tagged
`OUTPUT_TRUSTED`, which rests entirely on its write paths being gated: the events themselves can be
externally authored (an emailed invitation's title and description, confirm-approved into the
calendar by a human during email intake). A confirmation attests what the human read in the rendered
prompt, but the stored text is still attacker-composed, and reading it back later contributes no
provenance at all — externally authored prose enters an otherwise trusted turn with no label, no
advisory, and no matrix consequence. This is the one write-path artifact class with *no* provenance
story, where notes have a partial one. The fix is the same mechanism, not a special case: calendar
writes stamp per-event provenance exactly as note writes do (content-derived, turn-maximum
fallback), reads restore it the way note reads restore theirs, and the ingestion probe's screening
of the original email is remembered by that provenance rather than needing a second probe at read
time. One property of the calendar makes the fallback rule permanent rather than transitional: the
store is live CalDAV, and organizers, other calendar clients, and server sync write to it without
ever passing Family Assistant's write path, so there will always be events no stamp ever covered and
events whose content changed after stamping. Per-event provenance must therefore be bound to a
content hash of the prompt-visible fields, and every read must treat an event with absent provenance
— or a hash that no longer matches the live CalDAV content — as untrusted, permanently, not as a
migration interim. An externally created event the household actually trusts can be promoted the
same way as any artifact: human attestation through the review mechanism, invalidated on the next
external edit. The current blanket `OUTPUT_TRUSTED` is an assumption the write gates do not actually
support; the same audit applies to any other stored surface read back as trusted — workspace files,
automations, script bodies — with the same question asked of each: can anything other than a gated
Family Assistant write path mutate this store?

**Let adjudication absorb the rest at read time.** Restored artifact taint stops being expensive
once the middle cells are adjudicated: reading a tainted note no longer cascades into blanket
confirmations, because the judge sees *which* artifact contributed the taint — id, tier, age, review
state, all closed-vocabulary or tier-filtered fields — and weighs it against the user's request.
`max_tier` monotonicity and the egress floors are unchanged; a turn that read a tainted note still
cannot reach an unapproved external destination without a human. The impact reduction is in the
noise, not the tails.

### Calibration work that stays as-is

Unchanged from PR #1111 and prior docs, restated as prerequisites: prompt-admission control for
high-tier ambient artifacts; `history_taint_epoch` set on production; middle-tier sender allowlists
populated from authenticated connector evidence; `OUTPUT_UNTRUSTED` tag hygiene for pure-transform
tools; the Trino descriptor investigation; capability-scoped confirmation reuse (judge verdicts
reduce how often confirmations occur; reuse scoping governs how long one lasts — complementary, not
competing).

### `require_taint_enforcement` semantics

The Gmail/Drive registration floor currently demands `mode: enforce` plus ≥`confirm` on four sink
classes. Update the check so an `adjudicate` cell satisfies it iff the cell carries a `confirm`
floor (the two egress cells, `sandbox_network` per PR #1111's confirm-with-fail-closed) or is
`sensitive_read_broadening` (whose protection is the downstream egress floor, per the
defense-in-depth argument above). Without this, shipping adjudication would silently keep
Gmail/Drive unregistered — the same "appears configured, actually inert" trap PR #1111 flagged for
`operator_minimum`.

## What we deliberately do not build

- **A CaMeL-style planner/interpreter.** Wrong altitude for a chat assistant; the 7-point utility
  cost lands on every turn, not just risky ones.
- **Per-token or sentence-level information flow.** Still a non-goal; argument provenance matching
  is the bounded substitute.
- **A probe that gates by itself.** Probabilistic detection only escalates.
- **Judge authority over floors.** The lattice clamp applies to the adjudicator exactly as it does
  to profiles.
- **Tier decay / taint expiry.** The judge consumes the temporal fields instead; `max_tier`
  monotonicity stays, and thread healing remains the epoch's and prompt-window's job.
- **A second confirmation surface.** Adjudicator escalations flow into the existing durable
  confirmation machinery; PR #1111's single-merged-prompt requirement covers the new source too.

## Friction budget and measurement

Enforcement ships against numbers, not vibes, using the audit pipeline that exists:

- **Budget:** ≤ 1 interactive confirmation per day (p50 over a rolling 30 days), ≤ 3 p95, across the
  household. From the 30-day audit baseline (2,122 would-gate calls, 383 gated turns), the
  adjudicator must absorb effectively all of the middle-cell volume for this to hold — which is the
  point of building it.
- **Shadow-phase gates before `enforce`:** judge false-allow rate ≈ 0 on a replayed set of known
  injection shapes seeded through email intake (the red-team fixtures already exist in the test
  suite's email corpus, extended as needed); false-escalate rate low enough to fit the budget
  against 30 days of real traffic; p95 judge latency within a bound that doesn't visibly stall
  turns.
- **Standing metrics:** verdict counts by cell and by profile; escalation-counter trips;
  deny-and-continue recovery rate (did the turn still complete?); probe detections and their
  dispositions; confirmations per week. All countable from `taint_audit_events` plus the PR #1111
  observability additions; same privacy constraints (no raw content or destinations in aggregates).

## Rollout sequence

The lean core, in order, each phase independently shippable:

1. **Land PR #1111** (prompt admission, sandbox confirm, `operator_minimum` fix, epoch set on
   production). Re-run the audit so later decisions calibrate against traffic without ambient
   floors.
2. **Sink and tag corrections:** high-impact home actuation split out of `home_local`;
   `DESTRUCTIVE`-tagged writes and executable/scheduled artifact creation split out of
   `artifact_write`; calendar (and any other externally mutable store) de-trusted; middle-tier
   allowlists; tag hygiene; Trino fix. Config-level work that removes the known fail-open cells.
3. **Standing grants**, minimal form: grant storage on task definitions and browse sessions,
   envelope enforcement at the existing chokepoint, schema-constrained bodies for public sinks.
4. **Enforce the lean matrix:** confirm floors on egress and actuation, audit everywhere else,
   updated `require_taint_enforcement`. Gmail/Drive tools register for the first time.
5. **Measure against the friction budget for 30 days.**

**The gate:** if the measured confirm volume fits the budget and no household task category became
unusable — stop. The design is complete at phase 5 and the contingent tier stays on paper. Only if
the numbers exceed the budget does the contingent tier proceed, smallest-first and only for the
cells the data indicts:

6. **Adjudicator in shadow**, implemented *with its complete input contract* — the closed context
   schema, trusted-row selection, argument rendering filter, provenance-digest tier-filtering, and
   the matching machinery those filters depend on. A judge without its filter reads attacker prose,
   and a judge retrofitted with it later is a different judge than the one calibrated. Run under
   `observe`, evaluate against the shadow-phase gates, then enforce with deny-and-continue and
   escalation counters.
7. **Content-derived artifact provenance** (stamping, healing, lossless attribution, attestation
   extension) if artifact-restored taint is what the measurements indict.
8. **Capability-scoped confirmation reuse** beyond the minimal grant form, and the **injection
   probe**, escalate-only, once there is an enforcement layer for it to harden.

## Acceptance criteria

- With enforcement on, the confirmation budget holds over 30 days of real traffic, and no task
  category the household actually uses (email triage, browsing, notes, calendar, home control)
  becomes unusable — the operator-frustration test, stated as a requirement.
- Replayed injection fixtures attempting egress of note/calendar/email content are blocked or
  escalated in 100% of runs at the floor cells, independent of adjudicator verdicts and of any
  provenance-match result — the floors do this unconditionally; the judge is not load-bearing for
  the tails. The fixture set includes negation cases ("never send to X" in the current request,
  injection targeting X — must still confirm), data-smuggling variants around an approved
  destination (query parameters appended to an approved URL — must not reuse the approval), and
  payload substitution against a default-scope approval (a different payload to an approved
  destination — must re-confirm unless the human explicitly granted a task-bounded broader scope).
- An adjudicator outage degrades every `adjudicate` cell to `confirm`, visibly in diagnostics —
  never to `allow`.
- No code path allows probe output or judge output to lower a tier, remove a source, relax a floor,
  or write provenance.
- Judge context provably excludes untrusted-tier rendered content — in conversation rows, argument
  values, and provenance-digest fields (reasons, titles, identifiers) alike (unit-testable via the
  same row-selection and field-filtering functions the assembler uses).
- Adjudication in a delegated run sees the same temporal evidence (sensitive-read records,
  fresh-taint ordering) as it would in the parent turn: the serialized taint schema carries it
  across the delegation round trip.
- An artifact written from content that verbatim-matches the current turn's trusted-tier text
  carries trusted provenance even in a tainted turn; a model paraphrase of untrusted content fails
  the match and keeps the turn-maximum stamp — laundering by rewording is impossible by
  construction.
- Stored artifact provenance moves toward trusted only through deterministic content-derived
  stamping, deterministic revision-scoped healing, or an authenticated human attestation — never
  through a model verdict, and attribution survives storage without truncation.
- Every verdict, escalation, and probe detection is auditable after the fact with reasons.

## References

- Runtime taint machinery, epoch amnesty, and operational findings: this repo,
  `docs/design/runtime-taint-machinery.md`, `docs/design/taint-history-epoch-amnesty.md`, PR #1111.
- Claude Code auto mode: announcement and engineering writeup
  (https://claude.com/blog/auto-mode-default-in-claude-code,
  https://anthropic.com/engineering/claude-code-auto-mode); Simon Willison's critique
  (https://simonwillison.net/2026/Mar/24/auto-mode-for-claude-code/).
- OpenAI Codex approvals and sandboxing
  (https://developers.openai.com/codex/agent-approvals-security).
- CaMeL: "Defeating Prompt Injections by Design" (https://arxiv.org/abs/2503.18813); Willison's
  analysis (https://simonwillison.net/2025/Apr/11/camel/).
- Meta's Rule of Two (https://ai.meta.com/blog/practical-ai-agent-security/), already the basis of
  `AGENTS.md`'s security section.
- Survey of 2026 defenses and adaptive-attack results, including FIDES, MELON, LlamaFirewall, and
  "Attacker Moves Second"
  (https://zylos.ai/research/2026-04-12-indirect-prompt-injection-defenses-agents-untrusted-content/).

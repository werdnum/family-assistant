# Executable Definition Taint: Judgment at the Creation Chokepoint

## Status

Proposed. This design closes the remaining gap named in
[auto-tool-call-review.md](auto-tool-call-review.md)'s input contract and unattended-callback
sections: automation and script definitions persist no taint provenance, so every unattended
callback — schedule automations, event listeners, reminders, `schedule_future_callback`, script
wakes, script failures — enters its turn at `unknown_external` and renders to the tool-call reviewer
as a stub. It supplies the "artifact-provenance work [that] stamps automation definitions at their
authoring chokepoint" which that document defers to, and amends one invariant of
[risk-adjudicated-taint-enforcement.md](risk-adjudicated-taint-enforcement.md) — scoped, argued, and
bounded below.

It is complementary to [automation_provenance.md](automation_provenance.md), which records *whose
authority* an automation runs with (creating profile and user). This design records *whose words* it
runs — the trust tier of the content that became the definition, and whether its creation was judged
or confirmed.

## Problem

Automation and script definitions are stored intent: a human (or a model acting for one) writes an
instruction today that wakes an agent tomorrow, with no human present. Two things about the current
system interact badly with that shape:

1. **Definitions carry no taint provenance.** `schedule_automations` and `event_listeners` persist
   only creator identity (`processing_profile_id`, `created_by_user_id`); the `scripts` table
   persists nothing at all. `_llm_callback_review_trigger` therefore hard-codes
   `definition_taint_metadata=None`, the trusted-definition escape in
   `_llm_callback_trigger_taint_sources` is unreachable, and every unattended callback enters at
   `unknown_external` with the `unattended_callback` label.

2. **The firing turn has no renderable intent.** The reviewer's input contract renders a trigger
   definition only when stored provenance proves `trusted_user`; absent provenance fails closed to a
   stub. So on exactly the turns with no human to ask, the judge sees a stub where the intent should
   be. `confirm` and `deny` collapse into deferral or denial, and the reviewer works only from
   operator guidance and the delegating rule.

The consequences compound. A reminder scheduled from a perfectly clean interactive turn delivers
through `send_message_to_user` in a turn the machinery cannot distinguish from an attacker-triggered
one — which is why the enforce-migration note in auto-tool-call-review.md has to call out reminder
delivery deferring for confirmation under the literal pin. The `unknown_external` trigger row and
its taint-derived assistant rows persist in history, so later interactive turns reloading that
history inherit the elevated tier. And under enforcement, an automation authored in *any* tainted
turn is permanently gated: its firings can never render trusted intent, so anything its cell gates
defers or dies, forever, with no human in the loop to break the tie.

Delegations had the same blanked-intent problem and solved it by *carrying the intent forward*:
`build_delegation_review_trigger` propagates the delegating turn's human-authored rows, under their
own stored provenance, so the reviewer can tell a faithful delegation from a smuggled one. That
solution does not transfer directly to automations, for two reasons. The crossing is separated by
time — the authoring conversation is long gone when the firing runs — so anything carried forward
must be persisted, not read live. And when the authoring turn was tainted, there may be nothing
trustworthy to carry: the definition itself is the intent, and its trustworthiness is precisely what
is in question.

## Principle

**Intent is judged where it is visible; where propagation would destroy it, persist the boundary
decision instead of the taint.**

The creation of an executable definition is a guarded crossing — the risk document already places
`executable_persistence` in the gated set precisely because a stored definition is future execution.
At that crossing, everything the judgment needs exists: the authoring turn's trusted rows, the full
definition payload, the provenance digest, the destination echo. At firing time, none of it does.
Propagating the authoring turn's taint into every firing therefore does not preserve the judgment —
it forfeits it, re-asking a question at the one place it cannot be answered. Re-judging the same
immutable content at every firing spends unboundedly to decide, with strictly worse evidence, what
was already decidable once.

So the creation is the chokepoint, exactly as the delegation boundary is for delegations: the
exposure decision is governed at the boundary the definition crosses, never re-litigated inside the
blind firing. What crosses the boundary is recorded — the authoring stamp, the content hash, and the
disposition of the gate that let it through — and firing-time behaviour is a deterministic function
of that record. A definition that passed the gate fires with its intent intact; the gate's judgment
has *cured* the authoring taint for the definition's future firings, rather than the taint
propagating through time. A definition that never passed a real gate keeps today's fail-closed
behaviour in full.

## Design

Each executable definition carries a **definition record** with three parts:

1. **Authoring stamp** (deterministic): the authoring turn's taint metadata, written at every
   executable-persistence write, in the same shape notes already use (`provenance_metadata_json` /
   `_note_provenance_from_taint`). Writes through the authenticated web UI, with no model in the
   loop, stamp `trusted_user` by construction — the zero-friction path, as for notes.

2. **Content hash**: a hash of the definition's executable fields — the instruction/context text,
   script code, match conditions, recurrence rule, action config; whatever determines what runs and
   what the agent is told. The stamp and disposition are bound to it; a mismatch at resolution time
   voids the record and fails closed. This is the same rule the risk document imposes on CalDAV
   events, applied here as defence in depth: automations live in FA's own store, but a record that
   survives content it no longer describes is a laundering primitive whatever the mutation path.

3. **Creation disposition** (the cure): how the write left the gate. One of:

   - `clean` — the authoring turn held no externally authored taint; no gate engaged.
   - `human_confirmed` — a durable confirmation approved the write. For the approval to carry this
     disposition, the confirmation must have rendered the definition's executable fields in full; a
     confirmation that cannot render them does not cure (mirroring the reviewer contract's rule that
     an unrenderable payload is refused rather than truncated). A sighted human approval of the full
     definition is attestation in the risk document's sense, bound to the hash it approved.
   - `judge_allowed` — the tool-call reviewer returned `allow` for the write under `enforce`, from
     whichever layer gated it (an `adjudicate` taint cell or a static `review` rule). The verdict id
     stays in `taint_audit_events` as the audit anchor.
   - absent — legacy rows, hash mismatch, or a write that no real gate examined.

   The disposition is written by the existing confirmation and adjudication chokepoints themselves,
   not by the automation tools — whichever layer gated the write records how it resolved, so a new
   write path or a new gating layer gets the behaviour by construction.

### Firing-time resolution

`_llm_callback_review_trigger` and the script-execution seeding path stop hard-coding
`definition_taint_metadata=None` and instead resolve the definition record:

| record state                                  | definition renders to reviewer as        | trigger taint contribution |
| --------------------------------------------- | ---------------------------------------- | -------------------------- |
| stamp `trusted_user`, hash valid              | trusted definition                       | none                       |
| tainted stamp + `human_confirmed`, hash valid | trusted definition, marked attested      | none                       |
| tainted stamp + `judge_allowed`, hash valid   | trusted definition, marked judge-allowed | none                       |
| tainted stamp, no curing disposition          | stub (today)                             | `unknown_external` (today) |
| absent record or hash mismatch                | stub (today)                             | `unknown_external` (today) |

"Marked" means the review-status vocabulary (`clean` / `attested` / `judge-allowed at creation`,
plus creator identity) renders as closed-vocabulary context alongside the definition, so the
firing-time reviewer always knows whether it is weighing a human's sighted approval or a prior
machine verdict. The definition renders in the *definition* field, never in `originating_request`:
that field remains reserved for literal human-authored rows, per the delegation design's laundering
rule — a definition is usually model-composed even in a clean turn, and must not occupy the slot
that claims to be the human's own words.

Everything else about the firing is unchanged:

- **The trigger payload stays untrusted, always.** Event data, script failure output, and wake
  contexts contribute their own taint sources exactly as today; the cure applies to the definition
  only. A trusted definition with a payload present enters the turn tainted *by the payload* while
  still rendering its intent — which is the correct split, and the one that stops `confirm` and
  `deny` collapsing: the judge finally has something to judge alignment against. (Per-source payload
  tiering — e.g. Home Assistant events at `recognized_machine` — is orthogonal and unchanged.)
- **Downstream taint is unchanged.** A cured firing that reads fresh untrusted content re-taints
  normally; every gated sink in the firing still gates per its own cell. The cure restores the
  baseline the definition would have had if authored clean; it exempts nothing after that.
- **Capability provenance is unchanged.** Profile resolution, `allow_wake_llm`, and the fail-loud
  rules of automation_provenance.md are untouched.
- **The destination echo** extends to firings only through human-backed dispositions: a definition
  whose record resolves via a `trusted_user` stamp or `human_confirmed` supplies echo-eligible text
  in place of the (absent) current trusted request; a `judge_allowed` definition does not — its text
  is model-composed and machine-vetted, strong enough to render, not strong enough to count as the
  user's own words. The echo remains a signal, never a bypass, either way.

### What cures, and what deliberately does not

Only a disposition that **could have blocked the write** cures. An `allow` verdict recorded in
`observe` mode, or a write through a cell configured `audit`, records nothing curative: in those
postures the gate was not real, so its output cannot stand in for one. This makes the cure and the
gate turn on together — a deployment in `observe` feels neither the creation gate nor firing-time
enforcement, and the day it flips to `enforce`, tainted-authored definitions start being gated *and*
start being curable in the same motion. (Clean-authored and web-UI definitions — the overwhelming
majority — resolve trusted in every mode, which is where most of this design's value lands:
reminders, clean-turn schedules, and dashboard-created listeners stop entering as `unknown_external`
immediately.)

The strength of the cure inherits the creation cell's configuration, with no new dial. Under this
repository's shipped judge-forward defaults, a tainted-turn creation adjudicates with a full verdict
space, so the judge can cure. An operator who applies the recommended hardening set — a `confirm`
floor on executable persistence at externally authored tiers, per the risk document — thereby makes
every cure human-backed, because tainted creations can then only pass through confirmation. "Who may
make stored intent trusted" is exactly the question the creation cell's strictness already answers;
a separate curing knob would be a second place to configure the same decision.

Mutation re-enters the chokepoint. `update_automation` and script saves re-stamp, re-hash, and
re-gate: a tainted update of a clean automation produces a new record whose disposition reflects the
new gate, and the old record cannot survive it (the hash changed). `enable_automation` /
`disable_automation` change no content, so a valid record survives them — but the enable call itself
remains an executable-persistence action gated in its own turn, per the risk document's activation
rule. A definition mutated outside the write path resolves as hash-mismatch and fails closed.

### Stored scripts and the executable closure

An automation may reference a stored script by name, and the script body can change after the
automation was judged. Cross-artifact hashes would rot; instead, each artifact carries its own
record and **resolution walks the executable closure**: a firing resolves the record of every
definition artifact whose content it is about to execute or render — the automation and any stored
script it references — and the weakest resolution governs the whole firing. Editing a shared script
from a tainted turn re-gates the script itself, and every automation referencing it inherits the new
state at its next firing, un-cured until the script's own gate cures it. This mirrors the
re-validation rule automation_provenance.md already applies to stored-script capability.

### One-shot callbacks

`schedule_reminder`, `schedule_future_callback`, and `schedule_action` have no durable definition
table; their definition is the enqueued payload. The stamp, hash, and disposition ride the task
payload alongside the existing `tool_call_review_trigger_definition` fields, snapshotted at enqueue
from the live tracker and the gate that admitted the call. Follow-up reminders re-enqueued by the
task worker carry the original record forward unchanged — the content is the same content.

### The stamping chokepoint

All executable-persistence writes route through one shared stamping helper, and a conformance rule
makes it a chokepoint rather than an enumeration: any code path that sets
`tool_call_review_trigger_definition` on a task payload, or writes a definition table's executable
fields, must obtain the record from the helper — checked by the ast-grep conformance machinery,
alongside the risk document's planned `EXECUTABLE_PERSISTENCE` tag rule. A future scheduling tool
that forgets fails lint, degrading availability (visible failure) rather than safety (a stampless
definition would merely stay `unknown_external`, but silently, and the gap would re-open unnoticed).

## The amended invariant

The risk document's design principle — nothing probabilistic ever writes provenance, lowers a tier,
or persists a verdict as trust — is amended for exactly one artifact class, executable definitions,
in exactly one direction:

- **What is preserved:** no verdict ever rewrites the authoring stamp. The stored provenance remains
  the deterministic record of what authored the definition, forensics intact; `judge_allowed` is an
  additive record beside it, not a mutation of it. No verdict touches notes, calendar events, or any
  other artifact class. No verdict relaxes a configured floor — a floored creation cell excludes
  `allow`, and with it the judge's ability to cure, by the same configuration that excludes it from
  executing.
- **What is amended:** a persisted enforce-mode `allow` verdict on the *creation* is consulted, by a
  deterministic resolution function, when seeding the definition's firings — a verdict persisting as
  trust, for this class, bounded by the hash and revocable by any content change.

The amendment is justified by an asymmetry unique to this class. For every other artifact, un-cured
taint degrades to *judgment at use time with full context*: the turn that reads a tainted note has a
live request, a reviewer with rendered intent, and possibly a human. For executable definitions,
un-cured taint degrades to *judgment with nothing* — an unattended turn whose intent is blanked,
where `confirm` has no one to ask and the reviewer has nothing to weigh, so gating collapses toward
permanent deferral or denial. That is not "stricter"; it is the gate-without-a-satisfiable-path
shape that PR #1121 showed gets turned off and then protects nothing. Where judgment at use is
impossible, judgment at creation is not a convenience — it is the only place the question can be
asked, and the invariant's purpose (verdicts must not silently become durable authority) is served
instead by the bounds above: additive record, hash binding, floor-inherited strength,
close-vocabulary marking at every consultation, and the audit anchor to the creating verdict.

## Alternatives considered

- **Propagate-through with human attestation as the only cure** (the risk document's original
  durable fix). Kept as the universal fallback — it is precisely the "no curing disposition" row —
  but rejected as the only mechanism: it re-litigates immutable content at every firing with
  strictly worse evidence; under enforcement it makes every automation authored in a mixed turn
  (summarize-my-email turns are routinely mixed) permanently gated in contexts with no human; and it
  funnels all legitimacy through an attestation ceremony that the creation-time confirmation already
  performs with the same hash-bound rigor.
- **Per-firing adjudication of the definition.** Unbounded spend on a constant input, verdict
  flapping across firings of identical content, and strictly less evidence than the creation turn
  held. The firing-time reviewer still runs — on the firing's *tool calls*, where fresh evidence
  actually exists — which is the right division of labour.
- **Creator-identity trust** (treat definitions created by trusted profiles/users as trusted).
  Already rejected by auto-tool-call-review.md: identity does not capture what the authoring turn
  had read, which is the entire question.
- **Persisting the authoring turn's user rows onto the definition** (full delegation-style
  originating-request propagation). Deliberately not done: the creation-time gate is where
  request-versus-definition alignment is checked, a stored automation legitimately outlives its
  conversational context, and stale rows rendered years later would mislead more than they inform.
  Recorded as a deliberate simplification; the delegation machinery remains the pattern for live
  crossings.

## Security properties

- No firing ever enters trusted on identity, age, or absence of information: only a valid-hash
  record whose stamp is `trusted_user` or whose disposition is a real gate's `allow`/approval
  resolves trusted, and every other state — legacy, mismatch, uncured — is exactly today's
  fail-closed behaviour.
- Nothing probabilistic writes or rewrites stored provenance; the authoring stamp is append-only
  history, and the cure is an additive, hash-bound, auditable record consulted deterministically.
- The trigger payload never launders: no record state renders payload content or suppresses its
  taint source.
- A cured firing gains only its baseline: every sink its turn reaches is still gated by the same
  cells, against a reviewer that now has intent to judge with.
- An operator floor on the creation cell simultaneously governs execution and cure eligibility —
  tighten-only, with no second surface to misconfigure.
- Definition text never occupies the originating-request field, and judge-cured text never feeds the
  destination echo.

## Accepted residuals

- **A reviewer false-negative at creation plants a definition whose firings run at a clean
  baseline.** The headline trade, and deliberately the same class as the existing "false-negative on
  an unfloored egress cell" residual: the creation review sees the full definition fenced with the
  authoring turn's trusted context — the judge's best operating point — and the firings' own sinks
  remain gated. Operators for whom this is unacceptable apply the executable-persistence `confirm`
  floor, which makes every cure human-backed.
- **Observe-mode deployments accumulate tainted-authored definitions with no cures.** Also no
  enforcement friction, so nothing is worse than today; on the flip to `enforce`, those definitions
  gate until touched, re-gated, or attested — the migration section's paths apply.
- **Whole-definition stamping.** A mixed authoring turn stamps the whole definition at the turn
  maximum even if the human dictated the instruction verbatim; content-derived per-field stamping
  remains the risk document's contingent-tier refinement and composes here unchanged (it would
  upgrade some stamps to `clean`, reducing how often the cure is needed at all).
- **The closure walk covers stored artifacts, not conversation.** A definition that instructs the
  agent to read some other artifact at fire time gets no special treatment; whatever it reads
  contributes taint the ordinary way.

## Migration

Legacy definitions have no record and keep today's behaviour untouched. Three paths forward, all
existing shapes:

1. **Touch.** The next update through any write path stamps, hashes, and gates the definition in the
   updating turn.
2. **Attest.** A hash-bound human review surface for automations, listeners, and stored scripts —
   the same authenticated operation the risk document defines for notes and calendar events,
   generalized to this artifact class. A household's automation inventory is small; reviewing it
   once is proportionate, and the surface doubles as the standing answer for definitions authored
   under `observe`.
3. **Recreate.** For one-shot callbacks in flight, nothing: they expire on firing, and newly
   scheduled ones carry records from day one.

The enforce-migration note in auto-tool-call-review.md simplifies once this lands: reminder delivery
from clean turns no longer trips the `known_user_message` pin, so the reminder-compatible-exception
paragraph can be retired for deployments that adopt this design before flipping to `enforce`.

## Work plan

Each milestone is a PR-sized, independently shippable unit; construction detail (column names, hash
canonicalization, payload field names) belongs to the PRs.

**M1 — Definition records at rest.** Stamping helper; provenance-plus-hash storage on
`schedule_automations`, `event_listeners`, and `scripts`; payload fields for one-shot callbacks;
web-UI trusted-by-construction stamps; the conformance rule. No behaviour change at firing time.
*Verify:* round-trip tests per artifact; a clean-turn creation stamps `trusted_user` and a mixed
turn stamps the turn maximum; the conformance rule fails on a fixture write path that skips the
helper.

**M2 — Firing-time resolution for deterministic stamps.** `_llm_callback_review_trigger` and the
script-execution seeding resolve records; `trusted_user`-stamped, hash-valid definitions render and
contribute no trigger taint; the payload/definition taint split; the closure walk. *Verify:* a
reminder scheduled in a clean turn delivers without an `unknown_external` source; the same reminder
scheduled in a tainted turn still enters tainted; a hash-mismatched definition stubs and taints as
today; an event firing with a trusted definition and a payload renders intent while carrying payload
taint; an automation referencing a script re-saved in a tainted turn resolves un-cured.

**M3 — The cure.** Dispositions written at the confirmation and adjudication chokepoints
(enforce-mode only for verdicts; full-payload rendering required for confirmations); resolution
honours `human_confirmed` and `judge_allowed`; review-status vocabulary in the reviewer rendering;
echo eligibility rules. Depends on the risk document's executable-persistence sink split for the
adjudicated path; the confirmation path works wherever a gate confirms today. *Verify:* a
human-confirmed tainted creation fires clean and renders marked attested; a judge-allowed creation
under `enforce` fires clean and renders marked judge-allowed; an observe-mode `allow` verdict does
not cure; a floored cell yields only human-backed cures; no test path rewrites an authoring stamp.

**M4 — Attestation surface and documentation.** The hash-bound review operation for the three
artifact classes, in the web UI; user documentation for how automations become trusted;
`CONFIGURATION_REFERENCE.md` gains the interaction with the executable-persistence floor and the
simplified enforce-migration note. *Verify:* attesting a legacy automation makes its next firing
resolve trusted; any content change invalidates the attestation; docs build.

## Review questions

1. Is `judge_allowed` as a curing disposition acceptable under the shipped floorless defaults, or
   should the shipped default require `human_confirmed` for the cure even where the judge may
   `allow` the write itself (splitting execution authority from curing authority, at the cost of the
   second dial this design argues against)?
2. Should the disposition record the *delegating layer* (taint cell vs. static rule) and require a
   taint-layer gate specifically, or is any enforce-mode gate on the write sufficient — as designed
   — given that static confirmations render the same payload?
3. Is the closure-walk weakest-link rule right for stored scripts, or should an automation
   referencing a script pin the script's hash at automation-gate time (tighter, but re-creates the
   cross-artifact staleness this design avoids)?
4. Is whole-definition stamping acceptable for v1, with per-field content-derived stamping left to
   the contingent tier?
5. Should follow-up reminders re-enqueued by the task worker really inherit the original record
   unchanged, or does each re-enqueue deserve a fresh gate evaluation?

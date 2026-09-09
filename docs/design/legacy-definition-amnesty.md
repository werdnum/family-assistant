# Legacy Definition Amnesty: Restamping Artifacts That Predate Provenance

## Status

Proposed. Companion to [executable-definition-taint.md](executable-definition-taint.md), whose
migration section names three paths for definitions written before that design shipped — touch,
attest, recreate — and to [taint-history-epoch-amnesty.md](taint-history-epoch-amnesty.md), which
solved the same shape of problem for message history. This design supplies the bulk path the
automation estate needs, in the vocabulary the definition record already has.

## Problem

An executable definition written before definition records shipped carries no record. Resolution
reads that state exactly as it reads a hash mismatch or a denied verdict: unresolved, fail-closed.
Every firing of such an automation, listener, or stored script therefore renders a stub to the
tool-call reviewer and seeds its turn at `unknown_external` with the `unattended_callback` label —
permanently, because nothing about a legacy definition changes with time.

That is the correct default for an unknown definition and the wrong steady state for a deployment
whose entire automation estate predates the feature. The consequence is the one the history-epoch
work already diagnosed at a different layer: the observed taint state is dominated by absence of
metadata rather than by untrusted content, every unattended turn needs confirmation, and the
operator learns to rubber-stamp. The three existing paths do not disperse it — touch requires
editing definitions nobody wants to edit, recreate discards their execution history, and attest, as
a per-definition review surface, is the right instrument for a handful of definitions rather than
for the whole inventory at once.

The other durable artifact classes are already amnestied by construction and are deliberately out of
scope. A note or attachment carrying no stored provenance contributes *no* taint
(`artifact_taint_sources` returns nothing for absent provenance, and note read-back merges only what
is stored), because an artifact produced from untrusted input is labelled where it is created and
defaulting the unlabelled case to `unknown_external` would taint every ordinary upload. Executable
definitions are the one class that fails closed on absence, because for them absence means "no gate
ever examined future execution" — which is exactly the fact this design is about, and exactly why
they cannot simply adopt the notes rule.

## Why not a read-time epoch

The obvious symmetry is the history epoch: a config timestamp, and definitions older than it read as
trusted without touching stored rows. It is rejected for this artifact class, for reasons that do
not apply to history rows.

- **Nothing binds the amnesty to content.** A history row is immutable; a definition is not. A
  read-time rule keyed on a row's age blesses whatever that row contains *at the firing*, so any
  path that mutates executable content while leaving the record absent silently widens the amnesty
  to content the operator never had in mind. Binding the amnesty to a hash of the content as it
  stood when the operator granted it makes a later mutation void the amnesty and fail closed, which
  is the property the definition record exists to provide.
- **The available timestamps are the wrong ones.** Automations and listeners record `created_at` and
  no content-mutation timestamp, so an age test cannot distinguish a definition untouched since the
  epoch from one edited after it.
- **Amnesty would be invisible where the decision matters.** A firing-time reviewer weighing a
  definition must know whether it reads a human's sighted approval, a machine verdict, or an
  unexamined legacy artifact. A record says so in the closed vocabulary the reviewer already
  renders; a config key applied at read time does not travel with the definition at all.

A migration that fabricates *clean* stamps for legacy rows is rejected for a plainer reason: it
destroys the fact it is migrating. After it, nothing distinguishes a definition authored in a
verified-clean turn from one whose authoring nobody ever knew, and no rollback exists.

## Principle

**Amnesty is a decision about specific content, recorded where every other decision about that
content is recorded.**

The definition record already expresses "who authored this" separately from "how the gate that let
it through resolved". Legacy definitions are precisely the case where the first is unknown and the
second never happened. So the operator's amnesty is written as what it is: an honest
`unknown_external` authoring stamp — the authoring turn is genuinely unknown and no fabricated
provenance replaces it — bound to a hash of the content amnestied, carrying a disposition that says
an operator, not a gate, let this through. Resolution then needs no new rule: the existing cure path
turns a curing disposition on a tainted stamp into the clean machine-authored baseline, and the
existing hash check voids the whole thing the moment the content changes.

## Design

### The disposition

A new creation disposition, `legacy_amnestied`, joins the two that cure. It is written only by the
operator-run restamp described below, and its gate provenance records the amnesty as its own layer,
so the flip-time backlog listing separates operator-amnestied definitions from judge-cured and
human-confirmed ones without inspecting anything else.

It cures, and it claims nothing more:

- It never renders as human attestation, so an amnestied definition never feeds the destination
  echo. Only text a human typed or sighted in full does, and by construction nobody sighted this.
- The reviewer's closed status vocabulary gains a value naming it for what it is — amnestied by the
  operator, examined by no gate — so a firing-time reviewer weighing an amnestied definition is
  never told it was judged.
- In an executable closure it is the weakest claim: an automation naming an amnestied script
  describes itself as amnestied, whatever its own record says.

### The restamp

An operator-run, dry-run-by-default command walks the three durable definition classes — schedule
automations, event listeners, and stored scripts — and reports every definition whose record is
absent and whose row predates a cutoff the operator states explicitly. Applying it writes a record
per definition through the same stamping chokepoint every write path uses, over the content as read
in the writing transaction.

Four rules keep it from becoming a laundering primitive:

- **It fills absence only.** A definition that already holds a record — cured, uncured, or void
  through a hash mismatch — is never touched. A mismatch in particular is content that changed under
  a real record, which is the one case the fail-closed default exists for; amnestying it would
  reward the mutation.
- **The cutoff is stated, never defaulted.** The operator names the instant before which a
  definition counts as legacy — in practice, when definition records were deployed — and nothing
  newer is eligible. A definition written after that instant with no record is a write-path
  regression, which the existing conformance rule and the fail-closed default are there to surface,
  not something to bless in bulk.
- **The grant is bound to content.** The record hashes the definition as it stood at the restamp, so
  any later edit voids the amnesty and re-enters the ordinary creation gate.
- **It is reversible.** The same command revokes: it clears records carrying the amnesty disposition
  and nothing else, restoring the pre-restamp fail-closed state exactly. That is the rollback
  property config-only mechanisms get for free and a fabricating migration cannot offer at all.

One-shot callbacks in flight — reminders, future callbacks, one-shot script actions — are
deliberately out of scope, as in the companion design: their records ride an enqueued payload, they
expire on firing, and the population is self-clearing within the deployment's scheduling horizon.

## Security properties

- Amnesty is granted only by an operator with database access, per definition, over content named in
  a dry run first.
- No stamp is fabricated as trusted: the authoring stamp records `unknown_external` with a label
  saying the definition predates stamping, and the cure — not the stamp — is what resolves the
  firing. Nothing probabilistic writes or rewrites provenance, unchanged from the companion design.
- No existing record is overwritten, so no judge verdict, human confirmation, or genuinely tainted
  stamp can be laundered by running the restamp, however often it runs.
- A hash mismatch never becomes eligible, so content mutated outside the write path stays
  fail-closed.
- An amnestied definition gains only its baseline: every sink its firings reach is gated by the same
  cells as before, against a reviewer that now knows both the intent and that no gate vouched for
  it.
- Amnesty never claims a human's words: no destination echo, no originating-request slot, and a
  distinct review status.

## Accepted residuals

- **A latent injection in the pre-cutoff estate is amnestied along with everything else.** This is
  the same operator-accepted trade the history epoch documents, narrowed: the population is a
  household's automation inventory rather than a year of message rows, the operator sees it
  enumerated before applying, the grant is per-definition and revocable, and the firing's own sinks
  stay gated. An operator who wants each definition read before it is trusted uses the
  per-definition attestation surface instead; this exists because "read every one" and "trust none
  of them forever" are both worse answers for an estate that predates the feature.
- **Amnesty records the gate that never ran.** An amnestied definition keeps curing after the
  operator later hardens the executable-persistence cell, exactly as a judge-cured one does, and for
  the same reason: resolution is a pure function of the stored record, not of live configuration.
  The remedy is the same procedural one — the dispositions are listable, so hardening comes with a
  filterable review, and revocation is one command.
- **Whole-estate granularity is the operator's to narrow.** The command filters by artifact class
  and by name so a cautious operator can amnesty in batches, but nothing forces that; an operator
  who applies it to everything has accepted the bullet above for everything.

## Work plan

One milestone, PR-sized: the disposition and its resolution behaviour, the guarded per-class restamp
and revoke, the operator command, and the operator documentation. *Verify:* an amnestied
automation's next firing resolves trusted and renders its intent with the amnesty status; editing it
afterwards voids the record and the firing fails closed again; a definition holding any record —
cured, uncured, or hash-mismatched — is never restamped; a definition newer than the cutoff is never
eligible; revocation restores the pre-restamp state; an amnestied definition feeds no destination
echo and surfaces as the weakest claim in a closure.

A diagnostics inventory of definition records — counts per class of stamped, cured, uncured, absent,
and mismatched — is deliberately not part of this: the command's dry run answers the same question
for the operator who is about to act, and the audit endpoint's history inventory already answers the
flip-time one.

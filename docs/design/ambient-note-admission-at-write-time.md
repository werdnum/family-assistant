# Ambient Note Admission at Write Time

## Status

Proposed. Supersedes the "Minimal prompt-admission design" section of
[runtime-taint-enforcement-operational-findings.md](runtime-taint-enforcement-operational-findings.md)
(Issue 3), which proposed deriving a `blocked_by_taint` status at read time and readmitting notes
through an explicit user review. That review flow is not wanted; this design moves the decision to
the write instead.

Companion to [runtime-taint-machinery.md](runtime-taint-machinery.md) (the taint state and sink
matrix this design adds one sink to) and
[executable-definition-taint.md](executable-definition-taint.md), whose principle — judge intent at
the creation chokepoint and persist the boundary decision, not the taint — this design applies to a
second kind of stored artifact. Rather than build a parallel mechanism, it generalises the
definition-record machinery that design introduced into one **stored-artifact admission** mechanism
that automations and notes share; see "One mechanism for notes and automations".

## Principle

**A conversation must not start tainted.** Untrusted content may enter a turn because the model went
and got it — a web fetch, a document search, an explicit `get_note` — and the runtime taint state
records that. Content that reaches a turn *without* being sought is different: it is present before
the model has decided anything, so the gate cannot sit on the read. It has to sit on the change that
persisted the content, or with the operator who configured the installation.

Stored notes are the one artifact that currently violates this. Three surfaces put database content
into every turn's `<turn_context>` before any tool runs:

1. the body of every note marked `include_in_prompt`, together with the description and MIME type of
   each attachment associated with it;
2. the name and description of every database-backed skill (a note whose frontmatter declares skill
   metadata), regardless of `include_in_prompt`;
3. the title of every other note, in the "other available notes" catalog.

A note written in a turn carrying `unknown_external` content is stamped with that provenance. The
first surface restores the stamp on every later turn, so every turn starts at the highest tier and
the enforcement matrix is undeployable (the production numbers are in the operational-findings
document). The second and third surfaces put attacker-influenced strings into every prompt with no
taint at all. The two failures look opposite — one over-taints, the other under-taints — but they
have the same cause: the write that put the material on an ambient surface was never asked whether
it should.

## Design

### Ambient eligibility is decided when the note is written

A note carries a stored **ambient eligibility**: whether its **ambient material** may be placed in
turn context without being asked for. Ambient material is everything a note contributes to the
prompt: body, title, skill catalog entry, and the rendered metadata of its attachments. It is
defined by what the notes context provider renders, so anything the provider renders in future is
ambient material by construction and is covered without amending this rule. Eligibility is decided
by the write that produces the note's current ambient material, and only there:

- Every write of ambient material from a model turn crosses the ambient-write gate described below,
  at every tier. **Eligibility is the gate's disposition**: a cell that lets the write execute —
  `allow`, `audit`, or an admitting adjudication verdict — produces an eligible note; a denying
  verdict, or the fallback when no verdict comes, produces an ineligible one. The shipped matrix
  makes an ordinary interactive write (`trusted_user`) eligible without a judge, and puts the judge
  on the tiers where machine or external content is in the turn.
- A write that does not come from a model turn — an authenticated user through the web API, say —
  supplies its decision from its own trust, as described under "Every write supplies the decision".
- An ineligible note is stored, searchable, and readable by `get_note` exactly as today; it
  contributes nothing to any ambient surface.

Eligibility is a property of the note as written, in the same way `include_in_prompt` is: a later
write re-decides it under its own turn's taint. A user who wants an ineligible note in context asks
for it in a clean turn or edits it in the Notes UI; that write is trusted and the note becomes
eligible. There is no review queue, no attestation, and no readmission machinery: the readmission
*is* a write.

**A write promotes an ineligible note only if it replaces every piece of ambient material it does
not re-judge.** The repository merges partial writes — an `append` keeps the existing body, and a
call that omits attachment ids keeps the existing attachments — while the gate sees only what the
call carries. A partial write to an ineligible note therefore keeps the note ineligible, whatever
its own disposition; only a write that supplies the whole body and the whole attachment set decides
eligibility afresh. A partial write to an eligible note still crosses the gate (it adds ambient
material) and can demote it. The rule is evaluated in the repository, against the resolved
post-merge note, so no caller can promote by omission.

### The ambient-write gate

A write of ambient material is an `ambient_prompt_write` sink — a new `SinkClass` beside
`artifact_write`. It exists because the existing class is wrong for this write: `artifact_write` is
`audit` at every tier, on the reasoning that persisted content is protected downstream by the
provenance it carries. That reasoning holds for content the model must go and read back; it does not
hold for content that will be in every future prompt unasked.

The shipped cell values follow the existing lattice and the risk-adjudicated design:

| turn's max tier      | `ambient_prompt_write`        |
| -------------------- | ----------------------------- |
| `trusted_user`       | allow                         |
| `known_contact`      | audit                         |
| `recognized_machine` | adjudicate                    |
| `unknown_external`   | adjudicate (fallback confirm) |

`adjudicate` is the non-manual gate: the tool-call reviewer already sees the turn's trusted rows and
the full write, and decides whether the write is what the user asked for. Two cases fix what it
decides:

- *"Research how to do X and save it as a skill."* The turn is `unknown_external` from the web
  content, and the write puts a skill on an ambient surface. The trusted rows ask for exactly this
  write, so the judge admits it: the content is untrusted in origin but the user chose to make it a
  skill, and that choice is the intent being judged.
- *"Go and reserve me a restaurant."* A page the browser read tells the model to save a skill, and
  the model attempts the write. Nothing in the trusted rows asked for a skill or a note, so the
  judge denies it. The reservation continues; only the ambient write is refused.

The judge is bounded only by its own verdict, not by a floor: the cell exists to admit the first
case. The **fallback** — the outcome when no judge is configured or it does not answer — is
`confirm`, the same fallback the other adjudicated cells ship with. It is reached only when the
non-manual gate is absent, and there a single confirmation is the least friction available: a
refusal would force the user to redo the write in a clean turn, while a button lets them settle it
where they stand. A refused or unconfirmed ambient write is not a lost write — the same content can
still be saved as an ordinary, ineligible note in the same turn.

Which writes cross the gate:

- creating or updating a note with `include_in_prompt: true`;
- any write to a note that is already prompt-included — content, attachment associations, or
  anything else that changes its ambient material — whether or not the call names
  `include_in_prompt`. Attachments are the case that makes this general rule necessary: an
  attachment id names a stored description and MIME type that the prompt renders as text, so
  associating one is a write of ambient material even though the call carries only the id;
- creating or updating a note whose content declares skill frontmatter (the catalog entry is
  ambient);
- creating a note under a new title is **not** gated. A tainted create without ambient intent
  produces an ineligible note whose title is simply absent from the catalog. Gating every note
  creation in a tainted turn would put the reviewer on the common "save this for me" path for the
  sake of a title string, which is the friction the enforcement rollout cannot afford.

### One mechanism for notes and automations

Automations already have exactly this shape. An automation definition is stored intent written in
one turn and used in later turns with no human present; `executable-definition-taint.md` gates its
creation, stamps the authoring taint and the gate's disposition on a **definition record**, carries
a pending write id while a shadow verdict is outstanding, and treats an admitted definition as cured
for its future firings. A note is the same thing with a different future use: instead of firing, it
is read into later turns. The two artifacts should therefore share one mechanism rather than each
carrying its own.

The definition record generalises into a **stored-artifact admission record** with one schema for
both kinds: the authoring taint, the gate that examined the write and its disposition, a pending
write id, and the cure it grants. Automations keep their behaviour unchanged on the generalised
record; notes gain it. What differs between the two is declared, not coded twice: the sink class the
write is classified as (`executable_persistence` for a definition, `ambient_prompt_write` for a
note), what "the content" is (an immutable definition bound by hash; a note re-decided by each full
write), and what the cure permits (unattended firing with trusted intent; ambient presence and
explicit reads without re-tainting). **Eligibility is the readable form of a note's admission
record**, in the same way a definition's trusted-intent rendering is the readable form of its
record. The same reviewer, the same pending-verdict resolution, the same audit rows and the same
diagnostics endpoint serve both, so a fix to the gate reaches both artifacts at once.

### Admission cures the taint

An admitted note does not re-taint later turns, whether it arrives ambiently or by an explicit
`get_note`. This is the executable-definition rule: the judgment was made where the intent was
visible, and re-raising the tier later would re-ask, with no evidence, a question already answered.
Taint propagation from stored notes is therefore confined to the notes that carry a genuine risk —
those no judgment admitted: a note written ineligible, a tainted create with no ambient intent, a
note whose verdict is still pending. Reading one of those through `get_note` merges its stored
provenance into the turn exactly as today. Reading an admitted note does not.

The alternative — curing ambient presence but re-tainting on explicit read — was considered and
rejected as propagation without a matching risk. The content was judged against the user's intent
once; a later read of it is a read of user-approved context, and every unearned escalation is
friction that pushes enforcement further out. The notes context provider stops restoring provenance
altogether, since every note it includes is eligible; `get_note` restores provenance only for notes
whose record shows no admission.

### Ambient surfaces show only eligible notes

The three ambient reads — prompt-included bodies, the database-skill catalog, and the
excluded-titles catalog — filter on eligibility, in the repository, as they already filter on
`include_in_prompt` and `is_skill`. Every other read is unchanged: `get_note`, `list_notes`,
`search_documents`, the Notes UI and API all see ineligible notes as ordinary notes. Prompt context
may carry a bare count of ineligible notes so the model knows they exist; it must not carry their
titles.

### Every write supplies the decision

The notes repository is the chokepoint: its write operation **requires** the eligibility decision,
with no default, so a writer that does not supply one fails at the type checker rather than
persisting a silent default. Writers that come through tool dispatch supply the gate's disposition;
the rest supply a decision from their own trust:

- **Note tools** (`create_note`, `update_note`) and **workspace import**, which is a tool call in a
  model turn that derives ambient intent from file frontmatter, supply the gate's disposition.
- **Web API** writes are made by an authenticated user and stamp trusted provenance and eligibility.
  Today they preserve whatever provenance the note already had, which leaves a user's own edit
  carrying a stale tainted stamp; that is corrected, and it is the deterministic way a user restores
  a note the gate refused.
- **Memory apply** writes only from transcript chunks the review already rejected for external
  taint, and stamps the (clean) reviewing turn's provenance; its notes are eligible.
- **Core-memory bootstrap and index refresh** (`ensure_core_note`, `refresh_core_memory_index`)
  write the prompt-included core note directly against the table rather than through
  `add_or_update`. They are deployment-authored structure, not content from any turn, and stamp
  eligible explicitly as trusted internal writers. The eligibility column has no database default,
  so a raw write that omits it fails rather than inheriting one.
- **Call transcripts** (the Asterisk route) are authored by whoever was on the call and are never
  meant as ambient context; they are written ineligible.
- **Existing rows** get eligibility backfilled from their stored provenance: below
  `taint_policy.high_taint_tier` is eligible, at or above is not. The production notes whose stamp
  is poisoning every turn become ineligible on upgrade and can be restored by editing them in the
  Notes UI; nothing about their content or labels changes.

### Observe mode and pending verdicts

**Eligibility follows the verdict; the mode decides only whether the write is refused.** Under
`taint_policy.mode: observe` the judge still runs (the risk-adjudicated design preserves the outcome
and downgrades only its effect), so a gated write in observe mode has a real verdict. A write the
verdict admits produces an eligible note. A write the verdict denies — or that no one judged,
because the cell fell to its `confirm` fallback and observe mode asks nobody — still succeeds,
because nothing blocks in observe mode, but the note it produces is **ineligible**. Eligibility is
never granted by the mode being lenient, so switching the deployment to `enforce` later finds no
note that was admitted only because enforcement was off; the switch changes which writes are refused
or confirmed, not which notes are ambient.

The tool result reports what is known when the call returns: the verdict in enforce mode, and in
observe mode the fact that the note is saved and its ambient status pending review. There is no
later in-band notification of how a pending verdict resolved; the Notes UI and the taint-audit
endpoint show it. That is an accepted bounded residual of observe mode, not a gap to close with
lifecycle machinery.

In observe mode the verdict is not known when the write lands: the shadow review runs detached from
the tool call, so the note is persisted before the judge answers. Executable definitions already
have this shape and solve it with a pending-verdict record — the write carries a pending write id,
and the review's resolution attaches the verdict to the record it examined. Notes get the same
behaviour from the shared admission record: a gated write whose verdict is still pending is stored
**ineligible-pending**, and the verdict's resolution is the one other event that changes a note's
eligibility, flipping it to eligible on an admitting verdict and leaving it ineligible otherwise. A
pending note is ineligible on every ambient read in the meantime, so a note is never ambient before
it has been admitted, in either mode. Enforce mode, where the verdict is awaited before the write
executes, is the degenerate case in which the pending window is empty.

This is a real change in observe mode — today a tainted prompt note both lands in context and
re-taints every later turn — and it is the intended one: observe mode exists to measure what
enforcement would do, and it cannot measure that while every turn starts at the ceiling for a reason
enforcement would have removed.

## Deliberate simplifications

- **Titles of tainted, non-ambient notes are dropped from the catalog rather than gated.** The model
  can still find such a note by `list_notes` or search. Uncommon case, reasonable behaviour.
- **Eligibility is not bound to a content hash.** Automations need one because the definition is
  immutable and fires unattended; a note is re-decided by every full write to it, and a partial
  write cannot promote it, so the current eligibility always speaks for the current content.
- **Admission cures explicit reads as well as ambient presence.** One adjudicated write can, in
  principle, carry externally-authored content into later turns' egress without re-tainting them.
  That is accepted: the content was judged against the user's intent at the write, and re-tainting
  every later read would be propagation without a matching risk, which is the friction that keeps
  enforcement off.
- **No eventual in-band feedback for a pending verdict.** In observe mode the tool result can only
  say the note is pending review; how it resolved is visible in the Notes UI and the taint-audit
  endpoint, not in a later message.
- **Backfill admits existing rows below the high tier.** An existing row carries a tier but no gate
  disposition, so the backfill cannot ask what a judge would have said; rows at `recognized_machine`
  and below are admitted once, and any later write re-decides them under the full rule.
- **Workspace file content is judged by the importing turn's taint.** The gate sees the turn, not
  the provenance of the file a worker wrote; giving workspace files their own provenance is a
  separate change.
- **The web API is trusted without a gate.** It is authenticated, it is the user's own hands, and it
  is the only readmission path; gating it would recreate the review flow.

## Work plan

0. **Generalise the admission record.** Lift the definition record into the stored-artifact
   admission record described above, with automations moved onto it and no change in their
   behaviour. Verified by the existing executable-definition tests passing unchanged against the
   generalised record.
1. **Eligibility storage and backfill.** Add the stored eligibility to notes as a required write
   parameter, backfilled from provenance as above; the three ambient repository reads filter on it,
   and the repository applies the no-promotion-by-partial-write rule against the resolved note.
   Verified by repository tests that a high-tier note is absent from prompt notes, skills and
   excluded titles, and present in `get_note`, `list_notes` and search; and that an append or an
   attachment-omitting update to an ineligible note leaves it ineligible while a full write decides
   afresh.
2. **Write-time decision.** Tool writes decide eligibility from the turn's taint and the gate's
   disposition; web API writes stamp trusted provenance and eligibility; memory apply passes
   through; core-memory bootstrap and index refresh stamp eligible explicitly. Verified by tool
   tests covering each gated write shape, the ungated tainted create, the web restore path, and a
   fresh-database memory bootstrap.
3. **The sink and its cells.** `ambient_prompt_write` in the matrix, defaults and config surface,
   resolved for the gated write shapes (the update-of-a-prompt-included-note shape needs the
   existing row, so it is authorised inside the tool rather than at dispatch, through the same
   `authorize_taint_sink` path the profile-level sink check uses). Verified by policy tests for each
   tier and mode: an admitting verdict makes the note eligible, a denying verdict leaves it
   ineligible, and the `confirm` fallback asks in enforce mode and yields an ineligible note in
   observe mode. In observe mode the write is stored ineligible-pending through the shared admission
   record and the verdict's resolution flips it; verified by a test that a note is absent from
   ambient reads until an admitting shadow verdict lands, and stays absent after a denying one.
4. **Context assembly and explicit reads.** The notes provider stops restoring provenance and
   reports only a count of ineligible notes; `get_note` restores provenance only for notes without
   an admission. Verified by a functional test that a conversation with a poisoned prompt note
   starts at `trusted_user` after the note is ineligible, that `get_note` on an admitted note leaves
   the turn's tier unchanged while on an ineligible note it raises it, and by re-reading the
   taint-audit endpoint after representative traffic.
5. **Documentation.** `CONFIGURATION_REFERENCE.md` for the sink and its cells; the notes user guide
   for what happens when a save is refused or a note is not in context; the operational-findings
   document's Issue 3 section marked as superseded by this one.

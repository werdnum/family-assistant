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
second kind of stored artifact.

## Principle

**A conversation must not start tainted.** Untrusted content may enter a turn because the model went
and got it — a web fetch, a document search, an explicit `get_note` — and the runtime taint state
records that. Content that reaches a turn *without* being sought is different: it is present before
the model has decided anything, so the gate cannot sit on the read. It has to sit on the change that
persisted the content, or with the operator who configured the installation.

Stored notes are the one artifact that currently violates this. Three surfaces put database content
into every turn's `<turn_context>` before any tool runs:

1. the body of every note marked `include_in_prompt`;
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

A note carries a stored **ambient eligibility**: whether its prompt-visible material — body, skill
catalog entry, and title — may be placed in turn context without being asked for. The eligibility is
decided by the write that produces the note's current content, and only there:

- A write from a turn **below** `taint_policy.high_taint_tier` produces an eligible note. This is
  every ordinary interactive write, and every write from an authenticated user through the web API,
  which is user-authored by construction.
- A write from a turn **at or above** the high tier produces an eligible note only if the write
  passed the ambient-write gate described below. Otherwise the note is stored, searchable, and
  readable by `get_note` exactly as today, but ineligible: it contributes nothing to any ambient
  surface.

Eligibility is a property of the note as written, in the same way `include_in_prompt` is: a later
write re-decides it under its own turn's taint. A user who wants an ineligible note in context asks
for it in a clean turn or edits it in the Notes UI; that write is trusted and the note becomes
eligible. There is no review queue, no attestation, and no readmission machinery: the readmission
*is* a write.

### The ambient-write gate

A write that would leave a note eligible while the turn is at or above the high tier is an
`ambient_prompt_write` sink — a new `SinkClass` beside `artifact_write`. It exists because the
existing class is wrong for this write: `artifact_write` is `audit` at every tier, on the reasoning
that persisted content is protected downstream by the provenance it carries. That reasoning holds
for content the model must go and read back; it does not hold for content that will be in every
future prompt unasked.

The shipped cell values follow the existing lattice and the risk-adjudicated design:

| turn's max tier      | `ambient_prompt_write`          |
| -------------------- | ------------------------------- |
| `trusted_user`       | allow                           |
| `known_contact`      | audit                           |
| `recognized_machine` | adjudicate                      |
| `unknown_external`   | adjudicate (verdict floor deny) |

`adjudicate` is the non-manual gate: the tool-call reviewer already sees the turn's trusted rows and
the full write, which is what is needed to tell "add the term dates from this email to the family
note" from a smuggled instruction. The verdict floor at `unknown_external` is `deny` rather than
`confirm`, deliberately: a confirmation button is the manual review this design exists to avoid, and
a denied ambient write is not a lost write — the same content can be saved as an ordinary,
ineligible note in the same turn, and the tool result says so. The operator can lift the floor to
`confirm` through `matrix_overrides` if they prefer a button to a refusal.

Which writes cross the gate:

- creating or updating a note with `include_in_prompt: true`;
- updating the content of a note that is already prompt-included, whether or not the call names
  `include_in_prompt`;
- creating or updating a note whose content declares skill frontmatter (the catalog entry is
  ambient);
- creating a note under a new title is **not** gated. A tainted create without ambient intent
  produces an ineligible note whose title is simply absent from the catalog. Gating every note
  creation in a tainted turn would put the reviewer on the common "save this for me" path for the
  sake of a title string, which is the friction the enforcement rollout cannot afford.

The gate records its disposition on the note the way `stamp_callback_definition` records it on an
automation: the stored provenance keeps the turn's taint (so an explicit `get_note` still restores
it, unchanged) and additionally records that the ambient write was admitted. Eligibility is the
readable form of that record.

### Admission cures the taint for ambient use

An admitted note does not re-taint the turns it is ambient in. This is the executable-definition
rule: the judgment was made where the intent was visible, and re-raising the tier on every later
turn would re-ask, with no evidence, a question already answered. The notes context provider
therefore stops restoring provenance from prompt-included notes altogether — every note it includes
is eligible, so every one is either clean or admitted. Explicit retrieval keeps today's behaviour:
`get_note` on an admitted note merges its stored taint into the turn, because the model is now
acting on the content rather than merely seeing it.

### Ambient surfaces show only eligible notes

The three ambient reads — prompt-included bodies, the database-skill catalog, and the
excluded-titles catalog — filter on eligibility, in the repository, as they already filter on
`include_in_prompt` and `is_skill`. Every other read is unchanged: `get_note`, `list_notes`,
`search_documents`, the Notes UI and API all see ineligible notes as ordinary notes. Prompt context
may carry a bare count of ineligible notes so the model knows they exist; it must not carry their
titles.

### Non-LLM write paths

The gate is a tool-dispatch mechanism, so writes that do not come through a tool need the
eligibility decided by their own trust:

- **Web API** writes are made by an authenticated user and stamp trusted provenance and eligibility.
  Today they preserve whatever provenance the note already had, which leaves a user's own edit
  carrying a stale tainted stamp; that is corrected, and it is the deterministic way a user restores
  a note the gate refused.
- **Memory apply** writes only from transcript chunks the review already rejected for external
  taint, and stamps the (clean) reviewing turn's provenance; its notes are eligible by the ordinary
  rule.
- **Existing rows** get eligibility backfilled from their stored provenance: below the high tier is
  eligible, at or above is not. The production notes whose stamp is poisoning every turn become
  ineligible on upgrade and can be restored by editing them in the Notes UI; nothing about their
  content or labels changes.

### Observe mode

Under `taint_policy.mode: observe` the gate audits and admits, as every cell does. The write records
the would-have outcome, the note is eligible, and the context provider no longer re-taints later
turns from it. That is a real change in observe mode — today those turns re-taint — and it is the
intended one: observe mode exists to measure what enforcement would do, and it cannot measure that
while every turn starts at the ceiling for a reason enforcement would have removed.

## Deliberate simplifications

- **Titles of tainted, non-ambient notes are dropped from the catalog rather than gated.** The model
  can still find such a note by `list_notes` or search. Uncommon case, reasonable behaviour.
- **Eligibility is not bound to a content hash.** Automations need one because the definition is
  immutable and fires unattended; a note is re-decided by every write to it, so the current write's
  decision is always the current content's decision.
- **No cure for explicit reads.** An admitted note's stored taint is still merged by `get_note`.
  Curing it there would let one adjudicated write launder content into an unlimited number of later
  turns' egress; the ambient cure is bounded to "present in context", which is what was judged.
- **The web API is trusted without a gate.** It is authenticated, it is the user's own hands, and it
  is the only readmission path; gating it would recreate the review flow.

## Work plan

1. **Eligibility storage and backfill.** Add the stored eligibility to notes, backfilled from
   provenance as above; the three ambient repository reads filter on it. Verified by repository
   tests that a high-tier note is absent from prompt notes, skills and excluded titles, and present
   in `get_note`, `list_notes` and search.
2. **Write-time decision.** Tool writes decide eligibility from the turn's taint and the gate's
   disposition; web API writes stamp trusted provenance and eligibility; memory apply passes
   through. Verified by tool tests covering each gated write shape, the ungated tainted create, and
   the web restore path.
3. **The sink and its cells.** `ambient_prompt_write` in the matrix, defaults and config surface,
   resolved for the gated write shapes (the update-of-a-prompt-included-note shape needs the
   existing row, so it is authorised inside the tool rather than at dispatch, through the same
   `authorize_taint_sink` path the profile-level sink check uses). Verified by policy tests for each
   tier and mode, including the deny floor and an operator override to `confirm`.
4. **Context assembly.** The notes provider stops restoring provenance and reports only a count of
   ineligible notes. Verified by a functional test that a conversation with a poisoned prompt note
   starts at `trusted_user` after the note is ineligible, and by re-reading the taint-audit endpoint
   after representative traffic.
5. **Documentation.** `CONFIGURATION_REFERENCE.md` for the sink and its cells; the notes user guide
   for what happens when a save is refused or a note is not in context; the operational-findings
   document's Issue 3 section marked as superseded by this one.

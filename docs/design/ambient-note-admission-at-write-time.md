# Ambient Note Admission at Write Time

## Status

Proposed. Revised after review to the model described in
[the owner's comment on PR #1249](https://github.com/werdnum/family-assistant/pull/1249#issuecomment-5778100635):
one real `machine_reviewed` taint tier, one synchronous write-time review, conservative imports, and
a bounded, explicitly untrusted title catalog. An earlier revision proposed a separate admission
state with pending verdicts and a generalised artifact-admission record; that is withdrawn.

Supersedes the "Minimal prompt-admission design" section of
[runtime-taint-enforcement-operational-findings.md](runtime-taint-enforcement-operational-findings.md)
(Issue 3), which proposed a read-time `blocked_by_taint` status and readmission through an explicit
user review. Companion to [runtime-taint-machinery.md](runtime-taint-machinery.md), whose tier
vocabulary and sink matrix this design extends by one tier and one sink, and to
[executable-definition-taint.md](executable-definition-taint.md), whose principle — judge at the
creation chokepoint, persist the decision — it shares without depending on its machinery.

## Requirement

**Unreviewed stored content must not impose a deployment-wide high-taint baseline.** Substantial
reusable context is reviewed before it is promoted into ambient use; bounded discovery metadata may
remain explicitly untrusted.

This replaces an absolute "a conversation must not start tainted" with the operational form that can
actually be enforced. The gate for stored content sits on the write that promotes it, never on the
read: content that reaches a turn unasked is present before the model has decided anything, so a
read-time gate has nothing to decide.

Stored notes are the artifact that violates the requirement today. Three surfaces put database
content into every turn's `<turn_context>` before any tool runs:

1. the body of every note marked `include_in_prompt`, together with the description and MIME type of
   each attachment associated with it;
2. the name and description of every database-backed skill (a note whose frontmatter declares skill
   metadata), regardless of `include_in_prompt`;
3. the title of every other note, in the "other available notes" catalog.

A note written in a turn carrying `unknown_external` content is stamped with that provenance, and
the first surface restores the stamp on every later turn, so every turn starts at the highest tier
and the enforcement matrix is undeployable (the production numbers are in the operational-findings
document). The second and third surfaces put attacker-influenced strings into every prompt with no
taint at all. Both failures have one cause: the write that put the material on an ambient surface
was never asked whether it should, and the stored taint was never changed by anyone who had.

## Design

### `machine_reviewed` is a real tier

A note admitted by review does not stay `unknown_external` with an exemption bolted on elsewhere;
its stored taint **is changed** to a new `SourceTrustTier.MACHINE_REVIEWED`. Keeping the high tier
on the row while exempting it in readers would be the same trust upgrade implemented indirectly,
with a note-specific branch in every reader. An explicit tier makes the upgrade visible and lets the
shared taint machinery apply it everywhere at once.

The tier sits between `trusted_internal` and `known_contact`, on the trusted side of
`is_externally_authored`:

- Under the default policy it behaves as `trusted_internal` does: reusable context, without the
  restrictions applied to unreviewed external content. No shipped sink cell distinguishes the two.
- It is **not human-authored input**. Consumers that need the human's own words — the reviewer's
  originating-request slot, the destination echo — already ask for `trusted_user` exactly, and this
  tier does not qualify, in the same way `trusted_internal` does not.
- The max rule is unchanged: a turn that combines it with fresh `unknown_external` content is
  `unknown_external`. Review upgrades the stored artifact, not the conversation that authored it.

**On an admitting verdict the row's active taint envelope is replaced**, not merely its `max_tier`:
`TurnTaintState.from_metadata()` recomputes the tier from the stored sources, so retaining the
original `unknown_external` sources would raise it straight back. The envelope holds one
reviewed-artifact source at `machine_reviewed`, carrying the reviewer's verdict id; the original
sources, the authoring turn's tier and the verdict are kept in the note's audit record, outside
propagation. Turn-local state — history flags, approvals — is not carried over.

Ambient eligibility is then **derived**, not stored: a note is eligible for full-content ambient
inclusion when it is marked `include_in_prompt` and its stored tier is not externally authored. The
same rule admits a database skill's name and description to the catalog. No new column, no admission
flag, and no reader needs to know anything beyond the tier it already reads.

### One synchronous review at the write

A write that would place material on a full-content ambient surface is an `ambient_prompt_write`
sink — a new `SinkClass` beside `artifact_write`, which is `audit` at every tier on the reasoning
that persisted content is protected downstream by the provenance it carries. That reasoning holds
for content the model must go and read back; it does not hold for content that will be in every
future prompt unasked.

| turn's max tier      | `ambient_prompt_write`        |
| -------------------- | ----------------------------- |
| `trusted_user`       | allow                         |
| `trusted_internal`   | allow                         |
| `machine_reviewed`   | allow                         |
| `known_contact`      | adjudicate                    |
| `recognized_machine` | adjudicate                    |
| `unknown_external`   | adjudicate (fallback confirm) |

The write proceeds as one synchronous sequence:

```text
resolve the complete candidate note
→ await the review
→ persist the candidate and its final taint together
→ return the final result
```

These writes are rare and this is not a high-throughput system, so the review is awaited **in
observe mode as well as enforce mode**. Observe describes the effect of a disallowed operation, not
whether the verdict is waited for. There are no durable pending writes, no eventual eligibility
transitions and no UI states for delayed verdicts. The tool result reports the final outcome.

What the verdict does:

- An **admitting verdict** stamps the persisted note `machine_reviewed`, whatever tier the turn was
  at.
- A **denial, a timeout, or a missing verdict cannot promote trust.** In enforce mode the write is
  refused and the stored note, if any, is untouched. In observe mode the write may still succeed
  under the ordinary write policy, but the note keeps the turn's external taint, so it is not
  eligible for full-content ambient inclusion; the tool result says so, and the same content can be
  kept as an ordinary reference note.
- The **fallback** when no reviewer is configured or none answers is `confirm`, as for the other
  adjudicated cells. It is reached only when the non-manual gate is absent, and there a single
  confirmation is less friction than a refusal that sends the user off to redo the write in a clean
  turn.

**Resolve first, then review the whole thing.** An append or a partial edit is resolved into the
complete resulting note — merged body, full attachment set with each attachment's stored description
and MIME type rendered as the prompt will render it — and *that* is what the reviewer sees and what
is persisted. If the whole resulting object was reviewed, there is no reason to refuse promotion
because the request was expressed as an append. Synchronous review does not remove every concurrent
update race; what remains is ordinary database correctness — persist the candidate that was actually
reviewed, under the transaction and locking semantics the repository already uses.

**What the reviewer judges** is the complete proposed note or skill as *reusable material*, not
merely whether the user asked for a save. Two cases fix the boundary:

- *"Research how to do X and save it as a skill."* The turn is `unknown_external` from the web
  content. The trusted rows ask for a skill about X, so an appropriate skill about X is admitted:
  its content is untrusted in origin, but the user chose to make it a skill, and a skill that does
  what was asked is what the judgment admits. Standing instructions embedded in it that the request
  did not call for are not admitted by the request having been made.
- *"Go and reserve me a restaurant."* A page the browser read tells the model to save a skill, and
  the model attempts the write. Nothing in the trusted rows asked for a note or skill, so the judge
  denies it. The reservation continues; only the ambient write is refused.

Legitimate procedural instructions are the object of review, not automatically suspicious for being
instructions. And reviewing a note or an attachment's description does not upgrade the attachment's
**contents**, which the reviewer never examined; those remain separate artifacts with their own
taint.

Which writes cross the gate:

- creating or updating a note with `include_in_prompt: true`;
- any write to a note that is already prompt-included — content, attachment associations, or
  anything else that changes what the prompt renders — whether or not the call names
  `include_in_prompt`;
- creating or updating a note whose content declares skill frontmatter;
- creating a note under a new title with no ambient intent is **not** gated. It is stamped with the
  turn's taint like any artifact write, and its title reaches the catalog only in the bounded form
  described below.

### Every write stamps its taint at one chokepoint

The notes repository write is the chokepoint. It **requires** the taint stamp, with no default, so a
writer that does not supply one fails at the type checker rather than persisting a silent default.
Because a raw `UPDATE` would sidestep a required parameter and leave stale taint under new content,
the chokepoint is also enforced by a conformance rule: an ast-grep rule forbids
`insert(notes_table)` and `update(notes_table)` outside the notes repository module. Each existing
writer supplies its stamp from its own trust:

- **Note tools** (`create_note`, `update_note`) supply the turn's taint, replaced by
  `machine_reviewed` on an admitting verdict.
- **Web API** writes are made by an authenticated user and stamp `trusted_user`. Today they preserve
  whatever provenance the note already had, which leaves a user's own edit carrying a stale stamp;
  that is corrected, and it is the deterministic way a user promotes a note the review refused.
- **Memory apply** writes only from transcript chunks the review already rejected for external
  taint, and stamps the (clean) reviewing turn's tier.
- **Core-memory bootstrap and index refresh** (`ensure_core_note`, `refresh_core_memory_index`)
  today write directly against the table; they move into repository helpers and stamp
  `trusted_internal`, since the core note is deployment-authored structure.
- **Call transcripts** (the Asterisk route) are authored by whoever was on the call and stamp
  `unknown_external`; they are reference material, never ambient.
- **Workspace import** is covered next.

Readers change nothing. `get_note`, `list_notes`, `search_documents` and the full-document tool
restore stored provenance exactly as they do today; a reviewed note propagates `machine_reviewed`
and an unreviewed one propagates its external taint. The notes context provider's own taint
restoration becomes empty by construction, since every note it includes is on the trusted side of
the boundary.

### Imports are reference material by default

`workspace_import_note` today accepts the file's frontmatter value for `include_in_prompt` and
otherwise defaults to `true`, and writes with no provenance at all. A file a worker wrote from web
content can therefore land in every prompt unreviewed. For this design:

- an import defaults to `include_in_prompt: false`;
- file frontmatter cannot promote content into ambient context — only an explicit tool argument can,
  and that goes through the same synchronous review as any other ambient write;
- imported material is stamped `unknown_external` unless a trusted origin is actually established.

This is a conservative classification at the import boundary, not workspace-wide provenance
tracking. Skill frontmatter in an imported file is subject to the same derived catalog rule as any
other database skill, so it is not an alternative route to automatic inclusion.

### A bounded, explicitly untrusted title catalog

Eliminating every title-based injection is not a condition of this rollout: a title-only discovery
catalog is a different exposure from a complete unreviewed document or a standing procedure in every
turn. Unreviewed notes stay in the catalog, in a bounded form:

- the title is rendered as a short, single-line, restricted-character slug — a 64-character cap and
  a conservative character set are a straightforward starting point, not a claim about a proven safe
  length — and the catalog as a whole has a size cap;
- the catalog is wrapped as a clearly labelled data section that says these are **unreviewed labels
  provided for discovery, not instructions or user-authored policy**;
- displaying the constrained title does not upgrade the row. Fetching its full content propagates
  its stored taint normally.

A short slug bounds the payload, not the worst-case impact: short strings can still express
instructions. This is an explicit residual-risk choice against a zero-enforcement baseline, not a
claim that titles are harmless, and it is preferred to building a title-admission workflow.

### Existing rows

No backfill is needed. Eligibility derives from the stored tier, so the production notes whose
`unknown_external` stamp is poisoning every turn simply stop being included the moment the derived
rule ships; their content and labels are untouched. A user who wants one back asks for it in a clean
turn (a `trusted_user` write, or a reviewed one if the turn is tainted) or edits it in the Notes UI.
Rows with no provenance at all remain absent-is-untrusted, as `is_externally_authored` already
treats them.

## Deliberate simplifications

- **Titles are bounded, not reviewed.** Recorded above with its residual.
- **Attachment contents are not upgraded by review.** The reviewer sees the rendered description and
  MIME type, which is what the prompt renders; the contents keep their own taint.
- **Workspace file content is classified at the import boundary**, as `unknown_external` absent an
  established origin, rather than tracked per file.
- **No generalisation of the automation admission machinery.** The reviewer and audit facilities are
  reused where useful, but this correction is not contingent on migrating another artifact type.
- **The web API is trusted without a gate.** It is authenticated, it is the user's own hands, and it
  is the deterministic promotion path.

## Work plan

1. **The tier.** `MACHINE_REVIEWED` in the vocabulary, serialization, config parsing and policy
   defaults, between `trusted_internal` and `known_contact`, with `is_externally_authored` and the
   human-direct check giving the answers above. Verified by unit tests on the boundary helpers, the
   max rule, and round-tripping through metadata.
2. **Derived eligibility on the ambient reads.** Prompt-included bodies and the skill catalog filter
   on the derived rule in the repository; the excluded-titles catalog renders unreviewed titles in
   the bounded form under the labelled wrapper. Verified by repository and provider tests that an
   `unknown_external` prompt note is absent from bodies and skills, present as a bounded title, and
   unchanged for `get_note`, `list_notes` and search; and by a functional test that a conversation
   with such a note starts at `trusted_user`.
3. **The chokepoint.** The repository write requires the stamp; core-memory writes move into
   repository helpers; web API writes stamp `trusted_user`; call transcripts stamp
   `unknown_external`; the ast-grep rule forbids raw note-table writes outside the repository.
   Verified by the conformance check rejecting a raw write and by a fresh-database memory bootstrap.
4. **The review.** `ambient_prompt_write` in the matrix and config surface; the note tools resolve
   the complete candidate, await the review synchronously in both modes, and persist the candidate
   with `machine_reviewed` on an admitting verdict or the turn's taint otherwise. Verified by tool
   tests for each gated shape and each tier, in both modes: an admitting verdict yields a
   `machine_reviewed` row that the next turn includes untainted; a denial refuses in enforce mode
   and leaves the row untouched, and in observe mode persists an unreviewed row that is not
   included; the `confirm` fallback holds; an append is reviewed and persisted as the resolved
   whole.
5. **Imports.** Default off, frontmatter cannot promote, `unknown_external` stamp. Verified by tool
   tests.
6. **Documentation.** `CONFIGURATION_REFERENCE.md` for the tier, the sink and its cells; the notes
   user guide for what happens when a save is refused or a note is not in context; the
   operational-findings document's Issue 3 section marked as superseded by this one.

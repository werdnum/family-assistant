# Ambient Note Admission at Write Time

## Status

Proposed. Revised after review to the model described in
[the owner's comment on PR #1249](https://github.com/werdnum/family-assistant/pull/1249#issuecomment-5778100635):
one real `machine_reviewed` taint tier, one synchronous write-time review, and imports that are
reviewed like any other ambient write. An earlier revision proposed a separate admission state with
pending verdicts and a generalised artifact-admission record; that is withdrawn.

Supersedes the "Minimal prompt-admission design" section of
[runtime-taint-enforcement-operational-findings.md](runtime-taint-enforcement-operational-findings.md)
(Issue 3), which proposed a read-time `blocked_by_taint` status and readmission through an explicit
user review. Companion to [runtime-taint-machinery.md](runtime-taint-machinery.md), whose tier
vocabulary and sink matrix this design extends by one tier and one sink, and to
[executable-definition-taint.md](executable-definition-taint.md), whose creation-time cure this
design re-expresses as the same tier.

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
document). The second surface has an unreviewed route of its own: a workspace file a worker wrote
from web content is imported as a note, its frontmatter declares it a skill, and the skill's name
and description appear in every system prompt with no taint and no review. Both failures have one
cause: the write that put the material on an ambient surface was never asked whether it should, and
the stored taint was never changed by anyone who had.

## Design

### `machine_reviewed` is a real tier

A note admitted by review does not stay `unknown_external` with an exemption bolted on elsewhere;
its stored taint **is changed** to a new `SourceTrustTier.MACHINE_REVIEWED`. Keeping the high tier
on the row while exempting it in readers would be the same trust upgrade implemented indirectly,
with a note-specific branch in every reader. An explicit tier makes the upgrade visible and lets the
shared taint machinery apply it everywhere at once.

The tier sits between `trusted_internal` and `known_contact` in the ordering, so the max rule is
unchanged: a turn that combines it with fresh `unknown_external` content is `unknown_external`.
Review upgrades the stored artifact, not the conversation that authored it.

**Review changes what the content may be used for, not where it came from.** The security module
answers two different questions about a tier, and `machine_reviewed` answers them differently:

- **Authorship** — `is_externally_authored`. `machine_reviewed` content **is** externally authored:
  a skill researched from the web is still web-authored after a judge admits it. The authorship
  boundary therefore moves so that this tier falls on the external side. The audit log still
  withholds its free text and it never counts as the human's own words. Memory is the one consumer
  that changes predicate, below.
- **Reuse** — a new predicate, admissible for unasked reuse, true for `trusted_user`,
  `trusted_internal` and `machine_reviewed`. Ambient inclusion is decided by this predicate, and for
  sink policy `machine_reviewed` **is** `trusted_internal`: the equivalence is defined once, at the
  shared policy-lookup chokepoint where `trusted_internal` already resolves to the trusted pole, so
  the shipped cells, an operator's `matrix_overrides` and any `operator_minimum` configured for the
  trusted pole all apply to it unchanged. A row of its own in the default matrix would silently
  bypass a deployment's configured confirmation or denial. Downstream enforcement therefore treats
  reviewed material as reusable context without the restrictions applied to unreviewed external
  content, the admission sink included.

The tool-call reviewer already renders evidence in bands, and gains one: the user's own words
(`trusted_user`) are the intent it judges against; `trusted_internal` and `machine_reviewed` rows
and sources are rendered as **reviewed context** the judge may use to interpret that intent but not
as authorisation; external content is withheld as today. "Look up how to do X and set up an
automation" therefore gives the judge X as the intent and the reviewed procedure as context, rather
than the stub it renders today when the definition's stamp is external.

Ambient notes need a channel of their own to reach that band. Prompt-included notes travel in the
turn-context scaffolding message, which the reviewer's conversation rendering deliberately skips,
because that block also carries unreviewed titles and the other context providers' output. The
reviewer's input therefore gains one bounded section fed from the **eligible ambient material only**
— the same set the derived rule admits to the prompt, rendered by the **same renderer the prompt
uses**, so an included note arrives with its title, body and attachment metadata and a skill with
its catalog entry, nothing more and nothing less than the model saw — presented as reviewed context.
Nothing else in the turn-context block reaches the judge, so an ambient household procedure is
evidence the judge can match an action against without the unreviewed catalog riding along, and a
reviewed skill that shaped a call is in front of the judge as the skill's taint source already is.

**Automation definitions use the same tier.** `executable-definition-taint.md` gates a definition's
creation and marks an admitted definition as cured, so it fires with its intent intact. Under this
design that cure is not a separate flag: an admitted definition's stamp is changed to
`machine_reviewed`, exactly as an admitted note's is, and a definition at that tier renders to the
reviewer as the intent to judge against, as the cure does today. One tier expresses "a judge
admitted this stored artifact" for both kinds.

The firing rule follows from the tier rather than from a trusted-or-not branch. Today a payload-free
callback either contributes no taint source, when its definition's stamp is non-external, or enters
as an `unknown_external` trigger. A reviewed definition does neither: it **contributes a
`machine_reviewed` source** — its own stamp — and renders as the intent to judge against. The turn
is then at `machine_reviewed`, which the sink matrix treats as `trusted_internal` for enforcement
while authorship stays honest, exactly as a reviewed note included in the prompt does. In general
the callback contributes the definition's **resolved** tier, not the raw stamp on the row: nothing
for a trusted-pole stamp, `machine_reviewed` for an admitted one, `unknown_external` for anything
else; a payload is judged separately, as today. Resolution is what makes the two record forms one: a
prior-version record whose curing disposition still resolves it as valid intent resolves to
`machine_reviewed`, exactly as a record stamped with the tier does, so an existing automation keeps
firing at its cured baseline rather than dropping to `unknown_external` because its row was never
rewritten.

Records that already exist are not rewritten. A `definition_v1` record cured by its disposition
(judge-allowed, human-confirmed, or amnestied) keeps resolving exactly as it does today; only
admissions made after this ships stamp the tier. Resolution therefore accepts either form — a
`machine_reviewed` stamp, or a prior-version record whose disposition cures it — and the disposition
stays recorded on every record in either case, since it is audit data that consumers such as the
destination echo still read. This is read-time compatibility for persisted rows, as the history
epoch amnesty is, not a data migration and not a compatibility layer in code.

**On an admitting verdict the row's active taint envelope is replaced**, not merely its `max_tier`:
`TurnTaintState.from_metadata()` recomputes the tier from the stored sources, so retaining the
original `unknown_external` sources would raise it straight back. The envelope holds one
reviewed-artifact source at `machine_reviewed`, carrying the reviewer's verdict id; the original
sources, the authoring turn's tier and the verdict are kept in the artifact's audit record, outside
propagation. Turn-local state — history flags, approvals — is not carried over.

Ambient eligibility is then **derived**, not stored: a note is eligible for full-content ambient
inclusion when it is marked `include_in_prompt` and its stored tier is admissible for reuse. The
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

A turn is at exactly `machine_reviewed` when its only external influence is reviewed material — a
reviewed note or skill in the prompt, a reviewed definition firing. Transforming that material
without any other external influence is the point of curing it, so the row is `allow` and the write
persists at the turn's tier, `machine_reviewed`, eligible without a second review. Any other
external content in the turn raises its tier past this row and the candidate is reviewed as usual.
That one admission thereby vouches for what the model later composes from it alone is an accepted
residual, recorded below.

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
transitions and no UI states for delayed verdicts. The tool result reports the final outcome. This
is a deliberate carve-out from `auto-tool-call-review.md`'s rule that observe-mode adjudication runs
off the critical path: for this one sink the gated call awaits its verdict in both modes, because a
persisted note must carry the verdict's stamp and the writes are too rare for the latency to matter.
Every other sink keeps the shadow behaviour.

What the verdict does:

- An **admitting verdict** stamps the persisted note `machine_reviewed`, whatever external tier the
  turn was at. Admission only ever raises trust: a candidate already at the trusted pole keeps its
  `trusted_user` or `trusted_internal` stamp, so an operator who strengthens a trusted cell of this
  sink to `adjudicate` or `confirm` adds a check without turning the user's own words into reviewed
  external material. So does an explicit **relaxed cell**: an operator who overrides this sink to
  `allow` or `audit` at an external tier has chosen to admit unreviewed writes at that tier, and a
  write the cell lets through without a verdict is admitted on the operator's authority, stamped
  `machine_reviewed` with the override recorded in the audit record in place of a verdict. The
  alternative — a successful write that still does not make the note ambient — would give the
  override no advertised effect.
- A **denial, a timeout, or a missing verdict cannot promote trust.** In enforce mode the write is
  refused and the stored note, if any, is untouched. In observe mode the write may still succeed
  under the ordinary write policy, but an externally authored candidate persists with an
  **unadmitted stamp**: the maximum of the turn's tier, the retained material's tier and
  `known_contact`. The floor makes the outcome general: a non-admitted external candidate is never
  eligible for full-content ambient inclusion, whatever tier the note it replaces was at, so a
  denied replacement of an already reviewed note drops out of the prompt rather than riding on the
  old admission; the tool result says so, and the same content can be kept as an ordinary reference
  note. A trusted-pole candidate — reached only through an operator-strengthened trusted cell —
  keeps its trusted stamp on an observe-mode denial, because no stamp can record non-admission of
  the user's own words without falsifying their authorship; the denial is logged as observe mode
  logs every other disallowed operation, and takes effect in enforce mode. The original sources go
  to the audit record as they do on admission.
- A **configured reviewer is a precondition of admission.** A deployment with no tool-call reviewer
  has no path from an external candidate to `machine_reviewed` short of an operator override: in
  enforce mode the adjudicated cells refuse the write, in observe mode they persist it non-admitted,
  and the tool result says the note is reference material because no reviewer is configured. Nothing
  is silently admitted, and the clean-turn and Notes UI paths remain open.
- The **fallback** when a configured reviewer does not answer is `confirm`, as for the other
  adjudicated cells. It is reached only when the non-manual gate has failed, and there a single
  confirmation is less friction than a refusal that sends the user off to redo the write in a clean
  turn. A sighted human confirmation is an **admitting decision**: it stamps `machine_reviewed`
  exactly as a judge's admission does, as a human-confirmed executable definition is cured today. A
  declined or timed-out confirmation follows the denial rule above. The confirmation shows the human
  the **same resolved candidate the judge would have seen** — merged body, full attachment set with
  rendered metadata, or the imported file's contents — never the raw call arguments, since an append
  or an import path says nothing about the material being promoted. The fallback is reached in
  **enforce mode only**: observe mode never surfaces enforcement to the user, so when the reviewer
  fails an observe-mode write is simply not admitted — persisted as reference material with its
  external taint, without prompting.

**Resolve first, then review the whole thing.** An append or a partial edit is resolved into the
complete resulting note — merged body, full attachment set with each attachment's stored description
and MIME type rendered as the prompt will render it — and *that* is what the reviewer sees and what
is persisted. If the whole resulting object was reviewed, there is no reason to refuse promotion
because the request was expressed as an append. The converse holds too: material the candidate
**retains** from the stored note carries the stored note's taint into the decision. The tier the
gate evaluates is the maximum of the turn's tier, the tier of whatever the candidate keeps — the
existing body under an append, the existing attachments when the call omits them — and the stored
provenance of **every attachment whose metadata the candidate renders**, kept or newly associated.
Today the note tool only validates an attachment id, so a clean turn associating an email-derived
attachment would take the `trusted_user` allow cell and put its description in every prompt
unreviewed; merging the attachment's provenance into the gate state closes that. Merging alone is
not enough, because not every attachment has provenance to merge: tool-stored attachments, a Gmail
download among them, are registered through `store_and_register_tool_attachment` with no envelope at
all, so a sender-controlled description would still contribute nothing. Two rules close that. At the
gate an attachment with **no stored envelope is evaluated as `unknown_external`** — the description
is about to reach every prompt, and an unlabelled artifact must not pass on the strength of what
nobody recorded. And the attachment registry's registration boundary becomes a stamping chokepoint
like the notes repository, with the stamp coming from the writer's own trust exactly as a note's
does: a tool-stored attachment records the storing turn's taint state **merged with the producing
call's own result taint** — the declared output provenance the infrastructure resolves for the tool
at dispatch, or the dynamic taint the call reports — under the same machine-authorship floor
(`trusted_internal` at least, so a description the model composed is never the user's own words).
The merge matters because a tool registers its attachment while it is still executing, before the
infrastructure records the result's taint on the turn: a Gmail download that is the turn's first
external read would otherwise be stamped from a still-clean turn. With it, the download carries the
tier its message would raise the turn to whether or not anything external came before it. An
authenticated user's direct upload, which enters through `register_user_attachment` rather than the
tool helper, stamps `trusted_user`. Explicit reads of attachments are unchanged — an unlabelled
upload still contributes nothing when read, since defaulting there would taint every ordinary user
upload — the default applies only where the artifact is being promoted. A clean turn that appends to
an unreviewed note is likewise reviewed at that note's tier rather than promoting its old body to
`trusted_user` unread. The title is retained material too: `update_note` keys the row by title and
cannot replace it, and the title is rendered in full ambient inclusion and in the reviewer's band,
so a tool update of a stored note is always evaluated at no less than the note's stored tier, even
when the call replaces the body and clears the attachments. A tool write therefore never lowers a
stamp on its own; a stamp is lowered by an admission or by the user's own edit through the web API,
which replaces the title along with everything else. Synchronous review does not remove every
concurrent update race; what remains is ordinary database correctness — persist the candidate that
was actually reviewed, under the transaction and locking semantics the repository already uses.

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
taint. That has to hold on the read as well as the write: today `get_note` merges only the note's
provenance and returns attachment bytes without their own stamp, unlike ordinary attachment
injection. Attachments returned by a note read go through the shared attachment-provenance resolver,
so an email-derived attachment on a reviewed note still raises the turn to its own tier.

Which writes cross the gate is decided by the **resolved candidate**, not by the call or by the
note's previous state:

- a candidate that is prompt-included, whether the call set `include_in_prompt: true` or the note
  already was and the call changed anything the prompt renders — content, attachment associations,
  anything else;
- a candidate whose content declares skill frontmatter;
- a candidate that ends up reference-only — `include_in_prompt: false` and no skill metadata — takes
  the ordinary `artifact_write` path even if the note was ambient before, since the write removes
  ambient exposure rather than adding it. A user can always demote a note, in any turn. Demotion
  changes the note's exposure, not its provenance: the stamp keeps whatever the candidate retains,
  as the next rule says;
- creating a note under a new title with no ambient intent is likewise an ordinary artifact write,
  stamped with the turn's taint.

### Imports are reviewed like anything else

`workspace_import_note` today writes the imported note with no provenance, honours the file's
frontmatter for `include_in_prompt`, and defaults it to `true`. Files in the shared workspace are
written by sandboxed workers that may have read the web, so this is content from outside the trust
boundary entering an ambient surface on the file's own say-so.

The correction is to make the import an ordinary read followed by an ordinary write, with no path of
its own. Reading the file merges an `unknown_external` source into the turn, as fetching a web page
does — the file is external content, and the turn now says so. The note write that follows is then
gated exactly as any other: frontmatter may *request* `include_in_prompt` or declare skill metadata,
and that request crosses the `ambient_prompt_write` gate at `unknown_external`, where the judge
decides. An admitted import is `machine_reviewed`. A denied one follows the general rule: in enforce
mode nothing is stored, and retrying the unchanged file crosses the gate again because its
frontmatter still asks; in observe mode it may be stored as reference material with its external
taint, and its skill metadata does not reach the catalog because the derived rule excludes it.
Absent any frontmatter request, an import defaults to `include_in_prompt: false`, so the common
"pull this file in for reference" case involves no review.

### Every write stamps its taint at one chokepoint

The notes repository write is the chokepoint. It **requires** the taint stamp, with no default, so a
writer that does not supply one fails at the type checker rather than persisting a silent default.
Because a raw `UPDATE` would sidestep a required parameter and leave stale taint under new content,
the chokepoint is also enforced by a conformance rule: an ast-grep rule forbids
`insert(notes_table)` and `update(notes_table)` outside the notes repository module. The chokepoint
also applies the existing **machine-authorship floor** (`with_authorship_floor`, which raises a
`trusted_user` state to `trusted_internal`) to every stamp except the web API's: note text a model
composed in a clean turn is `trusted_internal`, never the human's own words, so a later explicit
read cannot present model-generated instructions as user-authored evidence to the reviewer. Only an
authenticated user's own edit stamps `trusted_user`. Each existing writer supplies its stamp from
its own trust:

- **Note tools** (`create_note`, `update_note`) and **workspace import** stamp the maximum of the
  turn's taint, floored at `trusted_internal`, and the stored taint of whatever the candidate
  retains — body under an append, attachments the call omits — replaced by `machine_reviewed` on an
  admitting verdict and floored at `known_contact` on a non-admitting one. The title is always
  retained, so a tool write never lowers a stamp; only an admission or the user's own web API edit
  does, and a clean-turn write, including a demotion, never launders retained external text into
  `trusted_user`.
- **Web API** writes are made by an authenticated user and stamp `trusted_user`. Today they preserve
  whatever provenance the note already had, which leaves a user's own edit carrying a stale stamp;
  that is corrected, and it is the deterministic way a user promotes a note the review refused.
- **Memory apply** writes only from transcript chunks the review admits under the reuse predicate —
  unreviewed external taint is still rejected — and stamps the reviewing turn's tier, floored at
  `trusted_internal` like any other machine-composed write, so a memory note built from reviewed
  material carries `machine_reviewed`. It rewrites each touched topic note whole, untouched entries
  included, so it is a partial write under the retained-provenance rule above: the stamp is the
  maximum of the reviewing turn's tier and the stored tier of the note it rewrites, and a later
  clean review that edits one entry of a `machine_reviewed` topic leaves the note `machine_reviewed`
  rather than relabelling the retained reviewed entries as internally authored.
- **Core-memory bootstrap and index refresh** (`ensure_core_note`, `refresh_core_memory_index`)
  today write directly against the table; they move into repository helpers. The bootstrap creates
  the core note and stamps `trusted_internal`, since an empty core note is deployment-authored
  structure. The refresh regenerates only the derived index and retains the rest of the core note,
  so it is a partial write like any other: it keeps the maximum of the core note's stored tier and
  `trusted_internal`, and a refresh after a topic update never relabels memory admitted from a
  `machine_reviewed` turn as internally authored.
- **Call transcripts** (the Asterisk route) are authored by whoever was on the call and stamp
  `unknown_external`; they are reference material, never ambient.

Readers change in one way only. `get_note`, `list_notes`, `search_documents` and the full-document
tool restore stored provenance as they do today — a reviewed note propagates `machine_reviewed` and
an unreviewed one propagates its external taint — except that a **note row's absent envelope is
treated as `unknown_external`** rather than skipped. Today the note tool and the shared artifact
helper both return early on missing metadata; the change is to the note path only, since every note
row is restamped below, while attachments keep their read behaviour and are defaulted only at the
admission gate, above. After the rollout batch restamp no row should be null, so this is a tripwire
for write-path regressions rather than a source of friction. The notes context provider does the
same for everything it places in the prompt, the included note bodies and the catalogued database
skills alike: a reviewed note or skill in the prompt merges a `machine_reviewed` source into the
turn, so the turn's tier says that the model processed reviewed external text. Today the provider's
taint sources come from the prompt notes only, which exclude skills; the eligible skills join them,
since a skill's name and description are in every prompt exactly as an included body is. That costs
no friction — the tier's sink cells are `trusted_internal`'s — and it keeps authorship honest: the
assistant rows stamped from that turn carry `machine_reviewed`, not a trusted-pole tier, so what
memory keeps from such a turn is stamped for what it is.

### Titles stay in the catalog

Unreviewed notes keep their titles in the "other available notes" catalog, rendered as they are
today. A title-only discovery catalog is a different exposure from a complete unreviewed document or
a standing procedure in every turn, and gating every note creation in a tainted turn for the sake of
a title string would put the reviewer on the common "save this for me" path, which is the friction
the rollout cannot afford. The catalog's existing wrapper text says these are titles to load on
demand; fetching a note's full content propagates its stored taint normally, and displaying its
title does not upgrade it. This is an explicit residual-risk choice against a zero-enforcement
baseline: short strings can still express instructions. It is recorded here so it is not
re-litigated, and it can be tightened later without touching the rest of the design.

### Existing rows

Rows that carry provenance need no backfill. Eligibility derives from the stored tier, so the
production notes whose `unknown_external` stamp is poisoning every turn simply stop being included
the moment the derived rule ships; their content and labels are untouched. A user who wants one back
has two paths: ask the assistant to rewrite it, which is reviewed at the note's stored tier because
the title is retained and admits it as `machine_reviewed`; or edit it in the Notes UI, which stamps
`trusted_user`.

Rows with **no provenance at all** are handled once, by a **batch restamp** the operator runs at
rollout. These are the notes written before provenance stamping existed, the core-memory note the
bootstrap created, and everything the current import path has written; the stored data cannot tell
them apart in general. Treating them all as external on every read would make the pre-rollout corpus
the largest new source of taint in the system — one `list_notes` over old household notes would
raise the turn to `unknown_external` — which is the friction this design exists to remove. The
restamp script first stamps `unknown_external` every row that the stored data *can* identify as
externally authored: the cohorts the write paths above classify as external, which today means the
call transcripts (the `Call Transcript:` title the Asterisk route writes, and the labels its profile
applies to them). Adding a cohort to the script is part of adding the write path's stamp, so the
script's classification and the chokepoint's never disagree. Every other null-provenance row it
stamps `trusted_internal`, minus a title pattern or an explicit list the operator supplies for rows
they know to be imports, which it stamps `unknown_external` as well. Each row's audit record records
the batch and which rule classified it. The script writes through the repository, so each restamp
enqueues the row's indexing task exactly as an ordinary write does: the indexed copy of a note
carries its own snapshot of the provenance envelope, which is what `search_documents` and the
full-document tool restore, and a restamp that left that snapshot null would make the same rows
external again through search. It is a deliberate operator judgment that the indistinguishable
remainder of the pre-rollout corpus is household material, of the same kind as the history epoch
amnesty, and it is recorded below as an accepted residual.

The same script restamps **attachments** with no envelope, and there the stored data distinguishes
every cohort, because the registry records each attachment's `source_type` and `source_id`: a `user`
upload is stamped `trusted_user`; an `email` attachment `unknown_external`; a `tool` or `script`
output at the tier its producing tool's declared output provenance names today, which is what the
registration chokepoint would have stamped, and `trusted_internal` where the tool declares none.
Without this, every Gmail download made before the chokepoint would stay envelope-less for good, and
an explicit read that skips a missing envelope would return sender-controlled bytes without raising
the turn.

After the batch, a null envelope is a write-path regression, not a legacy condition: the eligibility
resolver and the shared explicit-read resolver still treat absence as external, and they log it at
ERROR the way the history reader alarms on a post-epoch row with missing metadata. Neither parses a
missing envelope through `TurnTaintState.from_metadata()`, which would turn it into an empty trusted
state.

## Deliberate simplifications

- **Titles are neither reviewed nor bounded.** Recorded above with its residual.
- **The batch restamp trusts the indistinguishable part of the pre-rollout corpus.** A pre-rollout
  workspace import with web-derived text is restamped `trusted_internal` along with everything else
  unless the operator excludes it, because the stored data cannot distinguish it from a hand-written
  note. Cohorts the data can distinguish (call transcripts) are classified by the script, not left
  to the operator. Accepted against a zero-enforcement baseline, as the history epoch amnesty was;
  the operator's exclusion list is the mitigation for the rest.
- **Memory admits reviewed material.** The memory review and the memory-store invariants switch from
  the authorship predicate to the reuse predicate: a chunk from a turn at `machine_reviewed` is
  curated, and a memory note may carry that tier. The alternative was decided against: because a
  reviewed ambient note or skill raises every turn it is included in to `machine_reviewed`, an
  authorship-based memory review would skip every chunk of every conversation that had such a note
  in its prompt, which after rollout is most of them, and memory would be useless. The residual is
  that paraphrased reviewed web material can reach household memory; the memory curator's own review
  still sees it, and the `machine_reviewed` stamp on the chunk says what it is.
- **One admission vouches for what is composed from it alone.** A turn whose only external influence
  is reviewed material writes new ambient notes and skills at `machine_reviewed` without a second
  review. Transforming reviewed material is what curing it is for, and any other external content in
  the turn raises the tier and brings the review back. The residual is a reviewer miss that survives
  into a derived note; the derived note carries the same tier and the same reference-only fallback
  as its source.
- **Attachment contents are not upgraded by review.** The reviewer sees the rendered description and
  MIME type, which is what the prompt renders; the contents keep their own taint.
- **Workspace file content is classified as external at the read**, not tracked per file.
- **No generalisation of the definition-record machinery.** Automations share the tier, not a new
  record type; the reviewer and audit facilities are reused where useful.
- **The web API is trusted without a gate.** It is authenticated, it is the user's own hands, and it
  is the deterministic promotion path.

## Work plan

1. **The tier and the two predicates.** `MACHINE_REVIEWED` in the vocabulary, serialization, config
   parsing and policy defaults, between `trusted_internal` and `known_contact`; the authorship
   boundary moved so it reads as external; the reuse predicate added and used by ambient
   eligibility. Verified by unit tests on both predicates, the max rule, round-tripping through
   metadata, and the policy lookup: the shipped cells, a custom `matrix_overrides` entry and an
   `operator_minimum` set for the trusted pole all resolve identically for `trusted_internal` and
   `machine_reviewed`; and by memory-review and invariant tests that a `machine_reviewed` chunk is
   curated and a `machine_reviewed` memory write is accepted while an `unknown_external` one is
   still refused.
2. **Derived eligibility on the ambient reads.** Prompt-included bodies and the skill catalog filter
   on the reuse predicate in the repository. The taint-audit endpoint reports the count of
   prompt-intended notes and skills the derived rule excludes, so the operational rollout audit has
   its measurement. Verified by repository and provider tests that an `unknown_external` prompt note
   or skill is absent from bodies and the catalog, present by title, and unchanged for `get_note`,
   `list_notes` and search; that a turn whose prompt carries a reviewed skill and nothing else
   external starts at `machine_reviewed`, as one whose prompt carries a reviewed note does; that a
   row with null provenance is excluded the same way and, when fetched through `get_note`,
   `list_notes` or search, raises the turn to `unknown_external` and logs the regression; and by a
   functional test that a conversation with such a note starts at `trusted_user`.
3. **The chokepoint.** The repository write requires the stamp; core-memory writes move into
   repository helpers; a rollout script batch-restamps null-provenance rows `trusted_internal`, with
   the identifiable external cohorts (call transcripts) and an operator exclusion list stamped
   `unknown_external`; web API writes stamp `trusted_user`; call transcripts stamp
   `unknown_external`; the ast-grep rule forbids raw note-table writes outside the repository;
   `get_note` routes returned attachments through the shared attachment-provenance resolver;
   attachment registration stamps source-correct provenance. Verified by the conformance check
   rejecting a raw write, by registry tests that a Gmail download carries an `unknown_external`
   envelope when it is the turn's first external call as well as when the message already raised the
   turn, that a tool-stored attachment from a clean turn with no declared output provenance stamps
   `trusted_internal`, and that a direct user upload stamps `trusted_user`, by a test of the batch
   script that a null-envelope `email` attachment and a legacy Gmail download are restamped
   `unknown_external` while a legacy `user` upload is restamped `trusted_user`, by a repository test
   that a clean-turn tool write stamps `trusted_internal` while a web API write stamps
   `trusted_user`, by a memory-apply test that a clean review editing one entry of a
   `machine_reviewed` topic note leaves the note at `machine_reviewed`, and that the index refresh
   it triggers leaves a `machine_reviewed` core note at `machine_reviewed`, by a fresh-database
   memory bootstrap, by a test of the batch script that restamps a null row `trusted_internal`,
   stamps a null call-transcript row and an operator-excluded row `unknown_external`, leaves stamped
   rows untouched, writes into each restamped row's audit record the batch and the rule that
   classified it, and enqueues indexing for every row it restamps so that a document search over a
   restamped note restores the new tier, and by a test that a null row surviving the batch is
   excluded from ambient reads and logged at ERROR, and by a tool test that reading a reviewed note
   with an email-derived attachment raises the turn to the attachment's tier.
4. **The review.** `ambient_prompt_write` in the matrix and config surface; the note tools and the
   import tool resolve the complete candidate, await the review synchronously in both modes, and
   persist the candidate with `machine_reviewed` on an admitting verdict of an external candidate (a
   trusted-pole candidate keeps its trusted stamp), and otherwise, for an external candidate, with
   the maximum of the turn's taint, the stored taint of whatever the candidate retains and
   `known_contact`, so a denied or unreviewed partial write never lowers a stamp and never persists
   a reusable one, while a denied trusted-pole candidate keeps its trusted stamp; the import tool
   merges the file as an `unknown_external` source first and defaults inclusion off. Verified by
   tool tests for each gated shape and each tier, in both modes: an admitting verdict yields a
   `machine_reviewed` row, and the next turn that includes it merges exactly one `machine_reviewed`
   source and no external one; a denial refuses in enforce mode and leaves the row untouched, and in
   observe mode persists an unreviewed row that is not included, including when the candidate
   replaces an already reviewed note, which then drops out of the prompt, while a denied
   trusted-pole candidate under a strengthened cell keeps its `trusted_user` stamp and the denial is
   logged; the `confirm` fallback holds when a configured reviewer fails and its prompt renders the
   resolved candidate rather than the call's arguments, for an append and for an import alike; with
   no reviewer configured the write is refused in enforce mode and persisted non-admitted in observe
   mode, with a tool result that says why; an operator override of an external cell to `allow` or
   `audit` admits the write and stamps `machine_reviewed`; a trusted cell strengthened to
   `adjudicate` runs the review and an admitting verdict leaves the `trusted_user` stamp in place;
   an ambient write in a turn whose only external source is a reviewed note in the prompt is allowed
   without a review and persists `machine_reviewed`; on every path that changes a stamp — reviewer
   admission, human confirmation, operator override and observe-mode denial — the note's audit
   record holds the original sources, the authoring turn's tier and the verdict, confirmation or
   override that decided it, and the record is reachable from the note; an append is reviewed and
   persisted as the resolved whole; a clean-turn write that associates an attachment with no stored
   envelope is reviewed rather than allowed, while an explicit read of the same attachment still
   contributes no taint; a clean-turn `update_note` that replaces an unreviewed note's body, clears
   its attachments and enables inclusion is reviewed at the stored tier rather than allowed; an
   import with skill frontmatter is reviewed and, unreviewed, is absent from the catalog.
5. **The reviewer's bands and the definition cure.** `machine_reviewed` rows and sources render as
   reviewed context; eligible prompt notes and catalogued skills reach the reviewer through their
   own bounded section, produced by the prompt's own renderer; an admitted definition's stamp
   becomes `machine_reviewed` and renders as the intent to judge against, replacing the cure flag.
   Verified by the existing executable-definition tests passing with the cure expressed as the tier;
   by task-worker tests that a payload-free callback of a reviewed definition enters at
   `machine_reviewed` with its intent rendered, neither untainted nor `unknown_external`, for both
   record forms — a row stamped with the tier and a prior-version row cured by its disposition; and
   by reviewer rendering tests for each band, including that a reviewed note's title, body and
   attachment metadata and a reviewed skill's catalog entry appear in the bounded section exactly as
   the prompt rendered them, and that an unreviewed title in the turn-context block does not appear
   in the reviewer's input.
6. **Documentation.** `CONFIGURATION_REFERENCE.md` for the tier, the sink and its cells; the notes
   user guide for what happens when a save is refused or a note is not in context; the
   operational-findings document's Issue 3 section marked as superseded by this one;
   `runtime-taint-machinery.md`'s tier enumeration, sink table and policy matrix extended with
   `machine_reviewed` and `ambient_prompt_write`, since that document is the contract implementers
   read for the tier ordering and the sink surface and it must not disagree with this one; and the
   three companion contracts this design changes marked at the sentence, not the document:
   `auto-tool-call-review.md`'s rule that reviewer output never lowers provenance (an admitting
   verdict on an `ambient_prompt_write` now does, to `machine_reviewed` and only there);
   `risk-adjudicated-taint-enforcement.md`'s reservation of note promotion for human attestation
   (machine adjudication now promotes, human confirmation remaining one admitting path);
   `executable-definition-taint.md`'s statement that a cure never rewrites the authoring stamp (an
   admission now stamps the tier, the authoring taint moving to the audit record); and
   `auto-tool-call-review.md`'s rule, with its verification, that observe-mode adjudication never
   runs on the critical path (`ambient_prompt_write` alone awaits its verdict in observe mode); and
   `auto-tool-call-review.md`'s input contract, which stubs every conversation row above
   `trusted_internal` and applies the same rule to trigger definitions (`machine_reviewed` rows,
   sources and definitions now render as the reviewed-context band and as the intent to judge
   against, per milestone 5, rather than as stubs).

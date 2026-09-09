# Conversation Memory

## Status

Proposal, awaiting approval. Approach-level; construction detail (field names, payload shapes, exact
prompts) belongs to the implementing PRs.

## Problem

The assistant only remembers what it was explicitly told to save. The system prompt says "if you are
asked to remember something, add a note", and that works for "remember that Sam is allergic to
peanuts". It does nothing for the far larger class of durable information that surfaces in passing:
a preference revealed by a correction ("no, we always take the tram, not the bus"), a decision
reached over ten messages, a household routine, a fact about a child's school that the user never
thought to file. Each of these is lost when the prompt window (10 messages, 2 hours on Telegram)
rolls past it.

The raw material is not lost: every message is persisted, indexed, and searchable through
`get_message_history`. What is missing is the *curated* layer, the small set of things worth knowing
in every conversation, and a process that keeps it current without the user doing the filing.

Success is the right fact appearing in the right later conversation, with evidence and a way to
correct it. A model writing a plausible note is not success.

## What other harnesses do

The closest precedents are complementary rather than identical, and the design borrows from each
where the fit is real.

- [Claude Code auto-memory](https://code.claude.com/docs/en/memory#auto-memory) keeps a concise
  `MEMORY.md` index with on-demand topic files, writes memory during sessions, and caps what is
  loaded at startup. An oversized index is written and then answered with an error asking for
  condensation. The index-plus-topics shape and the loud cap are borrowed here; this design's
  repository-level rejection is the stronger form of the cap.
- [Codex memories](https://developers.openai.com/codex/memories) extract in the background after an
  eligible chat has been idle, keep supporting evidence alongside durable entries, separate "use
  memory" from "contribute to memory", and can exclude chats that used external context. The idle
  trigger, evidence retention, the two controls and the external-context exclusion are all
  precedents for decisions below.
- [Letta sleep-time agents](https://docs.letta.com/guides/agents/architectures/sleeptime/) give
  memory editing to a separate agent that runs while the primary one is idle, with capped memory
  blocks compiled into the prompt. The separate, confined curator is the same idea.
- [mem0](https://arxiv.org/abs/2504.19413) extracts per message pair and reconciles each candidate
  against similar stored memories with add, update, delete or no-op decisions. The operation-based
  update is borrowed; the per-pair cadence is not, for the reasons under trigger choice below.
- [OpenClaw](https://docs.openclaw.ai/concepts/memory) and
  [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) keep plain
  markdown memory files with a bootstrap budget, refuse or warn on oversized writes, and (OpenClaw)
  never promote content from tainted sessions. The plain-text, user-editable store and the taint
  rule are borrowed.
- [ChatGPT memory](https://openai.com/index/memory-and-new-controls-for-chatgpt/) pairs an explicit,
  editable list of saved memories with an opaque derived profile. The recurring complaint about the
  opaque half is the reason everything here stays inspectable.
- One study of continual consolidation,
  ["Useful Memories Become Faulty When Continuously Updated"](https://arxiv.org/abs/2605.12978),
  reports that an agent solving ARC-style tasks lost roughly half of its previously solved problems
  when its memory was repeatedly consolidated from its own solutions, and that keeping raw episodes
  alongside the consolidated memory recovered most of the loss. The task is far from household
  facts, but the mechanism (free rewrites drift, and evidence must survive consolidation) is the
  reason this design applies operations rather than prose and keeps message references.

Two distinctions matter when reading these precedents. How often a system *considers* saving
something is separate from how selectively it *saves*; per-turn consideration is not inherently bad,
and this design keeps foreground "remember" alongside the background review. And "memory must be
visible" means the curated layer is editable plain text with evidence, not that every write needs
approval.

## Design

### Memory lives in notes, as entries

Notes are already the assistant's long-term memory: they are user-visible and editable in the web
UI, visibility-labelled, indexed for search, injected into context, and stamped with the provenance
of the turn that wrote them. This design adds no second store. Memory is a set of notes carrying a
`memory` visibility label, in two tiers:

- **One always-loaded core note** (`include_in_prompt=true`), short and capped. Standing facts about
  the household and its members, standing preferences, and a bounded descriptive index of the topic
  notes.
- **Topic memory notes** (`include_in_prompt=false`), one per person, project or recurring theme,
  reachable with `get_note` and `search_documents`. Memory topics are **not** listed in the generic
  "Other available notes" title list; their pointers live inside the capped core note, so the memory
  contribution to every prompt is exactly the core note and nothing else grows with the number of
  topics.

Within a memory note the content is a list of **entries**. A note stays human-readable markdown, one
entry per bullet, but each entry carries a stable identity, the person or people it is about, the
date it was asserted, where relevant the period it applies to, its kind (an explicit statement, an
explicit correction, an inference the curator drew, a decision), and references to the messages it
came from. The structure is recoverable from the rendered note. Evidence has one shape for every
writer: a curator entry cites messages, and an entry a person adds or edits by hand in the notes UI
cites the authenticated editor and the time of the edit, with a user-authored kind; a line added
without any marker is adopted that way rather than rejected. Entries are what make the rest of the
design mechanical: forgetting, retry, contradiction handling and evidence links all operate on entry
identity, not on prose.

The always-loaded layer is exactly one note, and the notes repository enforces that shape for every
writer: a memory-labelled note may be `include_in_prompt` only if it is that one note, and a write
that would leave it over the ceiling is refused. The curator gets an error telling it to condense,
the foreground assistant gets the same tool error, and the notes UI shows it to the user. Condensing
has a legal shape when the core is full of distinct, current facts: a change set may **move** an
existing entry from the core note to a topic note, keeping its identity, lineage and evidence, and a
move cites no new evidence because it changes where an entry lives, not what it says. The curator
demotes the entries least like standing facts to make room, and the applier accepts the addition and
the moves as one all-or-nothing set, so a new core-worthy fact never fails for want of room it could
have made. Enforcing the singleton, the cap, and the exclusion of memory topics from the title list
together is what makes "capped" a statement about the rendered prompt rather than about a note, and
the rendered memory contribution is measured as such. Topic notes carry a cap of their own at the
same chokepoint, sized to the curator's input budget rather than the prompt: a write that would take
a topic past it is refused, and the curator opens a further topic instead, so no memory note ever
exceeds what one review or consolidation invocation can read.

Explicit requests ("remember that...", "forget that...") keep working in the foreground turn, and
they go through the same entry protocol as the curator. That is enforced where it cannot be
bypassed: the notes repository accepts a mutation to a memory-labelled note only from the entry
applier. The generic whole-note tools and the notes UI do not get a second path; a foreground
"remember" is an addition, a "forget" is a removal with a suppression, and a note edited by hand in
the UI is parsed back into entries and submitted as the change set that diff implies. Deleting a
memory note is a mutation like any other: through the notes UI or the delete tool, it becomes the
removal, with a suppression, of every entry the note holds, applied by the applier, and the
repository refuses a raw row deletion of a memory-labelled note just as it refuses a raw overwrite.
Without that, deleting a topic note would erase its entries and their lineage without recording a
single forget, and the next review could put them all back. The core note's topic index is derived,
not authored: the applier regenerates it from the set of topic notes that exist, in the same
transaction as any apply that creates or removes a topic, so a pointer can never outlive its topic
and no writer has to remember to update it. The index is a bounded projection, not a complete
listing: it names the most recently changed topics up to a fixed share of the core cap, so the core
note stays within its ceiling however many topics exist and opening a new topic can never fail on
the pointer it adds. A topic that has fallen out of the projection is still a memory note: it is
reachable by `get_note` by title and through document search, and the curator prompt says so, so the
projection is a convenience for the foreground turn rather than the only path to a topic. A profile
that could reach a memory note with the generic tools would be bypassing evidence validation,
suppression and entry-level conflict handling, so the repository refuses that regardless of which
tool asked. The background process is the complement for everything the user did not ask to have
saved.

### Whose memory it is

Memory has a subject (who a fact is about), a source (which conversation it came from) and an
audience (which conversations it may appear in). Per-person attribution of entries handles the
subject; it does not decide the audience. A private conversation about a surprise present must not
become context in the recipient's chat because the entry correctly names both people.

**The first version has one scope: the household.** Everything the curator learns from an opted-in
conversation is household memory, visible in every conversation of every profile that reads memory,
whoever is speaking. Conversations that contribute are those on household-member interfaces (web and
iOS chat, and Telegram) under a profile that opts into contributing. Two spoken interfaces are
read-only in the first version, for reasons in the persistence layer rather than the design: a
telephone call is saved as a transcript note, not as message-history rows, so nothing exists for the
sweep to review; and an iOS native-voice session is persisted with every assistant row stamped at
the untrusted extreme because the saved payload carries no runtime tracker, so every such stretch
would trip the provenance ceiling. Each becomes a contributor when its persistence carries message
rows with real provenance, which is follow-up work outside this design. This is stated in the user
documentation in plain terms: what you tell the assistant in those conversations may surface to any
household member. It is a deliberate, bounded choice, not an accident of a shared label, and the
curator prompt tells it to leave out anything a speaker plainly intended for one person.

Personal scopes are future work with a known shape: one core note per scope, an audience rule that
keys on the source conversation's participants, and one bound on the total memory injected across
scopes. Nothing in the first version forecloses that, and nothing in it pretends to offer it.

### A curator profile reviews each conversation when it goes idle

A conversation becomes reviewable when it has unreviewed user activity and has been quiet for an
idle window. The review runs as a background task under a new `memory_curator` processing profile.
The curator is given the unreviewed stretch of transcript as its input, sees the memory entries and
suppressions relevant to it, and proposes a change set.

**The curator's input is bounded; the applier's checks are not.** A long-lived household accumulates
topic entries and suppressions without limit, and a review that tried to show all of them would
eventually crowd out the transcript it is reviewing. So the curator's input is the capped core note
plus the topic entries and suppressions selected for relevance to the reviewed stretch, using the
search index the notes already have, under a fixed input budget. Selection only shapes what the
model sees. Everything the applier checks mechanically, conflict detection, duplicate detection and
suppression matching by version, runs over the whole store, so an entry or suppression the selection
left out still cannot be duplicated or resurrected through those checks. What a selection miss does
weaken is the one guard that is an instruction rather than a mechanism: a suppression the curator
was not shown cannot help it recognise a paraphrase from other old evidence. That is the residual
already recorded under Forgetting, and selection widens it only to the extent that relevance
selection fails to surface a suppression about the very topic under review.

**Why idle-per-conversation.** An idle stretch is a settled discussion: the curator sees the
resolution, not the half-finished question, and its work stays off the interactive path. Freshness
is good (a preference stated at lunch is available at dinner) and a single conversation is a
coherent unit, where a day's slice of a Telegram chat is an arbitrary window. This is a choice of
cadence, not a claim that foreground learning is bad: the foreground "remember" path stays, and
per-turn consideration could be added later if the review is found to miss things.

**A watermark, not a conversation boundary.** A small table records, per
`(interface_type, conversation_id)`, the last message reviewed. A review covers rows after the
watermark and advances it on success. This is what makes the design work on Telegram, where a
conversation is one chat id for its whole life and never "ends": the idle window supplies the
boundary, and the watermark keeps each review to the new material. It also handles web conversations
the user resumes days later.

Contribution has a durable **enablement boundary**: each time contribution is turned on for a
profile, the moment is recorded, and a review considers only rows newer than both the watermark and
the latest enablement of the profile the rows ran under. That one rule covers first enablement (a
conversation with no watermark row is reviewed only from the enablement moment, never from the
beginning) and every later off-and-on cycle (rows written while contribution was off lie before the
re-enablement moment and are never curated, even though the conversation keeps its old watermark).
Turning the feature on therefore learns from what is said from then on, and does not spend a burst
of model calls surfacing months of old conversations as new facts. Reviewing history before a
boundary is an explicit opt-in backfill, bounded and run on request, not an effect of enabling.

**Reviews are scheduled from state, not from events.** Whether a conversation is due is a pure
function of stored data: the first eligible turn after its watermark, meaning the first turn among
the rows the enablement boundary admits, is complete, so that a review can advance the watermark
past at least one turn rather than stop short of a live one and repeat every sweep, and either its
last activity is older than the idle window or its oldest unreviewed row is older than the maximum
deferral. The second clause is what guarantees a busy Telegram group that never goes quiet is still
reviewed. A recurring system task, on the same footing as the existing cleanup tasks, evaluates that
predicate every few minutes and enqueues one review task per due conversation, keyed on the
conversation so the same conversation never has two reviews in flight. Nothing is enqueued when a
message is persisted.

**A review covers a bounded chunk, and the watermark always moves on a terminal outcome.** A review
takes rows after the watermark up to a fixed budget of rendered size, not row count, so a stretch
can never outgrow the model's context however busy the chat was. The chunk boundary falls on a turn
boundary, never inside a turn: a user message and the assistant's reply to it are reviewed together,
so the curator never sees a request without its outcome, and the following chunk never consists of
orphaned assistant rows that the no-user-messages rule would then skip. Only completed turns are
eligible: a turn still awaiting its terminal reply, such as one parked on a confirmation that can
stay pending for a day, ends the chunk before itself and is reviewed once it completes, and the
watermark never advances past a turn that has not finished. "Finished" is defined so that no turn
can block a conversation forever, and with no clock of its own: a turn is complete when it has its
terminal reply, or when a later turn in the same conversation has completed and the earlier turn
holds no live durable work. A confirmation raised by a turn is live from the moment it is recorded
until the turn's reply lands; no change in the confirmation's status ends it, because approval,
rejection and expiry all leave the reply still to be written, so liveness is defined by the missing
terminal outcome rather than by the confirmation's status. A confirmation survives a restart as a
durable record while the in-memory turn does not, so a later turn can complete while the earlier one
is still waiting, and a turn lost to a restart after its confirmation resolved would otherwise stay
open for good; the confirmation's own expiry window therefore runs again from any resolution, and
only once it has passed with no reply does the turn fall to the never-finished rule below. That is
the one clock in the definition, and it is a deliberately narrowed guarantee rather than a safe
rule: a tool still running that long after its confirmation resolved is a failure in its own right,
and a reply that lands after the window is rendered as an orphaned assistant stretch and skipped,
recorded as a residual below. No broader time-based rule can be made safe: a confirmation can stay
pending longer than any deferral, and even after it resolves the turn is still running until its
reply lands, so a rule that declared a turn finished on elapsed time alone would advance past
outcomes that were about to be written. A turn cut off by a server restart, which the web flow
deliberately leaves without a terminal reply, therefore counts as complete as soon as the
household's next turn completes, and is rendered with a marker saying it never finished so the
curator does not read a request as an outcome. The cost is that a conversation whose last turn was
cut off is not reviewed until someone speaks in it again; that memory is delayed, not lost, and it
is recorded as a residual. A single turn larger than the budget on its own (a pasted document, say)
is rendered truncated with a marker, since a message's length has no limit at the API; the review
proceeds on what fits, and the turn's provenance is carried in full regardless. When more rows
remain, the watermark advances to the end of the chunk and the conversation is simply still due, so
the next sweep reviews the next chunk. The same rule closes the failure path: a review that fails
permanently is abandoned by advancing the watermark past its chunk, with the failed change set and
reason kept for the recent-changes view and an error logged. There is no separate retry ledger and
no state the due predicate does not already read; a conversation is due exactly when the first
eligible turn after its watermark is complete and the timing condition holds, and every terminal
outcome, success or abandonment, moves the watermark forward.

This is deliberately not an event-driven debounce. A per-message enqueue that pushes a task back has
to stay correct across the moment the worker marks a running task done, and every such design needs
a rule for the message that lands between the handler's last check and the completion write. With a
sweep there is no such moment: a message that arrives during a review simply leaves rows after the
watermark, and the next sweep sees them. Correctness rests on one predicate over durable state,
evaluated repeatedly, instead of on the ordering of two writers. The cost is that freshness is
quantised to the sweep interval, which is negligible against a thirty-minute idle window.

**Eligibility.** A stretch is reviewed only when its turns ran under a profile that contributes to
memory. Email intake, A2A, delegation subconversations, automation-triggered turns and internal
profiles such as the engineer, media analyst and event handler do not contribute. If the unreviewed
stretch contains no user messages, the review is skipped and the watermark advanced.

**Two settings, one convenience default.** Reading memory and contributing to it are separate
profile settings. Contributing implies reading: a profile that fed memory it could not see would be
a configuration error, and startup validation treats it as one. Reading does not imply contributing:
a specialised or experimental profile can benefit from the household's preferences without teaching
its conversations back into shared memory. The household-facing profiles enable both by default.

### The curator proposes; deterministic code applies

The curator does not rewrite notes. It emits a **change set**: additions, updates and removals of
entries, each citing the messages in the reviewed stretch it rests on, each with an asserted-on date
and, where relevant, an applicable period and a kind, plus moves of existing entries between the
core note and a topic note, which cite nothing new. A deterministic applier validates the change set
and applies it. Validation covers: every cited message lies inside the reviewed stretch; every
updated, removed or moved entry exists at the version the curator read; every operation that would
touch an entry's lineage, an update or removal of the entry or an addition matching any version the
entry has held, cites person-authored evidence newer than the entry's **floor**, the newest
person-authored evidence its current version rests on, whichever writer applied that version: a
curator update citing the person's correction sets it as surely as a hand-edit in the notes UI. This
is the same evidence that releases a suppression, so a review holding old evidence, or only the
assistant's acknowledgement of the person's change, can neither take back what a person did to the
entry since nor re-create as a new entry the version the person corrected away; nothing violates the
provenance ceiling, the cap, or a suppression; and an addition is not a duplicate of an entry
already present. Validation is all-or-nothing for the change set: one rejected operation fails the
review, which is retried with the rejection reasons fed back to the curator, so a fact is not
quietly dropped because a sibling operation was malformed. A review that exhausts its retries is
abandoned as described under scheduling: the watermark advances past the chunk, the rejected change
set and reason are kept for the recent-changes view, and an error is logged, so the failure is
visible rather than silent and the sweep does not keep paying for it. Neglect here degrades
availability of memory, not its integrity.

Application and watermark advancement happen in **one short transaction**, conditional on the
version of the memory store the applier validated against, with all model work outside it. The store
has one version for the household, not one per note: the duplicate and suppression checks read the
whole store, so the condition that protects them must cover the whole store, and a per-note
condition would let two reviews that each read a clean store land the same proposition in different
topic notes. If anything in the store changed underneath (a sibling curator, a foreground edit, the
notes UI), the transaction fails, the review is retried, and the curator sees the fresh state; with
reviews sparse and transactions short, the retries this costs are few. Operation identity is stable
across retries: updates and removals are keyed by the existing entry's identity, and a new entry's
identity is derived deterministically from its evidence together with everything the duplicate check
compares, its proposition, subject and applicable period, so two facts stated in one message are
distinct entries, and a retried review cannot land the same entry twice or lose unrelated entries in
a fresh rewrite. Version checks prevent a stale write; the change-set protocol is what prevents
semantic loss in a new one.

Consolidation, below, is no exception: it reads a whole partition but applies only guarded entry
operations through the same applier.

### Forgetting

"Forget that" has a durable meaning: **a forgotten entry is not reconstructed automatically from
evidence that was available when it was forgotten**, including by reviews already pending and by
retries. Optimistic concurrency alone does not give that: a curator reads an old conversation, the
user removes the fact, the curator's write conflicts, the retry sees the same old evidence and
recreates the fact. Another pending conversation can recreate it too.

So removing an entry, whether by a review that learned a contradiction, in the notes UI or through a
foreground "forget", records a **suppression**: the entry's lineage, its current text and subject,
and the time of forgetting. A **version** of an entry is its proposition, subject and applicable
period together, the same discriminators that identity and duplicate detection use, so that
forgetting is scoped to the semantic entry being removed and a fact about a different period is a
different version. An entry's identity is stable across updates, but its lineage is not one version:
it is every version the entry has held, with the evidence for each, from the addition that created
it through each update that changed any of them. A fact first learned as "bus" from one message and
updated to "tram" from a later one carries both versions, a preference re-attributed from Alice to
Bob carries both subjects, and forgetting the entry suppresses every version. Suppressions live in a
repository record outside the always-loaded note; the forgotten text never goes back into every
prompt as a negative instruction, and reaches only the curator's review input, which is a silent
background turn. The applier matches a proposal against suppressions by version alone, against every
version in the lineage, without regard to what evidence the proposal cites; only a proposal that
matches is then asked whether it cites person-authored evidence newer than the forgetting, and it is
rejected unless it does. Matching on the proposition rather than on the evidence-derived identity is
what stops the same fact returning from a different message, such as the assistant's acknowledgement
or an unrelated older conversation that stated it in the same words, while forgetting one fact from
a message still leaves the other facts from that message untouched, because they are different
propositions. That is the mechanical guarantee, and it covers retries and pending reviews over any
conversation. The curator sees the suppressed text so that it can recognise the same proposition
arriving as a paraphrase from a different old conversation, which the applier's version matching
cannot connect, because the paraphrase is a different proposition; that part is an instruction, not
a mechanism, and it is recorded as the residual below. A suppression is released only by evidence a
person authored after it: a user row newer than the forgetting, or a foreground action, and it
releases only the version that evidence restates, so a fact freed for one person or period stays
suppressed for another. An entry corrected from "bus" to "tram" and then forgotten is suppressed in
both versions, and a person later saying "tram" again frees "tram" alone, so a pending review over
the old "bus" evidence still cannot add "bus". The assistant's own acknowledgement ("I'll forget
that") is a newer row too, and it must not count, or the act of forgetting would supply the evidence
to un-forget; rows the assistant wrote, and any row older than the suppression, never release it.
Retaining the forgotten text in the suppression store is a deliberate trade: forgetting without it
cannot resist paraphrase at all, and the store is as private as the memory notes themselves.

Forgetting curated memory is distinct from deleting conversation history, indexed search entries and
other retained copies. The user documentation says so and points to what each requires.

### The curator is a confined agent

In Rule-of-Two terms the curator reads sensitive data and writes state, so both are confined to the
memory notes. It is configured, not coded, using the confinement that already exists where it
exists, and one new read-side rule where it does not:

- **Write policy**: `required_note_visibility_labels: [memory]`. The repository-level policy then
  refuses to create or overwrite any note outside that label set, so a bad review cannot damage the
  user's own notes.
- **Read policy**: every note exposed to the curator carries the `memory` label. Visibility grants
  alone do not give this: a note is visible when its labels are a subset of the reader's grants, so
  an unlabelled note is visible to every reader, including one granted only `memory`. The curator
  therefore runs under a **read policy** that adds a required-label filter, the read-side mirror of
  the write policy's required labels, enforced at every boundary where notes or skills are resolved
  for a profile: the notes repository for stored notes and the skill registry for file-based skills,
  which have no labels and would otherwise pass any grant set for the same subset reason. One policy
  object is what both boundaries consult, so every path that surfaces notes to the curator (the
  context provider's prompt notes, the title list, the skill catalogue, and `get_note` by title,
  including its file-skill fallback) goes through it. A conformance rule keeps it that way. The
  curator cannot notice that a fact is already in a user note, which is a small duplication cost the
  foreground assistant, which sees both, can repair.
- **Tools**: reading and writing memory entries. No document search: it is the widest path from the
  indexed corpus into a silent turn, and the curator has no need of it. No delete tool: deletion is
  not a write under the confinement policy, so `delete_note` would let the curator remove any note
  it can see; removals are entry operations in the change set. No messaging, no calendar, no egress,
  no delegation, no `wake_llm`, no scheduling. The three globally granted tools are withheld through
  `excluded_global_tools`, as the media analyst and coder profiles already do: a profile's own
  policy cannot refuse a global grant, and those tools would let the curator read any attachment the
  acting user owns and persist model-supplied text outside the memory label. Nothing it does is
  user-visible except the entries.
- **Context**: the core note and the relevance-selected memory entries and suppressions only.
  Turning aggregated context on for a profile attaches every context provider by default, and the
  read policy filters only notes and skills, so the curator lists every provider but the notes
  provider in `excluded_context_providers`, as the media analyst does. Calendar and Home Assistant
  text can be externally authored, and none of it belongs in a silent turn that writes household
  memory.
- **Model**: start on the standard tier with a small iteration ceiling, and keep that choice under
  the evaluation below rather than assuming it. Deciding what a household will want to know later,
  from a messy multi-speaker transcript, is judgement, not extraction.
- **History**: the curator's own rows are persisted in an internal subconversation, so they never
  enter the user's prompt window or the conversation list, but remain inspectable in diagnostics.

### The transcript is input, and its provenance travels with it

The curator does not fetch history with `get_message_history`. That tool is a broadening sensitive
read, which the taint matrix turns into a confirmation at high taint, and there is no human present
to confirm. Instead the review task renders the unreviewed rows into the request text, the same way
a delegation carries its request, and seeds the curator's taint tracker with the merged taint of
those rows. Rendering user rows only, or omitting tool result bodies, changes what the model sees;
it never changes the provenance the review carries, which is always the merged taint of the whole
stretch as the taint machinery recorded it.

**Memory holds nothing above the trusted pole, whoever writes it.** The trusted pole is the pair
`TRUSTED_USER` and `TRUSTED_INTERNAL` in the existing `SourceTrustTier`, the two tiers no shipped
policy cell distinguishes, and the boundary is the one `is_externally_authored` already draws. So
`KNOWN_CONTACT` and everything less trusted is outside it, while the ordinary curator write, whose
provenance is the household's own words and the assistant's internal processing, is inside it and
stays satisfiable. This is the memory-poisoning guard, and it is one invariant at the write
chokepoint rather than a check on one input: the notes repository refuses any write to a
memory-labelled note whose provenance stamp lies outside the trusted pole, the same
`is_externally_authored` predicate. For the curator that means a review whose turn taint has risen
above the ceiling, from any source, cannot write and fails visibly. For the foreground assistant it
means a "remember this" in a turn that has read an untrusted email is refused with a clear error
rather than filed. The precise guarantee is about origin: every memory entry was written by a turn
whose recorded provenance was at or below the trusted pole. It says nothing about truth. A household
member can be wrong, a curator can misread them, and a true statement can still be a poor standing
instruction; those are what the evidence links, the entry kinds and the evaluation below are for.

**Tainted stretches are skipped before the model call, and the loss is measured.** The review task
checks the merged taint of the unreviewed rows up front, and when it exceeds the ceiling it skips
the stretch, advances the watermark, and records the skip. This is the conservative choice, and its
cost is real: a user who says "for family hotels we need a separate sleeping area for the children"
and then has the assistant search hotel sites loses that preference to the research that followed
it. Skip observability is therefore part of the first milestone, not a later one: how many stretches
and how much user text are skipped, with a sampled review of skipped stretches to estimate the
useful facts lost. A later refinement can carry provenance per piece of evidence, so a user
statement whose own recorded provenance is clean is reviewable even when later rows in the stretch
are not, or route externally derived conclusions through explicit promotion. Neither is built until
the measurement says it is worth it. Tool result bodies are omitted from the rendered transcript in
any case: the user's words and the assistant's replies carry what mattered, and tool output is where
injected text lives.

### What the curator is asked to do

The curator prompt is short and operational. Its instructions, at approach level:

- Remember durable things: standing preferences and their corrections, facts about people and the
  household, decisions and their reasons, routines, and the state of anything the family is working
  on across conversations, including the constraints, rationale, rejected options and open decisions
  around an ongoing trip or project.
- Do not remember one-off requests, appointment timing (the calendar owns when things happen),
  device state, verbatim tool output, secrets or credentials, anything a speaker plainly meant for
  one person, or sensitive personal matters the user did not ask to have kept.
- Record what kind of thing each entry is. An explicit statement, an explicit correction, and an
  inference are different, and an assistant suggestion is not a household fact until a person
  accepted it. Where a discussion moved through considered, chosen and done, say which.
- Date every entry with when it was asserted, and where it matters, the period it applies to. A
  statement made today about how things were years ago does not supersede a current preference.
- Update rather than add. A changed fact is an update to its entry; a contradicted one is a removal
  with the new evidence cited. Never re-add a suppressed entry from old evidence.
- Attribute facts to a person. In a group chat the transcript carries who said what; "Alice prefers
  the tram" is a memory, "the user prefers the tram" is not.
- Keep the core note to standing facts and the topic index. Detail goes to a topic note.
- When nothing durable happened, propose nothing.

### Consolidation

Idle reviews are incremental and local to one conversation, so memory can accumulate near-duplicate
entries across topic notes and the core note drifts toward the cap. A consolidation pass runs under
the same curator profile over the memory entries alone, with no transcript, and merges duplicates,
resolves contradictions, and prunes entries whose own dates or wording mark them as expired. Its
input is bounded by partition, since there is no transcript to select against: one invocation per
topic note, each a natural unit that the review process keeps to a single theme, plus one invocation
over the core note alone to keep it within its cap and its index current. Consolidation is local to
a partition; it does not claim to reconcile entries across topics. Cross-topic duplicates are
prevented where they would arise, at review time, by the applier's whole-store duplicate check and
by the relevance selection that shows the curator matching entries from any topic; what slips past
both is a residual, not a job for this pass. A topic note always fits one invocation because it is
never allowed to grow past the input budget in the first place: the topic cap below is enforced at
the write chokepoint, so a review that would overfill a topic must open a new one in the same change
set while the old one still fits. Verified against a store larger than one model request.
Contradiction resolution uses the entries' kinds and applicable periods, not only assertion dates:
an explicit correction outranks an inference, and a later assertion about the past does not
overwrite a current preference. It has no calendar or tool access, so it never judges whether
something else now covers a fact. It is gated on volume, not the clock, and its output is a change
set applied by the same applier under a consolidation-specific evidence rule: with no reviewed
stretch, operations cite existing entries rather than messages, a merged or updated entry inherits
the union of its sources' evidence, and the applier validates that every cited entry exists at the
read version and that no operation drops evidence the entries carried. A merge retires the duplicate
structurally, like a move: the retired entry's lineage and evidence fold into the survivor and no
suppression is recorded, because nothing was forgotten; every other removal, whoever asked for it,
records one. Consolidation is limited to merging duplicates, resolving contradictions and pruning
expired entries; it does not reword. One extra guard applies, and it counts change of any kind: a
pass that would touch more than a fixed share of the existing entries, in any field and whether by
removal, merge or update, is rejected, so a faulty pass cannot rewrite, re-attribute or retime the
store while keeping its evidence references intact. It is a later milestone; the incremental design
is useful without it.

### Telegram

Telegram needs no separate mechanism, but three of the rules above exist because of it:

- The watermark and maximum deferral, because a chat id never ends.
- Per-person attribution, because a group chat is one conversation with several speakers, and the
  rendered transcript names the sender of each user message. That requires the persisted rows to
  carry it: the Telegram batcher today joins messages that arrive within its window and persists
  them under the last sender's identity, so two members speaking within half a second of each other
  collapse into one, and a message that arrives while another member's turn is already running is
  steered into that turn carrying only a display name, so it is persisted under the first member's
  identity. The rule is that every persisted user row carries its own sender: the batcher must never
  merge messages from different senders, and mid-turn input must carry the sender's identity through
  to persistence. Both changes are part of the Telegram milestone, since attribution that the
  transcript cannot support is not attribution.
- A longer idle window than the web, because Telegram conversation is bursty and a household member
  replying twenty minutes later is still the same exchange.

Profiles switched by slash command inside one chat (`/engineer`, `/coder`) are handled by
eligibility: only rows from contributing profiles are rendered into the review.

### User visibility and control

Memory notes are ordinary notes in the notes UI, distinguished by their label, and each entry shows
when it was asserted and links to the messages it came from. Evidence text stays with conversation
ownership: the cited turn's text is shown only to a reader the existing sole-owner rule already
admits to the source conversation, who can follow it into the full transcript as they already can,
and that rule is left exactly as it is. A group chat has no sole owner, so a memory learned there
shows its evidence text to nobody. Every other memory reader sees the entry's provenance summary
instead, who said it, in which kind of conversation, and when, without any transcript text. A cited
turn can carry a surprise or a sensitive aside alongside the durable fact the curator kept, and no
amount of excerpting by the curator makes the turn itself safe for the whole household, so the
design does not try. A recent-changes view lists what the curator added, updated or removed, with
undo. **Undo is a person-authored change set that inverts the recorded operation** and goes through
the applier like any other: undoing an addition removes the entry and is a forgetting; undoing a
removal re-adds the entry, and as person-authored evidence newer than the forgetting it releases the
suppression, while the floor above refuses a pending or retried review that would remove the
restored entry on the old evidence again; undoing an update restores the previous version. What a
suppression records is a set of versions, and the operation decides which: forgetting records the
entry's whole lineage, while undoing an update records only the version the person rejected, so the
restored version stays live and a later review that re-proposes the rejected one is refused, whether
the rejected update changed the proposition or only re-attributed it to another subject. A subtle
indicator in the chat surfaces that memory changed after a conversation without a notification per
fact. "Forget that I said X" in chat is a foreground removal with the suppression semantics above. A
deployment can turn contribution, reading, or the whole mechanism off. The user documentation for
this feature is a new `docs/user/memory.md` describing what the assistant remembers on its own, what
it never remembers, that memory is household-wide, and how to correct or forget.

## Deliberate simplifications

- **One household scope.** Personal memory is future work with a known shape; the first version says
  plainly that memory is shared and lets the curator leave out what was plainly meant for one
  person.
- **No per-turn extraction in the background.** Foreground "remember" plus idle review is the
  starting cadence; it can be revisited if the evaluation shows the review misses things people
  wanted kept.
- **No separate memory store.** Entries are a structure inside notes, recoverable from the rendered
  markdown, not a new table of facts.
- **Whole-stretch taint exclusion.** Per-evidence provenance and explicit promotion are named as the
  refinements; the first version measures the loss and ships the conservative rule.
- **Turning contribution off discards what was not yet reviewed.** Rows written while contribution
  was on but not reviewed before it was turned off lie before the next enablement moment and are
  never curated. Turning the feature off is read as "stop learning from this", and one boundary per
  enablement is what makes the eligibility rule a single comparison rather than a set of intervals.
- **Spoken interfaces read but do not contribute.** Telephone calls and iOS native-voice sessions
  are excluded from contribution until their persistence produces message rows with real provenance;
  a test pins the exclusion so the limitation is visible rather than a path that can never pass the
  gate.
- **The curator neither reads nor edits user-authored notes.** Findings that belong in a user note
  are written to a memory note; the user or the foreground assistant can merge them. This keeps both
  the input and the blast radius of a review inside the memory label.
- **No approval queue for ordinary memories.** Wrong memories are corrected after the fact through
  the recent-changes view, undo, the notes UI or in chat. An approval step for every fact would go
  unused and then be turned off.

## Residual risks

- A wrong inference from a clean conversation becomes a standing entry until someone notices. Entry
  kinds, evidence links, the recent-changes view and the small core note bound the damage.
- A forgotten fact can be re-proposed as a paraphrase from other old evidence that the applier's
  proposition-and-subject matching does not connect to the suppression, because a paraphrase is a
  different proposition; the curator's suppression input is the guard there, and it is an
  instruction rather than a mechanism.
- Memory carries the provenance of the conversation that wrote it. An entry written from a
  trusted-pole conversation keeps that tier on readers. That is the correct propagation.
- A conversation whose last turn never finished, after a restart, is not reviewed until the next
  turn in it completes. Memory from that stretch is delayed, not lost, and the alternative, a
  time-based completion rule, cannot be made safe against a turn that is still running.
- A turn whose confirmation resolved but whose tool ran on past the confirmation's expiry window is
  treated as never finished; a reply landing after that is skipped as an orphaned assistant stretch,
  so its outcome is not learned. The request is rendered with a never-finished marker, so nothing
  false is learned in its place.
- Idle review is one model call per active conversation per idle period. On a chatty deployment this
  is tens of cheap calls a day; the no-user-messages skip and the contribute setting are the levers.

## Work plan

Each milestone is independently useful and verifiable.

1. **Entries, applier, curator profile, watermark, sweep, read policy, skip metrics.** A functional
   test drives a web conversation with a fake LLM that returns a change set, advances the mock clock
   past the idle window, runs the sweep and the worker, and asserts the expected entries exist with
   evidence references and provenance; that a conversation with recent activity is not enqueued;
   that a pre-existing conversation with no watermark row is not reviewed for rows older than the
   enablement time; that after contribution is turned off and on again, rows written while it was
   off are never curated; that a re-run after the watermark reviews only new rows; that a turn
   parked on a confirmation across the idle window and past the maximum deferral, including the
   interval between the confirmation resolving and the reply landing, is neither enqueued nor
   reviewed until it completes and is then reviewed whole; that a turn left without a terminal reply
   by a restart is reviewed with a never-finished marker once a later turn completes, and the
   watermark passes it, and is not reviewed before then; that a store with more topics than the core
   index can name keeps the core within its cap and still allows a new topic to be opened; that a
   stretch larger than the chunk budget is reviewed across successive sweeps with the watermark
   advancing each time; that an abandoned review advances the watermark and leaves the conversation
   not due; and that a stretch carrying unknown-external taint is skipped with an audit record and
   counted. Concurrency is verified directly: a message persisted at any point during a review,
   including after the handler's last read and before the task is marked done, is covered by a later
   sweep; two reviews changing the same note leave both change sets applied, with one review
   retried; two reviews proposing the same proposition into different topic notes leave one entry,
   with the second review retried against the store that holds it; a note edited between a review's
   read and its apply is not overwritten; a retried review does not duplicate an addition; and a
   core note full of distinct current facts accepts a new one through a change set that moves older
   entries to a topic with their identity and lineage intact. The applier is verified to reject an
   operation citing evidence outside the stretch, an update to a missing entry, an over-cap result
   and a second always-loaded memory note, from the UI and foreground tool paths alike; to fail a
   whole change set on one rejected operation and keep the stretch reviewable; to accept a manual
   addition from the notes UI with the editor as its evidence; and to keep two facts from one
   message as distinct entries so forgetting one leaves the other, and that a corrected then
   forgotten entry restated by a person in its later version stays suppressed in its earlier one.
   The read policy is verified by seeding an unlabelled note, a default-labelled note and an
   unlabelled file-based skill and asserting none reaches the curator through the context provider,
   the title list, the skill catalogue or `get_note`, including the file-skill fallback; a
   conformance rule asserts every note or skill read the curator can reach goes through the policy.
   Conformance also confirms the curator's write policy carries the `memory` floor, that its
   effective tool set, global grants included, is exactly the memory entry tools, and that its
   effective context provider set is exactly the notes provider. Skip counters and skipped-volume
   gauges land here, on the existing metrics surface.
2. **Forgetting.** Suppression records, applier rejection, foreground "forget". Verified by the
   reconstruction scenario end to end: a fact is learned, forgotten, and a pending review over the
   original conversation plus a retry of a conflicting review both fail to recreate it, a review
   over the assistant's own acknowledgement of the forget does not re-add it, and a later user
   restatement does. Deleting a whole memory note through the notes UI and through the delete tool
   is verified to record a suppression for every entry it held and to leave no pointer to it in the
   core note's index, and a raw repository delete of a memory-labelled note is verified to be
   refused.
3. **Prompts, settings and documentation.** The curator prompt in `prompts.yaml`; the read and
   contribute settings on the profiles that carry them and the household default; a line in the
   assistant system prompt about what memory is and how to honour "forget"; `docs/user/memory.md`
   stating the household scope; the settings in the configuration reference. Verified by the
   existing prompt-render startup check, a startup validation that a contributing profile reads, a
   test that a read-only profile sees the core note and does not feed reviews, and a test that a
   foreground memory write from a turn above the trusted pole is refused.
4. **Telegram: attribution and maximum deferral.** Sender names in the rendered transcript, a
   batcher that never merges messages from different senders, mid-turn input persisted under its own
   sender, the maximum-deferral clause of the due predicate, and the longer idle window. Verified by
   Telegram functional tests with two senders posting inside one batching window and with the second
   posting while the first's turn is running, each attributed correctly in both, and a continuously
   active chat that is still reviewed.
5. **Evaluation.** A replay corpus of synthetic conversations with expected outcomes: nothing worth
   remembering, a correction, a tentative plan, an assistant mistake, several speakers, a deliberate
   forget, and useful user facts mixed with research. Each case is scored on what the curator
   proposed and on a later question answered with and without the resulting memory, tracking
   unsupported entries, missed useful facts and retrieval failures. The standard tier is compared
   against a stronger model before the cheaper one is taken as sufficient, and a sample of skipped
   stretches from a real deployment is scored for lost facts. Verified by the corpus running in CI
   with thresholds.
6. **User control.** Evidence links on entries with the owner-only text rule, the recent-changes
   view with undo, and the chat indicator. Verified by frontend tests; by functional tests that
   undoing an addition leaves the entry absent and suppressed, that undoing a removal restores the
   entry and a later review citing only the old evidence does not remove it again, and that undoing
   an update leaves the previous version live while a later review re-proposing the rejected version
   is refused; and by tests that a member who does not own the source conversation sees the
   provenance summary and no transcript text for a private-conversation memory, that every
   participant sees only the summary for a group-chat memory, and that the sole owner of a private
   conversation can open the cited turn.
7. **Consolidation pass.** Gated on review volume; merges, resolves by kind and period, prunes,
   refuses a pass that changes more than the allowed share of entries. Verified by seeded duplicate
   and contradictory entries, by three over-share passes, one each through removals, merges and
   updates, all rejected, and by a seeded store larger than one model request being consolidated
   partition by partition, with the topic cap verified to refuse a write that would overfill a
   topic.

## Open questions

- Idle windows. Proposed starting points: 30 minutes for web, 90 minutes for Telegram, 24 hours
  maximum deferral. These are settings, not design; the question is whether to ship them as defaults
  or leave memory off until a deployment sets them.
- Whether `complex_tasks` contributes from the start, or reads only. The proposal says it
  contributes. Telephone and iOS native voice read only until their persistence carries message rows
  with real provenance.
- Where the household-scope statement should surface beyond the user documentation: once, in the
  chat, when memory first writes something, or only in the docs.

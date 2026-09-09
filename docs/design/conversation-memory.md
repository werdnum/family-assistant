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
came from. The structure is recoverable from the rendered note, and a line a person adds by hand in
the notes UI without any marker is adopted as a user-authored entry on the next apply rather than
rejected. Entries are what make the rest of the design mechanical: forgetting, retry, contradiction
handling and evidence links all operate on entry identity, not on prose.

The always-loaded layer is exactly one note, and the notes repository enforces that shape for every
writer: a memory-labelled note may be `include_in_prompt` only if it is that one note, and a write
that would leave it over the ceiling is refused. The curator gets an error telling it to condense,
the foreground assistant gets the same tool error, and the notes UI shows it to the user. Enforcing
the singleton, the cap, and the exclusion of memory topics from the title list together is what
makes "capped" a statement about the rendered prompt rather than about a note, and the rendered
memory contribution is measured as such.

Explicit requests ("remember that...", "forget that...") keep working in the foreground turn, and
they go through the same entry protocol as the curator. That is enforced where it cannot be
bypassed: the notes repository accepts a mutation to a memory-labelled note only from the entry
applier. The generic whole-note tools and the notes UI do not get a second path; a foreground
"remember" is an addition, a "forget" is a removal with a suppression, and a note edited by hand in
the UI is parsed back into entries and submitted as the change set that diff implies. A profile that
could reach a memory note with the generic tools would be bypassing evidence validation, suppression
and entry-level conflict handling, so the repository refuses that regardless of which tool asked.
The background process is the complement for everything the user did not ask to have saved.

### Whose memory it is

Memory has a subject (who a fact is about), a source (which conversation it came from) and an
audience (which conversations it may appear in). Per-person attribution of entries handles the
subject; it does not decide the audience. A private conversation about a surprise present must not
become context in the recipient's chat because the entry correctly names both people.

**The first version has one scope: the household.** Everything the curator learns from an opted-in
conversation is household memory, visible in every conversation of every profile that reads memory,
whoever is speaking. Conversations that contribute are those on household-member interfaces (web,
iOS, Telegram, telephone) under a profile that opts into contributing. This is stated in the user
documentation in plain terms: what you tell the assistant in those conversations may surface to any
household member. It is a deliberate, bounded choice, not an accident of a shared label, and the
curator prompt tells it to leave out anything a speaker plainly intended for one person.

Personal scopes are future work with a known shape: one core note per scope, an audience rule that
keys on the source conversation's participants, and one bound on the total memory injected across
scopes. Nothing in the first version forecloses that, and nothing in it pretends to offer it.

### A curator profile reviews each conversation when it goes idle

A conversation becomes reviewable when it has unreviewed user activity and has been quiet for an
idle window. The review runs as a background task under a new `memory_curator` processing profile.
The curator is given the unreviewed stretch of transcript as its input, sees the current memory
entries and the suppression list described under Forgetting, and proposes a change set.

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

**Reviews are scheduled from state, not from events.** Whether a conversation is due is a pure
function of stored data: it has rows after its watermark, and either its last activity is older than
the idle window or its oldest unreviewed row is older than the maximum deferral. The second clause
is what guarantees a busy Telegram group that never goes quiet is still reviewed. A recurring system
task, on the same footing as the existing cleanup tasks, evaluates that predicate every few minutes
and enqueues one review task per due conversation, keyed on the conversation so the same
conversation never has two reviews in flight. Nothing is enqueued when a message is persisted.

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
and, where relevant, an applicable period and a kind. A deterministic applier validates the change
set and applies it. Validation covers: every cited message lies inside the reviewed stretch; every
updated or removed entry exists at the version the curator read; nothing violates the provenance
ceiling, the cap, or a suppression; and an addition is not a duplicate of an entry already present
from the same evidence. Rejected operations are dropped with a recorded reason, not silently.

Application and watermark advancement happen in **one short transaction**, conditional on the
versions of the notes the curator read, with all model work outside it. If a note changed underneath
(a sibling curator, a foreground edit, the notes UI), the transaction fails, the review is retried,
and the curator sees the fresh state. Operation identity is stable across retries: updates and
removals are keyed by entry identity, additions by their evidence, so a retried review cannot land
the same addition twice or lose unrelated entries in a fresh rewrite. Version checks prevent a stale
write; the change-set protocol is what prevents semantic loss in a new one.

Whole-note rewriting is retained only for consolidation, below, under its own guard.

### Forgetting

"Forget that" has a durable meaning: **a forgotten entry is not reconstructed automatically from
evidence that was available when it was forgotten**, including by reviews already pending and by
retries. Optimistic concurrency alone does not give that: a curator reads an old conversation, the
user removes the fact, the curator's write conflicts, the retry sees the same old evidence and
recreates the fact. Another pending conversation can recreate it too.

So removing an entry, whether in the notes UI or through a foreground "forget", records a
**suppression**: the entry's identity, its text and subject, its evidence references, and the time
of forgetting. Suppressions live in a repository record outside the always-loaded note; the
forgotten text never goes back into every prompt as a negative instruction, and reaches only the
curator's review input, which is a silent background turn. The applier rejects any proposed entry
that matches a suppression by identity or by evidence and cites nothing newer than the suppression;
that is the mechanical guarantee, and it covers retries and pending reviews over the same
conversations. The curator sees the suppressed text so that it can recognise the same proposition
arriving as a paraphrase from a different old conversation, which the applier's identity and
evidence matching cannot connect; that part is an instruction, not a mechanism, and it is recorded
as the residual below. Evidence that postdates the forgetting, such as the user restating the fact,
legitimately re-adds it. Retaining the forgotten text in the suppression store is a deliberate
trade: forgetting without it cannot resist paraphrase at all, and the store is as private as the
memory notes themselves.

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
  therefore runs under a **read policy** that adds a required-label filter at the repository, the
  read-side mirror of the write policy's required labels, and every path that surfaces notes to it
  (the context provider's prompt notes, the title list, the skill catalogue, and `get_note` by
  title) goes through that policy. A conformance rule keeps it that way. The curator cannot notice
  that a fact is already in a user note, which is a small duplication cost the foreground assistant,
  which sees both, can repair.
- **Tools**: reading and writing memory entries. No document search: it is the widest path from the
  indexed corpus into a silent turn, and the curator has no need of it. No delete tool: deletion is
  not a write under the confinement policy, so `delete_note` would let the curator remove any note
  it can see; removals are entry operations in the change set. No messaging, no calendar, no egress,
  no delegation, no `wake_llm`, no scheduling. The three globally granted tools are withheld through
  `excluded_global_tools`, as the media analyst and coder profiles already do: a profile's own
  policy cannot refuse a global grant, and those tools would let the curator read any attachment the
  acting user owns and persist model-supplied text outside the memory label. Nothing it does is
  user-visible except the entries.
- **Context**: memory entries and the suppression list only. No calendar, weather or Home Assistant.
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
memory-labelled note whose provenance stamp exceeds the known-user tier. For the curator that means
a review whose turn taint has risen above the ceiling, from any source, cannot write and fails
visibly. For the foreground assistant it means a "remember this" in a turn that has read an
untrusted email is refused with a clear error rather than filed. The precise guarantee is about
origin: every memory entry was written by a turn whose recorded provenance was at or below the
trusted pole. It says nothing about truth. A household member can be wrong, a curator can misread
them, and a true statement can still be a poor standing instruction; those are what the evidence
links, the entry kinds and the evaluation below are for.

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
resolves contradictions, and prunes entries whose own dates or wording mark them as expired.
Contradiction resolution uses the entries' kinds and applicable periods, not only assertion dates:
an explicit correction outranks an inference, and a later assertion about the past does not
overwrite a current preference. It has no calendar or tool access, so it never judges whether
something else now covers a fact. It is gated on volume, not the clock, and its output is a change
set applied by the same applier under a consolidation-specific evidence rule: with no reviewed
stretch, operations cite existing entries rather than messages, a merged or updated entry inherits
the union of its sources' evidence, and the applier validates that every cited entry exists at the
read version and that no operation drops evidence the entries carried. One extra guard applies: a
pass that would remove more than a fixed share of the existing entries is rejected. It is a later
milestone; the incremental design is useful without it.

### Telegram

Telegram needs no separate mechanism, but three of the rules above exist because of it:

- The watermark and maximum deferral, because a chat id never ends.
- Per-person attribution, because a group chat is one conversation with several speakers, and the
  rendered transcript names the sender of each user message.
- A longer idle window than the web, because Telegram conversation is bursty and a household member
  replying twenty minutes later is still the same exchange.

Profiles switched by slash command inside one chat (`/engineer`, `/coder`) are handled by
eligibility: only rows from contributing profiles are rendered into the review.

### User visibility and control

Memory notes are ordinary notes in the notes UI, distinguished by their label, and each entry shows
when it was asserted and links to the messages it came from. A recent-changes view lists what the
curator added, updated or removed, with undo, and a subtle indicator in the chat surfaces that
memory changed after a conversation without a notification per fact. "Forget that I said X" in chat
is a foreground removal with the suppression semantics above. A deployment can turn contribution,
reading, or the whole mechanism off. The user documentation for this feature is a new
`docs/user/memory.md` describing what the assistant remembers on its own, what it never remembers,
that memory is household-wide, and how to correct or forget.

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
  identity and evidence matching does not connect to the suppression; the curator's suppression
  input is the guard there, and it is an instruction rather than a mechanism.
- Memory carries the provenance of the conversation that wrote it. An entry written from a
  trusted-pole conversation keeps that tier on readers. That is the correct propagation.
- Idle review is one model call per active conversation per idle period. On a chatty deployment this
  is tens of cheap calls a day; the no-user-messages skip and the contribute setting are the levers.

## Work plan

Each milestone is independently useful and verifiable.

1. **Entries, applier, curator profile, watermark, sweep, read policy, skip metrics.** A functional
   test drives a web conversation with a fake LLM that returns a change set, advances the mock clock
   past the idle window, runs the sweep and the worker, and asserts the expected entries exist with
   evidence references and provenance; that a conversation with recent activity is not enqueued;
   that a re-run after the watermark reviews only new rows; and that a stretch carrying
   unknown-external taint is skipped with an audit record and counted. Concurrency is verified
   directly: a message persisted at any point during a review, including after the handler's last
   read and before the task is marked done, is covered by a later sweep; two reviews changing the
   same note leave both change sets applied, with one review retried; a note edited between a
   review's read and its apply is not overwritten; and a retried review does not duplicate an
   addition. The applier is verified to reject an operation citing evidence outside the stretch, an
   update to a missing entry, an over-cap result and a second always-loaded memory note, from the UI
   and foreground tool paths alike. The read policy is verified by seeding an unlabelled note and a
   default-labelled note and asserting neither reaches the curator through the context provider, the
   title list, the skill catalogue or `get_note`; a conformance rule asserts every note read the
   curator can reach goes through the policy. Conformance also confirms the curator's write policy
   carries the `memory` floor and that its effective tool set, global grants included, is exactly
   the memory entry tools. Skip counters and skipped-volume gauges land here, on the existing
   metrics surface.
2. **Forgetting.** Suppression records, applier rejection, foreground "forget". Verified by the
   reconstruction scenario end to end: a fact is learned, forgotten, and a pending review over the
   original conversation plus a retry of a conflicting review both fail to recreate it, while a
   later restatement does re-add it.
3. **Prompts, settings and documentation.** The curator prompt in `prompts.yaml`; the read and
   contribute settings on the profiles that carry them and the household default; a line in the
   assistant system prompt about what memory is and how to honour "forget"; `docs/user/memory.md`
   stating the household scope; the settings in the configuration reference. Verified by the
   existing prompt-render startup check, a startup validation that a contributing profile reads, a
   test that a read-only profile sees the core note and does not feed reviews, and a test that a
   foreground memory write from a turn above the trusted pole is refused.
4. **Telegram: attribution and maximum deferral.** Sender names in the rendered transcript, the
   maximum-deferral clause of the due predicate, and the longer idle window. Verified by a Telegram
   functional test with two senders in a group and a continuously active chat that is still
   reviewed.
5. **Evaluation.** A replay corpus of synthetic conversations with expected outcomes: nothing worth
   remembering, a correction, a tentative plan, an assistant mistake, several speakers, a deliberate
   forget, and useful user facts mixed with research. Each case is scored on what the curator
   proposed and on a later question answered with and without the resulting memory, tracking
   unsupported entries, missed useful facts and retrieval failures. The standard tier is compared
   against a stronger model before the cheaper one is taken as sufficient, and a sample of skipped
   stretches from a real deployment is scored for lost facts. Verified by the corpus running in CI
   with thresholds.
6. **User control.** Evidence links on entries, the recent-changes view with undo, and the chat
   indicator. Verified by frontend tests and a functional test that undo produces a suppression.
7. **Consolidation pass.** Gated on review volume; merges, resolves by kind and period, prunes,
   refuses a pass that drops more than the allowed share. Verified by seeded duplicate and
   contradictory entries.

## Open questions

- Idle windows. Proposed starting points: 30 minutes for web and telephone, 90 minutes for Telegram,
  24 hours maximum deferral. These are settings, not design; the question is whether to ship them as
  defaults or leave memory off until a deployment sets them.
- Whether `complex_tasks` and `telephone` contribute from the start, or read only. The proposal says
  both contribute.
- Where the household-scope statement should surface beyond the user documentation: once, in the
  chat, when memory first writes something, or only in the docs.

# Conversation Memory

## Status

Proposal, awaiting approval. Approach-level; construction detail (field names, payload shapes, exact
prompts) belongs to the implementing PRs.

This revision splits the design into **v1 invariants** and **future hardening**. An earlier revision
answered every reviewer counterexample with a mechanism and grew into a fact-management system:
semantic entry identities, version lineage, suppressions with selective release, evidence floors,
semantic undo, whole-store uniqueness, and a detailed model of the conversation-completion
lifecycle. Most of that is deferred here. The target for v1 is a helpful, inspectable notebook that
is allowed to be fallible. Complexity is kept only where it buys security, bounded resource use, or
prevention of silent data loss; everything else waits for evidence that it is needed.

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
  against similar stored memories with add, update, delete or no-op decisions. Proposing edits
  rather than free rewrites is borrowed; the per-pair cadence is not.
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
  facts, but the mechanism (free rewrites drift, and evidence must survive) is the reason this
  design applies small edits rather than rewriting notes and keeps message references.

Two distinctions matter when reading these precedents. How often a system *considers* saving
something is separate from how selectively it *saves*; per-turn consideration is not inherently bad,
and this design keeps foreground "remember" alongside the background review. And "memory must be
visible" means the curated layer is editable plain text with evidence, not that every write needs
approval.

## What v1 guarantees, and what it does not

The v1 invariants, each of which is enforced by code rather than by prompt:

- **Confined reads.** The curator sees memory notes and nothing else of the household's.
- **Confined writes.** The curator writes memory notes and nothing else, through no broad tools.
- **Bounded sizes.** The always-loaded memory contribution, every memory note, and every review
  input have fixed caps enforced at the write chokepoint for every writer.
- **Provenance.** No memory is written from a turn whose recorded provenance is outside the trusted
  pole, whoever writes it.
- **Bounded, recoverable review.** A durable per-conversation watermark, reviews over bounded
  chunks, and a sweep that recovers from any crash by re-evaluating stored state.
- **No silent stale overwrite.** A memory write is short, atomic with its watermark advance, and
  conditional on the store revision it read; an edit list proposed against a store a person has
  since changed is never committed.
- **Household scope and correct attribution.** Memory is household-wide by stated policy, and every
  entry names who said it.
- **Inspectable.** Memory is plain notes with dates and source references, editable in the notes UI,
  with a recent-changes view.

What v1 deliberately does **not** guarantee: that a deleted fact stays deleted if someone states it
again or an old conversation is reviewed later; that the same fact never appears twice in different
words; that undoing a curator change is semantically exact; that memory is consolidated on its own;
or that every unusual turn lifecycle is modelled. Those are listed under future hardening, each with
the evidence that would justify building it.

## v1 design

### Memory lives in notes

Notes are already the assistant's long-term memory: they are user-visible and editable in the web
UI, visibility-labelled, indexed for search, injected into context, and stamped with the provenance
of the turn that wrote them. This design adds no second store. Memory is a set of notes carrying a
`memory` visibility label, in two tiers:

- **One always-loaded core note** (`include_in_prompt=true`), short and capped. Standing facts about
  the household and its members, standing preferences, and a short descriptive index of the topic
  notes.
- **Topic memory notes** (`include_in_prompt=false`), one per person, project or recurring theme,
  reachable with `get_note` and `search_documents`. Memory topics are **not** listed in the generic
  "Other available notes" title list; their pointers live inside the capped core note, so the memory
  contribution to every prompt is exactly the core note and nothing else grows with the number of
  topics.

A memory note is human-readable markdown, one entry per bullet. Each entry carries the date it was
asserted, who said it, and references to the messages it came from; where it matters, the period it
applies to. The curator writes the text; the speaker and the date are not its to type. Each edit
names one of its cited messages as the grounding message, the user row in which the claim was made,
and the apply path checks that it is a cited user row inside the stretch and renders its sender and
time into the entry, so attribution is right whenever the grounding citation is, and a wrong
citation is the one thing left for the evaluation to catch. It follows that v1 records only what a
person said: a statement, a correction, a decision. An inference the curator might draw from a
pattern of requests has no row in which anyone said it, so it has no honest speaker or date, and it
is excluded from v1 rather than given a provenance shape of its own. That is the whole entry model
in v1. There is no semantic identity, no version lineage, and no machine-readable classification
beyond what the curator writes in the text ("correction:", "decision:"). Entries are the unit the
curator edits and the user reads, not a fact schema.

**Caps are enforced at the repository for every writer.** The always-loaded layer is exactly one
note: a memory-labelled note may be `include_in_prompt` only if it is that one note, that note
cannot have the flag turned off, and a write that would leave it over its ceiling is refused,
whichever tool or UI asked. The repository holds the shape from both sides, never a second
always-loaded memory note and never none. The curator is told to condense, moving detail to a topic
note; the foreground assistant gets the same tool error; the notes UI shows it to the user. Topic
notes carry a cap of their own at the same chokepoint, sized to the curator's input budget, so no
memory note ever exceeds what one review can read; a review that would overfill a topic opens a
further one. Enforcing the singleton, the caps and the title-list exclusion together is what makes
"capped" a statement about the rendered prompt rather than about a note, and the rendered memory
contribution is measured as such. The core note's topic index is derived, not authored: the apply
path regenerates it from the topic notes that exist, their titles and when each last changed, on
every apply that touches a topic note, so a pointer never outlives its topic or its title, the
projection is current, and no writer has to remember to update it. It is short by the same cap: it
names the most recently changed topics up to a fixed share of the core, and a topic that has dropped
off it is still reachable by title and by search.

Explicit requests ("remember that...", "forget that...") keep working in the foreground turn, and
they go through the same apply path as the curator, described below. The notes UI edits memory notes
as it edits any note, and those edits land through the apply path too. A person's edit has a
different kind of evidence: the authenticated editor and the time of the edit, rendered the same way
a curator entry renders its speaker and date, and no transcript to cite. The invariants that apply
to it are the ones that are about the store rather than about a review: the label, the caps, the
provenance rule, which a signed-in household member satisfies, and the store revision.

### Whose memory it is

Memory has a subject (who a fact is about), a source (which conversation it came from) and an
audience (which conversations it may appear in). Per-person attribution handles the subject; it does
not decide the audience. A private conversation about a surprise present must not become context in
the recipient's chat because the entry correctly names both people.

**The first version has one scope: the household.** Everything the curator learns from an opted-in
conversation is household memory, visible in every conversation of every profile that reads memory,
whoever is speaking. Conversations that contribute are those on household-member interfaces (web and
iOS chat, and Telegram) under a profile that opts into contributing. Two spoken interfaces are
read-only in the first version, for reasons in the persistence layer rather than the design: a
telephone call is saved as a transcript note, not as message-history rows, so nothing exists for the
sweep to review; and an iOS native-voice session is persisted with every assistant row stamped at
the untrusted extreme because the saved payload carries no runtime tracker, so every such stretch
would trip the provenance rule. Each becomes a contributor when its persistence carries message rows
with real provenance, which is follow-up work outside this design. The user documentation says in
plain terms that what you tell the assistant in those conversations may surface to any household
member, and the curator prompt tells it to leave out anything a speaker plainly intended for one
person.

Personal scopes are future hardening with a known shape: one core note per scope, an audience rule
keyed on the source conversation's participants, and one bound on the total memory injected across
scopes.

### A curator reviews each conversation when it goes idle

A conversation becomes reviewable when it has unreviewed user activity and has been quiet for an
idle window. The review runs as a background task under a new `memory_curator` processing profile.
The curator is given the unreviewed stretch of transcript, the core note, and the topic entries most
relevant to that stretch, under a fixed input budget, and proposes a small number of note edits, or
nothing.

**Why idle-per-conversation.** An idle stretch is a settled discussion: the curator sees the
resolution, not the half-finished question, and its work stays off the interactive path. Freshness
is good (a preference stated at lunch is available at dinner) and a single conversation is a
coherent unit, where a day's slice of a Telegram chat is an arbitrary window. This is a choice of
cadence, not a claim that foreground learning is bad: the foreground "remember" path stays, and
per-turn consideration could be added later if the evaluation shows the review misses things.

**A watermark, not a conversation boundary.** A small table records, per
`(interface_type, conversation_id)`, the last message reviewed. A review covers rows after the
watermark and advances it on every terminal outcome, success or abandonment. This is what makes the
design work on Telegram, where a conversation is one chat id for its whole life and never "ends":
the idle window supplies the boundary, and the watermark keeps each review to the new material. It
also handles web conversations the user resumes days later.

**Enablement boundary.** Contribution is a property of a profile-and-interface pair, since the same
profile serves several interfaces. Each time a pair becomes contributing, the moment is recorded for
that pair, and a review considers only rows newer than both the watermark and the moment for the
pair the rows ran under. Adding Telegram to the interface list later therefore learns from Telegram
from that moment, not from rows that accumulated while it was excluded. Turning the feature on
therefore learns from what is said from then on, and does not spend a burst of model calls surfacing
months of old conversations as new facts. Reviewing older history is an explicit, bounded,
on-request backfill.

**Reviews are scheduled from state, not from events.** Whether a conversation is due is a pure
function of stored data: it has at least one completed, eligible turn after its watermark, and
either its last eligible activity is older than the idle window or its oldest unreviewed eligible
row is older than the maximum deferral, both measured over the rows the profile-and-interface
boundary admits, so an excluded backlog neither hurries nor delays a review. The second clause is
what guarantees a busy Telegram group that never goes quiet is still reviewed. A recurring system
task evaluates that predicate every few minutes and enqueues one review per due conversation, keyed
on the conversation so the same conversation never has two reviews in flight. Nothing is enqueued
when a message is persisted, so there is no race between a message landing and a task completing: a
message that arrives during a review leaves rows after the watermark, and the next sweep sees them.
Freshness is quantised to the sweep interval, which is negligible against a thirty-minute idle
window.

**A review covers a bounded chunk of completed turns.** A review takes rows after the watermark up
to a fixed budget of rendered size, cut on a turn boundary so the curator never sees a request
without its outcome. A turn is complete when it has its terminal reply. A turn that has none yet,
one parked on a confirmation or cut off by a restart, ends the chunk before itself; if a later turn
in the same conversation completes first, the earlier turn is rendered with a marker saying it never
finished and the watermark passes it, so no turn blocks a conversation for good. That is the whole
lifecycle model in v1. A reply that lands after its turn was passed over is an orphaned assistant
row and is not reviewed; the cost is one unlearned outcome from a rare sequence, recorded as a
residual. A single turn larger than the budget is rendered truncated with a marker. When more rows
remain after a chunk, the conversation is simply still due. A review that fails permanently is
abandoned by advancing the watermark past its chunk, with the reason logged and kept for the
recent-changes view.

**Eligibility.** A stretch is reviewed only when its turns ran under a profile that contributes to
memory and arrived on an interface the deployment lists as contributing. Both dimensions are needed
because Telegram and the web share the default profile: the interface list is what lets Telegram
stay out until its attribution is trustworthy while the same profile contributes from the web. Email
intake, A2A, delegation subconversations, automation-triggered turns and internal profiles such as
the engineer, media analyst and event handler do not contribute. If the unreviewed stretch contains
no user messages, the review is skipped and the watermark advanced.

**Two settings, one convenience default.** Reading memory and contributing to it are separate
profile settings. Contributing implies reading: a profile configured to contribute without reading
is a configuration error that startup validation rejects. Reading does not imply contributing: a
specialised or experimental profile can benefit from the household's preferences without teaching
its conversations back into shared memory. Contribution ships off, and turns on by default for the
household profile only once the evaluation and the user controls below have landed, so no deployment
receives silent model-written memory before it can measure it and correct it.

### The curator proposes edits; the apply path enforces the invariants

The curator does not rewrite notes. It emits a short list of **edits**: add an entry to a named
note, replace an entry (identified by its current text) with new text, remove one, or move one to
another memory note. An addition, replacement or removal cites the messages in the reviewed stretch
it rests on, a removal citing the contradiction that grounds it; a move carries the entry's existing
references and cites nothing new, because it changes where an entry lives rather than what it says,
and it is how the curator makes room in a full core note. A deterministic apply path validates and
applies them. Validation is exactly the v1 invariants: every target note carries the `memory` label
and is within the curator's scope; every addition, replacement or removal cites at least one
message, every cited message lies inside the reviewed stretch, and the grounding message it names is
one of those citations and a user row; the writing turn's provenance is inside the trusted pole; no
note ends over its cap; and the store is still at the revision the curator read. Validation is
all-or-nothing for the list, and a rejected list is retried once with the reasons fed back before
the review is abandoned.

Application and watermark advancement happen in **one short transaction**, conditional on the store
revision, with all model work outside it. The store has one revision for the household, and
**curator writes are serialised household-wide**: one review applies at a time. At this scale that
costs nothing and removes every question about two reviews landing the same fact in two places at
once. If the revision moved underneath, because a person edited or deleted memory while the review
ran, the apply fails and the review is retried against the fresh store. What that guarantees is
narrow and mechanical: an edit list proposed against a store a person has since changed is never
committed. The retry reads the corrected store and the same transcript, and it can still propose the
old fact again; the curator is shown the current entries and told not to, and the recent-changes
view shows it when that instruction fails. Making a person's correction mechanically binding on
later reviews is the durable-forgetting item under future hardening.

That is the whole protocol. There is no semantic identity for an entry, no duplicate detection
beyond showing the curator the relevant entries and telling it to update rather than add, and no
lineage. A retried review re-reads the store and proposes against what is there.

### Forgetting in v1

Deleting an entry, or a whole topic note, whether in the notes UI, through the delete tool, or by
saying "forget that" in chat, removes the curated memory and, through the store revision,
invalidates any curator proposal in flight. The core note is the one memory note that cannot be
deleted, since exactly one must exist: deleting it is refused, and clearing it is an ordinary edit
that empties its entries and regenerates the index. That is all it guarantees. If somebody states
the fact again, or a conversation that mentions it is reviewed later, it can be learned again. The
user documentation says both things plainly: forgetting removes it from memory, not from
conversation history or search, and it may come back if it is said again. The curator prompt says
never to re-add something a person has just removed, and the recent-changes view makes it visible if
that instruction fails.

Durable semantic forgetting, in which a removed fact resists reconstruction from old evidence, is
future hardening, gated on the product requirement showing up in use.

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
  including its file-skill fallback) goes through it. A conformance rule keeps it that way.
- **Tools**: reading memory notes and proposing edits. No document search: it is the widest path
  from the indexed corpus into a silent turn. No delete tool: deletion is not a write under the
  confinement policy, so `delete_note` would let the curator remove any note it can see; removals
  are edits in the list. No messaging, no calendar, no egress, no delegation, no `wake_llm`, no
  scheduling. The three globally granted tools are withheld through `excluded_global_tools`, as the
  media analyst and coder profiles already do.
- **Context**: the core note and the relevance-selected topic entries only. The curator lists every
  provider but the notes provider in `excluded_context_providers`, as the media analyst does.
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
it never changes the provenance the review carries.

**Memory holds nothing above the trusted pole, whoever writes it.** The trusted pole is the pair
`TRUSTED_USER` and `TRUSTED_INTERNAL` in the existing `SourceTrustTier`, and the boundary is the one
`is_externally_authored` already draws. The notes repository refuses any write to a memory-labelled
note whose provenance stamp lies outside it. For the curator that means a review whose turn taint
has risen above the ceiling cannot write and fails visibly; for the foreground assistant it means a
"remember this" in a turn that has read an untrusted email is refused with a clear error. The
guarantee is about origin, not truth: a household member can be wrong, a curator can misread them,
and a true statement can still be a poor standing instruction. The evidence links and the evaluation
are what address that.

**Tainted stretches are skipped before the model call, and the loss is measured from day one.** The
review task checks the merged taint of the unreviewed rows up front, and when it exceeds the ceiling
it skips the stretch, advances the watermark, and records the skip. This is the conservative choice,
and for this assistant its cost may be large: a user who says "for family hotels we need a separate
sleeping area for the children" and then has the assistant search hotel sites loses that preference
to the research that followed it, and research-heavy conversations are exactly where durable
preferences and decisions tend to emerge. Skip observability is therefore part of the first
milestone: how many stretches and how much user text are skipped, with a sampled review of skipped
stretches to estimate the useful facts lost. The likely refinement, reviewing the user's own rows
under their own recorded provenance when only later rows in the stretch are tainted, is the first
item under future hardening, and the measurement decides when it is built. Tool result bodies are
omitted from the rendered transcript in any case: the user's words and the assistant's replies carry
what mattered, and tool output is where injected text lives.

### What the curator is asked to do

The curator prompt is short and operational. Its instructions, at approach level:

- Remember durable things: standing preferences and their corrections, facts about people and the
  household, decisions and their reasons, routines, and the state of anything the family is working
  on across conversations, including the constraints, rationale, rejected options and open decisions
  around an ongoing trip or project.
- Do not remember one-off requests, appointment timing (the calendar owns when things happen),
  device state, verbatim tool output, secrets or credentials, anything a speaker plainly meant for
  one person, or sensitive personal matters the user did not ask to have kept.
- Record only what a person said: a statement, a correction, a decision. Do not infer a preference
  from a pattern; if nobody said it, it is not a memory. An assistant suggestion is not a household
  fact until a person accepted it. Say in the text when an entry is a correction or a decision.
- Date every entry with when it was asserted, and where it matters, the period it applies to. A
  statement made today about how things were years ago does not supersede a current preference.
- Update rather than add. A changed fact replaces its entry; a contradicted one is removed with the
  new evidence cited. Do not re-add something a person has removed.
- Attribute facts to a person. In a group chat the transcript carries who said what; "Alice prefers
  the tram" is a memory, "the user prefers the tram" is not.
- Keep the core note to standing facts and the topic index. Detail goes to a topic note. When the
  core is full, move the least standing entries out before adding.
- When nothing durable happened, propose nothing.

### Size management without a consolidation pass

The caps are the only size control in v1. When a review would take the core note over its ceiling,
the curator is asked in the same review to move detail to a topic note first; when a topic note
would overflow, it opens another. Near-duplicate entries across topics, and drift in the core note,
are tolerated and made visible in the notes UI, where a person can tidy them. An autonomous
consolidation pass is future hardening, gated on the notes actually accumulating the mess it would
clean up.

### Telegram

Telegram needs no separate mechanism, but three of the rules above exist because of it:

- The watermark and maximum deferral, because a chat id never ends.
- Per-person attribution, because a group chat is one conversation with several speakers, and the
  rendered transcript names the sender of each user message. That requires the persisted rows to
  carry it: the Telegram batcher today joins messages that arrive within its window and persists
  them under the last sender's identity, and a message that arrives while another member's turn is
  running is steered into that turn carrying only a display name. The rule is that every persisted
  user row carries its own sender: the batcher must never merge messages from different senders, and
  mid-turn input must carry the sender's identity through to persistence. Both changes are part of
  the Telegram milestone, since attribution that the transcript cannot support is not attribution.
- A longer idle window than the web, because Telegram conversation is bursty and a household member
  replying twenty minutes later is still the same exchange.

Profiles switched by slash command inside one chat (`/engineer`, `/coder`) are handled by
eligibility: only rows from contributing profiles are rendered into the review.

### User visibility and control

Memory notes are ordinary notes in the notes UI, distinguished by their label, and each entry shows
when it was asserted, who said it, and links to the messages it came from. Evidence text stays with
conversation ownership: the cited turn's text is shown only to a reader the existing sole-owner rule
already admits to the source conversation, and that rule is left exactly as it is; a group chat has
no sole owner, so a memory learned there shows its evidence text to nobody. Every other memory
reader sees the entry's provenance summary instead: who said it, in which kind of conversation, and
when. A cited turn can carry a surprise or a sensitive aside alongside the durable fact the curator
kept, and no amount of excerpting makes the turn itself safe for the whole household.

A **recent-changes view** lists what each review added, replaced or removed, with the edit's
before-and-after text and its evidence, so a person can see what the notebook learned and fix it in
the notes UI. It is an audit view, not a transactional undo: reverting a change is an ordinary edit
made by a person, and it wins over in-flight curator work like any other. A subtle indicator in the
chat surfaces that memory changed after a conversation, without a notification per fact. A
deployment can turn contribution, reading, or the whole mechanism off. The user documentation is a
new `docs/user/memory.md` describing what the assistant remembers on its own, what it never
remembers, that memory is household-wide, how to correct or forget, and that forgetting is not
erasure of history.

## Deliberate simplifications

Each of these is a chosen limitation of v1, with the reason it is acceptable.

- **One household scope.** Personal memory has a known shape and is not built; the user docs say
  memory is shared, and the curator leaves out what was plainly meant for one person.
- **No per-turn extraction in the background.** Foreground "remember" plus idle review is the
  starting cadence; the evaluation says whether the review misses things people wanted kept.
- **No separate memory store, no entry schema.** Entries are bullets with a date, a speaker and
  message references, recoverable from the rendered markdown.
- **Forgetting is not durable.** A removed fact can be learned again from a later statement or a
  later review of an old conversation. The docs say so; the recent-changes view shows it when it
  happens.
- **No semantic undo.** Reverting a curator change is an ordinary human edit.
- **No uniqueness guarantee.** The same fact can appear twice in different words; the curator is
  shown relevant entries and told to update rather than add, and a person can tidy the rest.
- **No consolidation pass.** Caps plus condense-on-overflow are the size control.
- **A minimal turn-completion rule.** A turn without a terminal reply is passed over once a later
  turn completes; a reply that lands after that is not reviewed.
- **Whole-stretch taint exclusion, measured.** The refinement is named and gated on the numbers.
- **Turning contribution off discards what was not yet reviewed.** Rows written while contribution
  was on but not reviewed before it was turned off lie before the next enablement moment and are
  never curated. One boundary per enablement keeps eligibility a single comparison.
- **Spoken interfaces read but do not contribute**, until their persistence produces message rows
  with real provenance. A test pins the exclusion so it stays visible.
- **The curator neither reads nor edits user-authored notes.** Findings that belong in a user note
  are written to a memory note; the user or the foreground assistant can merge them.
- **No approval queue for ordinary memories.** Wrong memories are corrected after the fact. An
  approval step for every fact would go unused and then be turned off.

## Future hardening

Nothing here is built until the stated evidence appears. Each item names the trigger.

- **Per-evidence provenance for the taint skip.** Review the user's own rows under their own
  recorded provenance when only later rows in a stretch are tainted. Trigger: the skip measurement
  from milestone 1 shows a material share of user text, or of useful facts in the sampled review,
  being lost.
- **Durable forgetting.** A suppression record for removed facts, honoured by the apply path across
  pending reviews and retries, with a rule for when a later human statement releases it. Trigger:
  removed facts observably returning, in the recent-changes view or the evaluation, at a rate people
  notice.
- **Entry identity and lineage.** A stable identity per entry so that updates, removals and undo can
  be keyed semantically rather than by current text. Trigger: durable forgetting or exact undo being
  built, since both need it and nothing else does.
- **Semantic undo.** Inverse operations per change kind in the recent-changes view. Trigger: people
  reverting curator changes often enough that hand-editing is a burden.
- **Duplicate detection at the apply path.** Trigger: the notes UI accumulating duplicates faster
  than people tidy them.
- **Consolidation pass.** Partitioned, evidence-preserving, with a change-share guard. Trigger: the
  core note repeatedly hitting its cap with genuine standing facts, or topic notes filling with
  near-duplicates.
- **Fuller turn-lifecycle modelling.** Confirmation liveness across restarts and late replies.
  Trigger: the never-finished marker or the orphaned-reply residual showing up in practice.
- **Personal scopes.** One core note per scope, an audience rule, a total injection bound. Trigger:
  a household wanting memory that is not shared.
- **Inferred entries.** Entries the curator derives from a pattern rather than a statement, with a
  provenance shape that says so instead of naming a speaker. Trigger: the evaluation showing that
  statements alone miss preferences people expected the assistant to pick up.
- **Per-turn consideration.** Trigger: the evaluation showing the idle review misses things people
  wanted kept.

## Residual risks

- A misread statement from a clean conversation becomes a standing entry until someone notices.
  Evidence links, the recent-changes view and the small core note bound the damage.
- A removed fact can return from a later statement or a later review of an old conversation. The
  docs say so, and the recent-changes view shows it.
- A conversation whose last turn never finished is not reviewed until the next turn in it completes,
  and a reply landing after its turn was passed over is not reviewed. Memory from those stretches is
  delayed or, rarely, lost; nothing false is learned in its place.
- Memory carries the provenance of the conversation that wrote it. An entry written from a
  trusted-pole conversation keeps that tier on readers. That is the correct propagation.
- Idle review is one model call per active conversation per idle period. On a chatty deployment this
  is tens of cheap calls a day; the no-user-messages skip and the contribute setting are the levers.

## Work plan

Each milestone is independently useful and verifiable. The evaluation comes early, because proving
usefulness is the point of v1.

1. **Curator profile, apply path, watermark, sweep, read policy, skip metrics.** A functional test
   drives a web conversation with a fake LLM that returns an edit list, advances the mock clock past
   the idle window, runs the sweep and the worker, and asserts the expected entries exist with
   dates, speakers and message references; that a conversation with recent activity is not enqueued;
   that rows before the enablement moment are never reviewed; that a re-run after the watermark
   reviews only new rows; that a turn without a terminal reply ends the chunk, is passed with a
   marker once a later turn completes, and is not reviewed before then; that a stretch larger than
   the chunk budget is reviewed across successive sweeps; that an abandoned review advances the
   watermark; and that a stretch carrying unknown-external taint is skipped with an audit record and
   counted. The apply path is verified to refuse an edit citing evidence outside the stretch, an
   edit whose grounding message is an assistant row or an uncited row, an over-cap result, a second
   always-loaded memory note, an edit that turns the core note's always-loaded flag off, and a write
   from a turn above the trusted pole, from the UI and foreground tool paths alike; to fail a whole
   list on one rejected edit; and to fail against a store the person edited during the review, with
   the retry proposing against the fresh store. Serialisation is verified by two due conversations
   producing two applies in sequence, each seeing the other's result. The read policy is verified by
   seeding an unlabelled note, a default-labelled note and an unlabelled file-based skill and
   asserting none reaches the curator through the context provider, the title list, the skill
   catalogue or `get_note`, including the file-skill fallback; a conformance rule asserts every note
   or skill read the curator can reach goes through the policy, that its write policy carries the
   `memory` floor, that its effective tool set is exactly the memory tools, and that its effective
   context provider set is exactly the notes provider. Skip counters and skipped-volume gauges land
   here, on the existing metrics surface.
2. **Prompts, settings and documentation.** The curator prompt in `prompts.yaml`; the read and
   contribute settings on the profiles that carry them and the contributing-interface list, both
   shipping off by default, so a deployment opts in explicitly until milestone 7 flips the default;
   a line in the assistant system prompt about what memory is and how to honour "forget";
   `docs/user/memory.md` stating the household scope and what forgetting means; the settings in the
   configuration reference. Verified by the existing prompt-render startup check, a startup
   validation that a contributing profile reads, and a test that a read-only profile sees the core
   note and does not feed reviews.
3. **Evaluation.** A replay corpus of synthetic conversations with expected outcomes: nothing worth
   remembering, a correction, a tentative plan, an assistant mistake, several speakers, a deliberate
   forget, and useful user facts mixed with research. Each case is scored on what the curator
   proposed and on a later question, in a fresh conversation, answered with and without the
   resulting memory, so that retrieval of an old topic is tested and not assumed. It tracks
   unsupported entries, missed useful facts and retrieval failures. The standard tier is compared
   against a stronger model before the cheaper one is taken as sufficient, and a sample of skipped
   stretches from a real deployment is scored for lost facts. Verified by the corpus running in CI
   with thresholds. This milestone decides whether the taint refinement is built next.
4. **Telegram: attribution and maximum deferral.** Sender names in the rendered transcript, a
   batcher that never merges messages from different senders, mid-turn input persisted under its own
   sender, the maximum-deferral clause of the due predicate, the longer idle window, and Telegram
   admitted to the contributing-interface list. Verified by Telegram functional tests with two
   senders posting inside one batching window and with the second posting while the first's turn is
   running, each attributed correctly, and a continuously active chat that is still reviewed.
5. **User control.** Evidence links on entries with the owner-only text rule, the recent-changes
   view, and the chat indicator. Verified by frontend tests and by tests that a member who does not
   own the source conversation sees the provenance summary and no transcript text, that every
   participant sees only the summary for a group-chat memory, and that the sole owner of a private
   conversation can open the cited turn.
6. **Per-evidence provenance**, if milestone 3 says so. The user's own rows reviewed under their own
   provenance when later rows are tainted. Verified by the hotel example: the preference is learned
   and the research is not.
7. **Default on.** Contribution on by default for the household profile, with the interface list
   naming web and iOS, and Telegram too once milestone 4 has landed. Gated on milestones 3 and 5, so
   the default arrives with the quality measurement and the controls to notice and correct a bad
   entry. Verified by a startup test of the shipped defaults.

## Open questions

- Idle windows. Proposed starting points: 30 minutes for web, 90 minutes for Telegram, 24 hours
  maximum deferral. These are settings, not design; the question is whether to ship them as defaults
  or leave memory off until a deployment sets them.
- Whether `complex_tasks` contributes from the start, or reads only. The proposal says it
  contributes.
- Where the household-scope statement should surface beyond the user documentation: once, in the
  chat, when memory first writes something, or only in the docs.

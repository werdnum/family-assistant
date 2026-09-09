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

## What other harnesses do

A survey of Claude Code auto-memory, ChatGPT memory, Claude.ai memory, Letta sleep-time agents,
mem0, OpenClaw, Hermes, Gemini CLI and Codex CLI converges on a few points that hold across
otherwise different systems.

- **Two layers, consolidated on a gate.** Every mature design keeps raw episodes (transcripts, daily
  notes, archival) separate from a small curated layer, and moves raw to curated on a trigger
  (session idle, compaction, nightly, every N steps) rather than on every turn. Per-turn extractors
  (mem0, ChatGPT's `bio` tool) are where trivia and duplicates come from.
- **Idle-triggered extraction works.** Codex CLI extracts "in the background after a chat goes
  idle"; Claude Code's consolidation runs between sessions; Letta's sleep-time agent runs after the
  primary agent pauses. Extracting from a finished stretch of conversation avoids summarising
  half-done work and lets the extractor see the resolution, not just the question.
- **The always-injected layer has a hard cap, and writes past the cap fail loudly.** Hermes and
  Claude Code return an error that makes the model merge or delete before writing. Silent truncation
  is the reported failure mode.
- **Emit operations, not prose.** Update the existing entry, remove the contradicted one, add the
  new one. Free rewrites drift; one study found continual consolidation lost half of the facts it
  was consolidating, and keeping the raw episodes doubled accuracy.
- **Absolute dates on entries.** "Next Tuesday" is meaningless a month later.
- **Say what not to remember.** Anything derivable from tools, one-off requests, secrets, and
  content from untrusted sources. Codex has a switch to skip sessions that used web or MCP tools;
  OpenClaw never promotes from tainted sessions. Memory poisoning through a silent background turn
  is a documented attack class.
- **Memory must be visible and editable in plain text.** ChatGPT's opaque derived profile is the
  canonical complaint; the fix for a wrong fact is to let the user see and change it.

## Design

### Memory lives in notes

Notes are already the assistant's long-term memory: they are user-visible and editable in the web
UI, visibility-labelled, indexed for search, injected into context, and stamped with the provenance
of the turn that wrote them. This design adds no second store. Memory is a set of notes carrying a
`memory` visibility label, in two tiers that mirror the survey's index-plus-topics shape:

- **One always-loaded memory note** (`include_in_prompt=true`), short and capped. Standing facts
  about the household and its members, standing preferences, and pointers to the topic notes.
- **Topic memory notes** (`include_in_prompt=false`), one per person, project or recurring theme.
  Their titles appear in every turn's "Other available notes" list and their content is reachable
  with `get_note` and `search_documents`.

The always-loaded layer is exactly one note, and the notes repository enforces that shape for every
writer: a memory-labelled note may be `include_in_prompt` only if it is that one note, and a write
that would leave it over the ceiling is refused. The curator gets an error telling it to condense,
the foreground assistant gets the same tool error, and the notes UI shows it to the user. Enforcing
the singleton and the cap together is what makes "capped" a statement about the prompt rather than
about a note; a per-note cap that many notes could each sit under would bound nothing.

Explicit requests ("remember that...", "forget that...") keep working in the foreground turn: the
opted-in profiles hold the `memory` grant and edit the memory notes directly. The background process
described next is the complement for everything the user did not ask to have saved.

### A curator profile reviews each conversation when it goes idle

The trigger is per conversation, not per turn and not a nightly sweep. A conversation becomes
reviewable when it has unreviewed user activity and has been quiet for an idle window. The review
runs as a background task under a new `memory_curator` processing profile. The curator is given the
unreviewed stretch of transcript as its input, sees the current memory notes through the notes
context provider, and updates the memory notes with whatever in that stretch should outlive it.

**Why idle-per-conversation over a nightly sweep.** Freshness: a preference stated at lunch is
available at dinner. Coherence: a single conversation is a natural unit with a beginning and a
resolution, whereas a day's slice of a Telegram chat is an arbitrary window. Cost is comparable: one
cheap-model call per conversation that had new content, versus one large call over everything. A
nightly pass still has a role, described under Consolidation below, but it operates on the memory
notes, not on transcripts.

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

**Memory writes are conditional on what the writer read.** Two conversations can go idle together
and the worker pool will run both curators at once; a user can edit a memory note in the UI while a
review is in flight. Every memory write therefore carries the version of the note the writer read,
and the repository applies it only if the note is still at that version. A stale write fails the
review, which is retried from scratch against the fresh state; because the curator updates in place
rather than appending, a retry after a partially applied review does not duplicate what already
landed. The watermark advances only after every write in the review has succeeded. This is one rule
at the write chokepoint instead of a lock around the review, and it protects the user's foreground
corrections the same way it protects a sibling curator's.

**Eligibility.** A stretch of transcript is reviewed only when the interface is one a household
member talks through (web, iOS, Telegram, telephone) and the turns ran under a profile that opts
into memory. Email intake, A2A, delegation subconversations, automation-triggered turns and internal
profiles such as the engineer, media analyst and event handler do not feed memory. The opt-in is a
profile setting so a deployment can add or remove profiles without code, and it is one setting, not
two: a profile that contributes to memory also reads it, holding the `memory` grant so the
always-loaded note and the topic-note titles reach its turns and its foreground "remember" and
"forget" requests can edit them. A profile that fed memory it could not see would be a configuration
error, and startup validation treats it as one. If the unreviewed stretch contains no user messages,
the review is skipped and the watermark advanced.

### The curator is a confined agent

In Rule-of-Two terms the curator reads sensitive data and writes state, so its writes are confined
to the memory notes and nothing else. It is configured, not coded, using the confinement that
already exists:

- **Write policy**: `required_note_visibility_labels: [memory]`. The repository-level policy then
  refuses to create or overwrite any note outside that label set, so a bad review cannot damage the
  user's own notes.
- **Read grants**: the `memory` label only. The curator sees the memory notes and nothing else the
  household has filed. That costs it the ability to notice that a fact is already in a user note,
  which is a small duplication cost; what it buys is that everything in the curator's context is
  content that has already passed the memory ceiling below, so no user note written from a tainted
  turn, and no indexed email or web page, can reach an unattended writer.
- **Tools**: reading and writing notes. No document search: it is the widest path from the indexed
  corpus into a silent turn, and the curator has no need of it. No delete tool: the write policy
  confines what the curator may create or overwrite, but deletion is not a write under that policy,
  so giving the curator `delete_note` would let it remove any note its read grants reach. A
  contradicted fact is removed by rewriting the note it lives in, which is what the curator does
  anyway. No messaging, no calendar, no egress, no delegation, no `wake_llm`, no scheduling. Nothing
  it does is user-visible except the note content.
- **Context**: memory notes only. No calendar, weather or Home Assistant; those are things memory
  should never duplicate.
- **Model**: the standard tier, with a small iteration ceiling. This is extraction, not reasoning.
- **History**: the curator's own rows are persisted in an internal subconversation, so they never
  enter the user's prompt window or the conversation list, but remain inspectable in diagnostics.

### The transcript is input, and its taint travels with it

The curator does not fetch history with `get_message_history`. That tool is a broadening sensitive
read, which the taint matrix turns into a confirmation at high taint, and there is no human present
to confirm. Instead the review task renders the unreviewed rows into the request text, the same way
a delegation carries its request, and seeds the curator's taint tracker with the merged taint of
those rows. Two consequences follow, both wanted:

- Every memory note the curator writes is stamped with the provenance of the conversation it came
  from, through the existing note provenance mechanism, and re-taints future readers exactly as a
  note written in the foreground would.
- The review task can decide, before spending a model call, whether the stretch is clean enough to
  learn from at all.

**Memory holds nothing above the known-user tier, whoever writes it.** This is the memory-poisoning
guard, and it is one invariant at the write chokepoint rather than a check on one input: the notes
repository refuses any write to a memory-labelled note whose provenance stamp exceeds the known-user
tier. For the curator that means a review whose turn taint has risen above the ceiling, from any
source, cannot write and fails visibly. For the foreground assistant it means a "remember this" in a
turn that has read an untrusted email is refused with a clear error rather than filed. Because the
ceiling is enforced on the way in, the memory notes are clean by construction, which is what lets
the curator's context be exactly those notes without a second gate on the read side. An instruction
planted in an email or a web page therefore cannot become a standing "fact" that every later turn
reads, by whichever path it tries to arrive.

**Tainted stretches are skipped before the model call.** The write-side rule is the guarantee; the
review task also checks the merged taint of the unreviewed rows up front, and when it exceeds the
ceiling it skips the stretch, advances the watermark, and records the skip in the audit log rather
than spending a model call on a review that could not write. It costs the household some memory from
conversations where the assistant did research, which is the accepted trade-off below. Tool result
bodies are also omitted from the rendered transcript; the user's words and the assistant's replies
carry what mattered, and tool output is where injected text lives.

### What the curator is asked to do

The curator prompt is short and operational. Its instructions, at approach level:

- Remember durable things: standing preferences and their corrections, facts about people and the
  household, decisions and their reasons, routines, and the state of anything the family is working
  on across conversations.
- Do not remember one-off requests, appointments and dated events (those belong in the calendar),
  device state, verbatim tool output, secrets or credentials, or sensitive personal matters the user
  did not ask to have kept.
- Update, do not append. Rewrite the entry that changed, delete the one that was contradicted, and
  honour an explicit "forget". Give every entry an absolute date.
- Attribute facts to a person. In a group chat the transcript carries who said what; "Alice prefers
  the tram" is a memory, "the user prefers the tram" is not.
- Keep the always-loaded note to standing facts and pointers. Detail goes to a topic note.
- When nothing durable happened, write nothing.

### Consolidation

Idle reviews are incremental and local to one conversation, so memory can accumulate near-duplicate
entries across topic notes and the always-loaded note drifts toward the cap. A consolidation pass
runs under the same curator profile over the memory notes alone, with no transcript, and merges
duplicates, resolves contradictions in favour of the later-dated entry, and prunes entries whose own
dates or wording mark them as expired. It has no calendar or tool access, so it never judges whether
something else now covers a fact; that is the curator's job at review time, by category. It is gated
on volume, not the clock: it runs when enough reviews have written since the last pass. It is a
later milestone; the incremental design is useful without it, and the cap keeps the always-loaded
note honest in the meantime.

### Telegram

Telegram needs no separate mechanism, but three of the rules above exist because of it:

- The watermark and maximum deferral, because a chat id never ends.
- Per-person attribution, because a group chat is one conversation with several speakers, and the
  rendered transcript names the sender of each user message.
- A longer idle window than the web, because Telegram conversation is bursty and a household member
  replying twenty minutes later is still the same exchange.

Profiles switched by slash command inside one chat (`/engineer`, `/coder`) are handled by
eligibility: only rows from opted-in profiles are rendered into the review.

### User visibility and control

Memory notes are ordinary notes in the notes UI, distinguished by their label. The user can read,
edit or delete any of them, and "forget that I said X" in chat is a foreground note edit. A
deployment can turn the whole mechanism off with one setting. The user documentation for this
feature is a new `docs/user/memory.md` describing what the assistant remembers on its own, what it
never remembers, and how to correct it.

## Deliberate simplifications

- **No per-turn extraction.** Foreground "remember" requests plus idle review cover the common case;
  the survey is unambiguous that per-turn extraction produces junk.
- **No separate memory store, schema or confidence scores.** Notes with a label, absolute dates in
  the text, and a consolidation pass are enough. Decay and reference-count promotion can be added to
  consolidation later if memory actually bloats.
- **Tainted conversations are skipped, not quarantined for later promotion.** A
  quarantine-and-review path is the right long-term shape (it is what the confined-writes design
  built for diagnostics), but it needs a human review step that does not exist for memory yet.
  Skipping loses some memory from research-heavy conversations and is safe.
- **The curator neither reads nor edits user-authored notes.** Findings that belong in a user note
  are written to a memory note; the user or the foreground assistant can merge them, and the
  foreground assistant, which sees both, can also deduplicate on the way. This keeps both the input
  and the blast radius of a review inside the memory label.
- **No approval queue for new memories.** Wrong memories are corrected after the fact through the
  notes UI or in chat. An approval step would go unused and then be turned off.

## Residual risks

- A wrong inference from a clean conversation becomes a standing fact until someone notices. The
  curator's instructions favour updating over adding and the always-loaded note is small, which
  bounds the damage; the notes UI and provenance stamp make it findable.
- Memory carries the taint of the conversation that wrote it. A memory note written from a
  known-user-tier conversation will keep that tier on readers. That is the correct propagation, and
  it is bounded by the skip rule above.
- Idle review is one model call per active conversation per idle period. On a chatty deployment this
  is tens of cheap calls a day; the review-skip on no-user-messages and the profile opt-in are the
  levers if it matters.

## Work plan

Each milestone is independently useful and verifiable.

1. **Curator profile, review task, watermark, sweep.** A functional test drives a web conversation
   with a fake LLM, advances the mock clock past the idle window, runs the sweep and the worker, and
   asserts a labelled memory note exists with the expected content and provenance; that a
   conversation with recent activity is not enqueued; that a re-run after the watermark reviews only
   new rows; and that a stretch carrying unknown-external taint is skipped with an audit record.
   Concurrency is verified directly: a message persisted at any point during a review, including
   after the handler's last read and before the task is marked done, is covered by a later sweep;
   two reviews writing the same memory note leave both sets of facts in place, with one review
   retried; and a note edited between a review's read and its write is not overwritten. The
   repository is verified to refuse a second always-loaded memory note and an over-cap write from
   the UI and foreground tool paths alike, and a conformance check confirms the curator's write
   policy carries the `memory` floor and its tool set has neither delete nor document search.
2. **Prompts, grants and documentation.** The curator prompt in `prompts.yaml`; the memory opt-in on
   the profiles that carry it, with the `memory` grant it implies, and a line in the assistant
   system prompt about what the memory notes are and how to honour "forget"; `docs/user/memory.md`;
   the settings in the configuration reference. Verified by the existing prompt-render startup
   check, a startup validation that every opted-in profile holds the grant, a test that each
   opted-in profile sees the always-loaded note and can edit it, and a test that a foreground memory
   write from a turn above the known-user tier is refused.
3. **Telegram: attribution and maximum deferral.** Sender names in the rendered transcript, the
   maximum-deferral clause of the due predicate, and the longer idle window. Verified by a Telegram
   functional test with two senders in a group and a continuously active chat that is still
   reviewed.
4. **Consolidation pass.** Gated on review volume; merges, resolves, prunes. Verified by a test that
   seeds duplicate and contradictory entries and checks the later-dated fact survives, and that the
   pass refuses a rewrite that drops most of the existing entries.
5. **Observability.** Counters for reviews run, skipped-clean, skipped-tainted, notes written and
   cap rejections, on the existing metrics surface; verified by the metrics test pattern already in
   use.

## Open questions

- Idle windows. Proposed starting points: 30 minutes for web and telephone, 90 minutes for Telegram,
  24 hours maximum deferral. These are settings, not design; the question is whether to ship them as
  defaults or leave memory off until a deployment sets them.
- Whether the curator should see assistant replies at all, or only user messages. Replies carry the
  resolution of a discussion and are the more useful half; they are also where a tainted tool result
  could be echoed. The skip rule covers the second concern, so the proposal includes them.
- Whether `complex_tasks` and `telephone` opt in from the start. The proposal says yes for both.

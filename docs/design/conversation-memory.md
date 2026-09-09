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

The cap on the always-loaded note is enforced at the note write chokepoint (a size ceiling on the
curator's write policy), so an over-size write fails with an error telling the model to condense,
instead of quietly growing the per-turn prompt forever.

Explicit requests ("remember that...", "forget that...") keep working in the foreground turn: the
default assistant holds the `memory` grant and edits the memory notes directly. The background
process described next is the complement for everything the user did not ask to have saved.

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

**Idle detection uses an existing primitive.** Task ids prefixed `system_` have upsert semantics in
the task queue: re-enqueuing the same id replaces its `scheduled_at`. So the chokepoint that
persists a user message also upserts a review task for that conversation, due at
`now + idle window`. Each further message pushes the due time back. When the task finally runs, the
conversation has been quiet for the whole window. No new scheduler, no polling, and no in-memory
state that a restart would lose.

**A watermark, not a conversation boundary.** A small table records, per
`(interface_type, conversation_id)`, the last message reviewed. A review covers rows after the
watermark and advances it on success. This is what makes the design work on Telegram, where a
conversation is one chat id for its whole life and never "ends": the idle window supplies the
boundary, and the watermark keeps each review to the new material. It also handles web conversations
the user resumes days later.

**A conversation that never goes idle is still reviewed.** A busy Telegram group can push the due
time back indefinitely. The due time is therefore the earlier of `last activity + idle window` and
`first unreviewed message + maximum deferral`. A review of a still-active conversation is fine; the
watermark means the next one picks up where it left off.

**Eligibility.** A stretch of transcript is reviewed only when the interface is one a household
member talks through (web, iOS, Telegram, telephone) and the turns ran under a profile that opts
into memory. Email intake, A2A, delegation subconversations, automation-triggered turns and internal
profiles such as the engineer, media analyst and event handler do not feed memory. The opt-in is a
profile setting so a deployment can add or remove profiles without code. If the unreviewed stretch
contains no user messages, the review is skipped and the watermark advanced.

### The curator is a confined agent

In Rule-of-Two terms the curator reads sensitive data and writes state, so its writes are confined
to the memory notes and nothing else. It is configured, not coded, using the confinement that
already exists:

- **Write policy**: `required_note_visibility_labels: [memory]`. The repository-level policy then
  refuses to create or overwrite any note outside that label set, so a bad review cannot damage the
  user's own notes. Read grants include the default label so the curator can see existing notes and
  avoid duplicating a fact the user already filed.
- **Tools**: the note tools and document search. No messaging, no calendar, no egress, no
  delegation, no `wake_llm`, no scheduling. Nothing it does is user-visible except the note content.
- **Context**: notes only. No calendar, weather or Home Assistant; those are things memory should
  never duplicate.
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

**Tainted stretches are not reviewed.** If the merged taint of the unreviewed rows exceeds the
known-user tier, the review skips the stretch, advances the watermark, and records the skip in the
audit log. This is the memory-poisoning guard: an instruction planted in an email or a web page
cannot become a standing "fact" that every later turn reads. It costs the household some memory from
conversations where the assistant did research, which is the accepted trade-off below. Tool result
bodies are also omitted from the rendered transcript; the user's words and the assistant's replies
carry what mattered, and tool output is where injected text lives.

### What the curator is asked to do

The curator prompt is short and operational. Its instructions, at approach level:

- Remember durable things: standing preferences and their corrections, facts about people and the
  household, decisions and their reasons, routines, and the state of anything the family is working
  on across conversations.
- Do not remember one-off requests, anything the calendar or a tool already knows, verbatim tool
  output, secrets or credentials, or sensitive personal matters the user did not ask to have kept.
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
duplicates, resolves contradictions in favour of the later-dated entry, and prunes entries that are
stale or that the calendar or tools now cover. It is gated on volume, not the clock: it runs when
enough reviews have written since the last pass. It is a later milestone; the incremental design is
useful without it, and the cap keeps the always-loaded note honest in the meantime.

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
- **The curator cannot edit user-authored notes.** Findings that belong in a user note are written
  to a memory note with a pointer; the user or the foreground assistant can merge them. This keeps
  the blast radius of a bad review to the memory label.
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

1. **Curator profile, review task, watermark, idle trigger.** A functional test drives a web
   conversation with a fake LLM, advances the mock clock past the idle window, runs the worker, and
   asserts a labelled memory note exists with the expected content and provenance; that a second
   message before the window pushes the task back; that a re-run after the watermark reviews only
   new rows; and that a stretch carrying unknown-external taint is skipped with an audit record.
   Verified also by a conformance check that the curator's write policy carries the `memory` floor
   and the size cap.
2. **Prompts, grants and documentation.** The curator prompt in `prompts.yaml`; the default
   assistant's grant on the `memory` label and a line in its system prompt about what the memory
   notes are and how to honour "forget"; `docs/user/memory.md`; the settings in the configuration
   reference. Verified by the existing prompt-render startup check and a test that the foreground
   assistant can edit a memory note.
3. **Telegram: attribution and maximum deferral.** Sender names in the rendered transcript, the
   earlier-of-two due-time rule, and the longer idle window. Verified by a Telegram functional test
   with two senders in a group and a continuously active chat that is still reviewed.
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

# Conversation history and compaction

## Status

Implemented (#1353, #1355 and the Jev milestone); Jev relevance ships in shadow mode. Follows
[append-only-prompt.md](append-only-prompt.md), which makes every request the previous request plus
appended messages and leaves "the history window drops messages from the front" to this document.
Approach-level; construction detail belongs to the implementing PRs, and operator settings are in
[CONFIGURATION_REFERENCE.md](../operations/CONFIGURATION_REFERENCE.md#conversation-history-window).

## Problem

The prompt's history is the newest N message-history rows within an age cutoff (`get_recent`). Every
tool call batch and every tool result is its own row. Shipped defaults are 10 rows / 2 hours on
Telegram and 100 rows / 30 days on web, with no size budget anywhere on the load path.

- **Telegram forgets the request before last.** A turn with three tool calls is six to eight rows,
  so after one tool-heavy turn the next sees the last answer and its tool plumbing but not the
  question before it. Follow-ups like "and the other one?" fail, and the window is spent on the
  least useful part of old turns.
- **Web history is unbounded in size.** Full tool results ride along for up to 100 rows; this is the
  ~140k characters of history measured in `prompt-cache-turn-context.md`, and the path that ends in
  `ContextLengthError`.
- **The window slides on almost every turn.** Under append-only prompts each slide is a prefix edit:
  it misses the cache from the start of the conversation and drops every earlier thinking block on
  Claude models that bind thinking to its prefix.
- **The only compaction is an emergency.** After a provider rejects a prompt as too long,
  `prune_messages_for_context` keeps placeholders for old tool results and drops everything but the
  last three turns, including the user's earlier requests.
- **Thread replies load the whole thread**, from every profile, with no size or age limit.
- **Old images are re-sent as bytes every turn**, and past errors replay their tracebacks.

The raw history is not lost: every row is persisted, `get_message_history` retrieves it (structured
and semantic), and the memory curator distils durable facts. The prompt window does not have to be
the only memory, and the model can already fetch its own history when it knows something is there.

## Approach

**History is built from whole turns against a size budget, and compacted at discrete events. Between
compaction events the history window makes no edits to the prompt, only appends.**

### Turns and budget

- The loader works in turns, never rows: a turn is the rows its own turn wrote, and a row with no
  turn id (a proactive send) is a turn of its own. A turn is never split. The active profile's
  history filtering and subconversation filtering apply to every input, including thread replies,
  which today load every profile's rows.
- The window is bounded by a character budget and a minimum number of turns, set per profile and
  interface, within the existing age cap. Cache reads cost 5-25% of normal input on current
  providers, so a warm append-only history is cheap; the budget exists for attention and noise more
  than for cost or the context window, and can be generous where the model is.
- Pinned rows, the replied-to thread (every turn in it, including the message replied to) and
  resumed turns are inputs to the one loader, so they are rendered and de-duplicated once. Every
  path that builds a window, including the web turn producer's taint computation, goes through it.

### Compaction events

- Each compaction event decides, for every turn up to that event, whether it renders verbatim,
  compacted, or not at all; the kept-verbatim turns need not be contiguous. Every event acts only on
  completed turns: the active turn, including a resumed one, is always sent whole. Turns after the
  latest event render verbatim. **Anything that removes or re-renders an earlier turn happens only
  at a compaction event**, and that includes the age cap: turns that age out between events leave at
  the next one. The decision is recorded with the event, and the compacted rendering is derived
  deterministically from stored rows, so replays are byte-identical without persisting a summary.
- **Events, with hysteresis.** A compaction event runs at the start of a model turn, whether a user
  message or a system wake such as a delegation completion triggered it (never inside a tool round),
  whenever the window has to change: the whole rendered window, compacted turns included, passes the
  budget; a turn in it passes the age cap; the request explicitly refers to a turn outside it; or,
  on Telegram, the message is the first after an idle gap. A budget event compacts well below the
  budget, so the next one is many turns away. Provider caches live for minutes, so after an idle gap
  the cache is already cold and compacting there costs almost nothing extra.
- **Compacted rendering.** A compacted turn keeps what the user said and the assistant's final
  answer, and reduces each tool call to a one-line stub that names the tool and says the detail is
  retrievable with `get_message_history`. Old attachments become references (id, type, filename)
  instead of bytes. Errors render as one line. A turn that still does not fit is dropped whole,
  oldest first. Stubs matter: a model that sees "09:12 asked about two plumber quotes" will look it
  up when asked "and the other one?"; a model that sees nothing will guess. Compaction only produces
  references the profile can follow: a tool stub needs `get_message_history`, an attachment
  reference needs a tool that reads attachments. For a profile without them (such as `council`),
  those turns are kept verbatim or dropped whole.
- **Provider constraints at an event.** On Claude, every thinking block after the first changed
  message loses its binding, in kept turns and in the active turn alike; the compaction strips them
  (text and tool calls stay) rather than leaving the API to drop them, so `prefix_binding_mismatch`
  keeps meaning a bug. On Gemini, thought parts inside a kept turn are never trimmed; whole older
  turns are compacted or not. A compacted turn carries no provider replay state (such as the OpenAI
  response output an adapter would otherwise prefer over the messages), so every adapter renders it
  from its compacted messages.
- **Tool activations follow their turn.** Under append-only prompts the active tool set comes from
  the activating messages in history, and an activation lasts until the window drops the message
  that made it. A compacted turn keeps its activations (the stub carries them); a turn that is
  dropped, whether for budget or age, takes its activations with it, as `append-only-prompt.md`
  specifies, and the model can activate them again.
- **The emergency fallback** becomes a compaction event at a smaller budget through the same
  renderer, and `prune_messages_for_context` is deleted. It is the one event that can run inside a
  tool round, where today's retry runs.

### Relevance at compaction events

At a compaction event a classifier ranks which turns still in the window, verbatim or compacted, the
new message continues, and the compaction keeps those verbatim in preference to the rest. The budget
is enforced in code, not by the classifier: relevance only decides which turns fill it. If the
classifier times out or fails, the compaction proceeds oldest-first as if it had returned nothing.
Between events the result is frozen. The active turn, the newest one or two turns within the age cap
(compacted if need be) and the turns the user explicitly points at always stay; these are the
mandatory set. The explicit references are the thread being replied to and pinned rows, which stay
even if an earlier event or the age cap dropped them. When the mandatory set alone exceeds the
budget it is sent anyway, and a provider context-length failure then reaches the user as it does
today. That is rare and is left there.

The classifier is TypeSafe Jev, an operator-configured integration. It sends the new message and the
candidate turns' text to TypeSafe, a new external processor the owner has accepted; a deployment
without it compacts oldest-first, as on classifier failure, and keeps its configured Auto
classifier. Jev gives a typed, calibrated yes/no per candidate turn, around 100 ms, priced per input
token at a level that is negligible per message. Its state is small (the new message plus each
candidate turn's user text and final answer), and time gaps are computed in code, not asked of it.
All questions in one request read the same state, so untrusted text in a candidate turn can
influence every answer in that request. That is bounded by what the answers control: the classifier
has no tools and its output is schema-constrained, so the worst case is that the wrong turns are
kept verbatim within the budget and the right ones compacted or, for a profile without
`get_message_history`, dropped. Shadow mode measures that the same way it measures any wrong choice.

Auto keeps deciding per request, as today, and moves to Jev as a choice question among the profile's
`auto_model_tiers` with the profile's routing guidance as its rubric. Its inputs stay those of
today's classifier (bounded recent history, the request, attachment metadata and the profile), and
it is its own request, never sharing state with relevance questions. That replaces an LLM call with
a ten-second timeout and adds a probability over tiers to tune a threshold against. Its exposure to
injected history is the same as today's classifier: the answer cannot leave `auto_model_tiers`. A
tier switch drops earlier thinking on Claude, so shadow mode reports how often Auto would switch, as
a cost to weigh, not a reason to hold the tier.

### Taint

Unchanged. Every row written after a tool result carries that result's source as introduced, so a
turn's final answer keeps the taint of what it read for as long as it is in the prompt. Compaction
neither launders nor heals taint; a conversation heals when the whole turn leaves the window, as
today. Window taint is computed over the rendered window by the same loader on every path.

## Deliberate simplifications

- **Other history edits are out of scope.** `append-only-prompt.md` also leaves trigger attachment
  metadata, delegation wake triggers, the final-iteration instruction and reply-thread context as
  edits to earlier requests. This document removes only the history window's edits; those stay as
  they are, and the append-only verification below covers turns without them.

- **No LLM summary.** Anthropic recommends client-side "simple compaction" (summarise everything
  into one message) and reports it performs comparably to more elaborate schemes. Deterministic
  stubs come first because they need no extra call, give the same output every time, and do not
  create a persisted summary carrying the union taint of everything it read. If shadow data shows
  the model often fetching history after a compaction, an LLM summary at compaction events is the
  next step.

- **No provider-side compaction or context editing.** Anthropic's server-side compaction and context
  editing and OpenAI's `/responses/compact` produce provider-specific state (OpenAI's is opaque)
  that cannot be replayed on a fallback provider, and they hide which rows are in the prompt, which
  window taint depends on. A client-side compaction renders identically on every adapter.

- **Relevance is decided only at events.** A turn the user returns to between events stays wherever
  the last event put it, and a turn already dropped is not a candidate again; the stub, where there
  is one, and `get_message_history` cover those cases.

- **Budgets start as estimates** and are tuned from the provider-reported prompt and cache token
  counts already recorded in diagnostics. No tokenizer is added.

## Milestones

1. **Turn-aligned budgeted loader**, one line per error, pins and threads through the loader.
   Verified by tests that a window never splits a turn, that a Telegram conversation with tool-heavy
   turns keeps the previous request, and that two consecutive turns with no event between them
   render as strict prefix plus append.
2. **Compaction events and rendering**, including attachment references, thinking stripped from kept
   Claude turns, activations kept by compacted turns, and the emergency fallback moved onto the
   renderer. Verified by tests that compacting a turn does not change the active tool set or reduce
   taint relative to the verbatim turns, a compacted history renders as stubs, and is accepted, on
   each provider adapter, and the cached-token share in diagnostics does not regress.
3. **Jev relevance and Auto in shadow mode.** Probabilities and tier choices are logged beside the
   current behaviour. Verified against whether the model then called `get_message_history`, whether
   the user had to repeat themselves, and the outcomes Auto shadow mode already records; it switches
   on only where it does at least as well.

# Conversation history and compaction

## Status

Proposed. Follows `append-only-prompt.md`
([PR #1348](https://github.com/werdnum/family-assistant/pull/1348)), which makes every request the
previous request plus appended messages and leaves "the history window drops messages from the
front" to this document. Approach-level; construction detail belongs to the implementing PRs.

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

**History is built from whole turns against a size budget, and compacted at discrete points. Between
compaction points the prompt is append-only.**

### Turns and budget

- The loader works in turns, never rows: a turn is the rows its own turn wrote, and a row with no
  turn id (a proactive send) is a turn of its own. A turn is never split. Profile and
  subconversation filtering stay as they are.
- The window is bounded by a character budget and a minimum number of turns, set per profile and
  interface, within the existing age cap. Cache reads cost 5-25% of normal input on current
  providers, so a warm append-only history is cheap; the budget exists for attention and noise more
  than for cost or the context window, and can be generous where the model is.
- Pinned rows, thread roots and resumed turns are inputs to the one loader, so they are rendered and
  de-duplicated once. Every path that builds a window, including the web turn producer's taint
  computation, goes through it.

### Compaction points

- The loader keeps a **compaction point**. Turns before it render compacted; turns after it render
  verbatim. The point moves only at a compaction event, and both the point and the compacted
  rendering are derived deterministically from stored rows, so replays are byte-identical without
  persisting a summary.
- **Events, with hysteresis.** A compaction runs at the start of a user turn (never inside a tool
  round) when the verbatim part passes the budget, and compacts well below it, so the next event is
  many turns away. On Telegram, the first message after an idle gap is also an event. Provider
  caches live for minutes, so after an idle gap the cache is already cold and compacting there costs
  almost nothing extra.
- **Compacted rendering.** A compacted turn keeps what the user said and the assistant's final
  answer, and reduces each tool call to a one-line stub that names the tool and says the detail is
  retrievable with `get_message_history`. Old attachments become references (id, type, filename)
  instead of bytes. Errors render as one line. A turn that still does not fit is dropped whole,
  oldest first. Stubs matter: a model that sees "09:12 asked about two plumber quotes" will look it
  up when asked "and the other one?"; a model that sees nothing will guess.
- **Provider constraints at an event.** On Claude, the verbatim turns kept across a compaction lose
  their thinking blocks, because those blocks were bound to the longer prefix; the compaction strips
  them (text and tool calls stay) rather than leaving the API to drop them, so
  `prefix_binding_mismatch` keeps meaning a bug. On Gemini, thought parts inside a kept turn are
  never trimmed; whole older turns are compacted or not.
- **Tool activations survive.** Under append-only prompts the active tool set comes from the
  activating messages in history; a compaction must never change it.
- **The emergency fallback** becomes a compaction event at a smaller budget through the same
  renderer, and `prune_messages_for_context` is deleted.

### Relevance at compaction events

At a compaction event a classifier decides which turns since the previous event the new message
continues; those stay verbatim and the rest are compacted. It never removes the newest one or two
turns, and between events its decision is frozen.

The classifier is TypeSafe Jev: a typed, calibrated yes/no per candidate turn, around 100 ms, priced
per input token at a level that is negligible per message. Its output is constrained to the schema,
so injected history can at worst flip one keep-or-compact decision. Its state is small (the new
message plus each candidate turn's user text and final answer), and time gaps are computed in code,
not asked of it. On timeout or error it keeps everything verbatim.

The same request also carries the Auto tier question, which is already a schema-constrained choice
among the profile's `auto_model_tiers`, as a Jev choice question with the profile's routing guidance
as its rubric. That replaces an LLM call with a ten-second timeout and adds a probability over tiers
to tune a threshold against. A tier switch also drops earlier thinking, so Auto should hold its tier
between compaction events, or at least report how often it flips.

### Taint

Unchanged. Every row written after a tool result carries that result's source as introduced, so a
turn's final answer keeps the taint of what it read for as long as it is in the prompt. Compaction
neither launders nor heals taint; a conversation heals when the whole turn leaves the window, as
today. Window taint is computed over the rendered window by the same loader on every path.

## Deliberate simplifications

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
  the last event put it; the stub and `get_message_history` cover that case.
- **Budgets start as estimates** and are tuned from the provider-reported prompt and cache token
  counts already recorded in diagnostics. No tokenizer is added.

## Milestones

1. **Turn-aligned budgeted loader**, one line per error, pins and threads through the loader.
   Verified by tests that a window never splits a turn, that a Telegram conversation with tool-heavy
   turns keeps the previous request, and that two consecutive turns with no event between them
   render as strict prefix plus append.
2. **Compaction points and rendering**, including attachment references, thinking stripped from kept
   Claude turns, unchanged tool activations, and the emergency fallback moved onto the renderer.
   Verified by tests that a compaction does not change the active tool set or reduce taint relative
   to the verbatim turns, a Gemini request with a compacted history is accepted, and the
   cached-token share in diagnostics does not regress.
3. **Jev relevance and Auto in shadow mode.** Probabilities and tier choices are logged beside the
   current behaviour. Verified against whether the model then called `get_message_history`, whether
   the user had to repeat themselves, and the outcomes Auto shadow mode already records; it switches
   on only where it does at least as well.

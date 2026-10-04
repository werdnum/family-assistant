# Append-only prompts: no per-turn context block, persistent tool activation

## Problem

[prompt-cache-turn-context.md](prompt-cache-turn-context.md) moved the clock and the context
providers' output out of the system prompt into a trailing `<turn_context>` user message that is
rebuilt on every turn and never persisted. That made the system prompt and history cacheable, but
the block still leaves a hole: on the next turn the message it was merged into comes back without
it, so everything from that point on is a different prefix.

Two costs follow:

- **Cache.** The previous turn's assistant and tool messages are re-read uncached on every turn
  boundary. The earlier design accepted this as cheaper than the alternatives available then.
- **Preserved thinking.** Claude Opus 5.5, Claude Sonnet 5.5 and Claude Fable 5.1 bind each thinking
  block to the exact `system`, `tools` and message prefix that produced it. A replayed block whose
  prefix changed is a 400 on accounts created after 2026-08-31, and is otherwise sent to the model
  with a recorded mismatch. Every turn boundary is such a change, so a conversation on the `deep` or
  `frontier` tier either errors into its fallback model or carries invalid reasoning from its second
  turn on.

Three other places edit history the same way (the first is fixed for every provider: assistant text
is now replayed exactly as it was sent):

- `format_history` strips the text from an assistant message that also made tool calls, unless it
  carries a Gemini thought signature.
- `activate_tools` rewrites the system prompt and changes the tool list in the middle of a turn, and
  activations reset at the end of each turn, so the tool list changes back on the next one.
- The on-demand catalog in the system prompt shrinks as tools are activated.

## Approach

**Every request is the previous request plus appended messages.** Content is placed by how often it
changes, and nothing is placed where a later request would have to remove or rewrite it.

### The turn-context block goes away

What the block carried splits by volatility:

| Content                                                                           | Changes                                     | Goes to                                                          |
| --------------------------------------------------------------------------------- | ------------------------------------------- | ---------------------------------------------------------------- |
| Always-loaded notes (including the core memory note), skills catalog, known users | When a note, skill or the config is written | The system prompt                                                |
| Calendar, weather, Home Assistant status                                          | Minute to minute                            | Tools the model calls when it needs them                         |
| The clock                                                                         | Every request                               | The timestamp of each user message, rendered from the stored row |
| "Other available notes" title list                                                | Every new note; unbounded                   | Dropped; `list_notes` covers it                                  |

**Rarely-changing content in the system prompt.** The system prompt is rebuilt every request but is
byte-identical until a note or skill is written. A write costs one cache miss and the earlier turns'
thinking (dropped by the API, see below) on the next request of each conversation, and the content
can never be stale. The notes provider's taint sources move with it: they are still gathered with
the content and still gated on `include_aggregated_context`.

**Volatile data as tools.** The model asks for what it needs, when it needs it, and the answer is a
tool result: persisted, replayed verbatim, part of the append-only history.

- Calendar: `search_calendar_events` already returns a superset of what the provider showed.
- Weather: a new `get_weather_forecast` tool built from the provider's WillyWeather code, keeping
  its hourly cache.
- Home Assistant: a new `get_home_status` tool that renders the operator's curated
  `context_template`. The template is what tells the model which entities matter; without the tool
  the model could only reach them through `render_home_assistant_template` if it already knew them.

This also removes a synchronous CalDAV fetch and a Google round trip from the front of every turn.
The prompt tells the model to check the calendar before scheduling or answering time-sensitive
questions, because it no longer sees upcoming events without asking.

**The clock from stored timestamps.** Each user message is shown to the model with the time it was
sent, rendered in the profile's timezone from the row's `timestamp`. Replays are byte-identical with
nothing new to store, and the model learns when every message was sent, not only the latest. The
current turn's own message carries the current time.

**Voice** renders its system instruction once per session already; it gets the same system prompt
content and the same tools. The context viewer shows the system-prompt providers.

### Tool activation is persisted and appended

Activation becomes part of the conversation rather than turn-local state:

- A tool message that activated tools (an `activate_tools` call, or a skill loaded with `get_note`
  that declares tools) records the names it activated, persisted with the row.
- A request's active set is everything activated by the tool messages in its history, so an
  activation lasts for the rest of the conversation (until the history window drops the message that
  made it) instead of resetting each turn.
- The on-demand catalog in the system prompt always lists every on-demand tool, active or not, so
  activation never changes the system prompt.
- The loop always passes the full tool list, with on-demand tools marked deferred. That is the one
  contract every provider adapter implements:
  - **Anthropic** declares deferred tools with `defer_loading: true` and turns each recorded
    activation into a `tool_addition` block in a `role: "system"` message after the tool results
    that made it (beta `mid-conversation-tool-changes-2026-07-01`). The `tools` array never changes.
    Anthropic models without mid-conversation tool changes (Haiku 4.5, Sonnet 5 and older) take the
    filtering path below instead.
  - **Gemini and OpenAI** have no equivalent, so their adapters filter the list to the non-deferred
    tools plus those activated in the messages they were given. Their tool list sits at the front of
    the request, so an activation still costs one cache miss there; persisting activations means it
    costs that once per conversation rather than on every turn.

Making it an adapter contract rather than a loop decision matters because of fallbacks: the same
request can be retried on a different provider, and each adapter has to render it correctly.

### Anthropic

- User content is always sent as a block list, so a message has one wire shape whether or not a tool
  result or steering message merges into it. One cache breakpoint at the end of the conversation
  replaces the one that had to skip back over the turn-context block.
- Every request sends `thinking.block_binding.prefix_mismatch_behavior: "drop_block"` under the
  `thinking-binding-controls-2026-08-01` beta, so a remaining mismatch drops the affected thinking
  blocks instead of failing the request into its fallback. `input_transformations` is logged:
  `model_binding_mismatch` is expected after a tier switch; `prefix_binding_mismatch` means some
  code path still edits history.
- The `frontier` tier moves to Claude Fable 5.1. Forced tool choice was already removed from the
  client, which is the breaking change that blocked it.

## What is left (deliberate simplifications)

These still edit history and are left for a follow-up on history management and compaction:

- **The history window** drops messages from the front by count and age. Every removed message
  changes the prefix of every later thinking block, so each slide drops the earlier thinking (via
  `drop_block`) and misses the cache from the start of the conversation. The proper fix is
  server-side context editing or compaction rather than a client-side window.
- **Trigger attachment metadata** is injected into the trigger message in memory and not persisted.
- **Delegation wake triggers** are system messages, which the Anthropic adapter hoists into the
  top-level `system`, so a wake turn's system prompt differs from its neighbours'.
- **The final-iteration instruction** is a trailing message that is not persisted. It only appears
  when a turn exhausts its iteration budget.
- **Reply-thread context** (the attachment summary of a replied-to thread) is appended as a one-off
  message on the turns that reply to a thread. Those turns already replace the history with the full
  thread, so their prefix differs regardless.

## Verification

- Unit tests that render two consecutive turns' requests and assert the second is the first plus
  appended messages, on each provider adapter, including across a tool activation.
- Anthropic request-shape tests for `defer_loading`, `tool_addition` placement and the binding beta.
- The existing functional suites for the notes, calendar, Home Assistant and weather paths, updated
  for tools in place of preloaded context.
- In production, `input_transformations` with `prefix_binding_mismatch` should appear only after the
  history window slides or a note is written.

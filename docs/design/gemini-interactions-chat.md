# Gemini chat on the Interactions API, and native tool retrieval

**What's here:** moving the Gemini chat adapter from `generateContent` to the stateless Interactions
API, and then using Gemini's native tool retrieval (a client-side `tool_search`) for on-demand tools
on top of it.

## Why

Google documents Interactions as the default Gemini API and `generateContent` as legacy. It is to
`generateContent` what OpenAI's Responses API is to Chat Completions, a move this project has
already made for OpenAI. New capabilities land there first: tool retrieval exists only on
Interactions (`v1beta`); `generateContent` does not support it.

Native retrieval is what Gemini's prompt cache wants. Without it the Gemini adapter narrows the
declared tool list to what the history has activated (`resolve_deferred_tools`); the tool list sits
at the front of the request, so every activation is a cache miss. With retrieval the tool list is
fixed for the conversation and a loaded tool's definition arrives appended to history, the shape the
Anthropic adapter already has. Measured on `gemini-3.8-flash` with 41 tools, withholding 40 of them
took the first call of a turn from 9,906 to 906 input tokens, and the cached prefix grew on every
call of the loop.

## Milestones

1. **SDK 3** (PR #1717). google-genai 3.6.1, every client pinned to `v1beta` by one factory.
2. **Chat on Interactions** (PR #1719). Verified by the Gemini provider integration suite
   re-recorded against Interactions, a streamed tool call replayed into the next request, history
   begun on `generateContent` continued on Interactions, the whole tool catalog accepted in one
   request, and recorded steps surviving the database and a chat-API follow-up.
3. **Native tool retrieval** (this PR). On by default with a per-model kill switch, `activate_tools`
   is a client-side `tool_search` and on-demand tools are delivered in activation results. Verified
   by the chat-API tests run with the tool on demand (the model activates it, receives its
   definition, calls it, and a follow-up replays the turn), a property test that the declared tools
   never change across activations, and a recorded refusal that falls back.

## How milestone 2 works

**The message is the record; metadata keeps only what it cannot say.** Interactions requires the
model's steps back as received, signatures included; a tool call without one is rejected. A
response's text and tool calls are already the assistant message's `content` and `tool_calls`, so
`provider_metadata` (tagged `gemini_interactions`) stores only the opaque steps (thoughts and their
signatures, tool searches, server-side tool runs) in order, with an anchor where each piece of text
or tool call went. Replay fills the anchors from the message, as the Anthropic adapter rebuilds text
and tool use around its stored thinking blocks, so there is one source of truth for what the model
said: a call the message no longer has is left out, and one no anchor places is added unsigned. A
message with nothing stored (from `generateContent`, another provider, or a compacted turn) is the
same rendering with no opaque steps, its calls signed with their generateContent signature or the
documented skip-validator placeholder. User messages and tool results go through the existing part
conversion, so attachments and multimodal tool results behave as before.

**Streaming rebuilds steps from deltas.** The completion event carries usage but no steps, so the
adapter folds `step.start` / `step.delta` / `step.stop` into whole steps. Text streams to the user
as it arrives; tool calls are emitted when the stream ends.

**Tool schemas go through as JSON Schema.** The `generateContent` converter flattens schemas to what
its `Schema` type models, dropping nested objects and enums. Interactions takes JSON Schema and
ignores what it does not enforce, so schemas are sent as written, with the two constructs it rejects
outright (a `type` that is not a JSON Schema type, and tuple-form `items`) rewritten.

**Configuration maps once, at startup.** The configured `llm_parameters` are translated onto the
Interactions request when the client is built, and one without an equivalent fails there, naming the
model.

**Escape hatch.** `use_interactions_api: false` in a model's `llm_parameters` keeps its chat on
`generateContent`, mirroring `use_responses_api` for OpenAI. Computer use and the Interactions
agents (Deep Research, Antigravity) keep their own paths.

## How milestone 3 works

Gemini offers two retrieval modes. In **server-side** retrieval deferred tools are declared with
`defer_loading` and Gemini searches them itself, returning signed `tool_search_call` /
`tool_search_result` steps that must be replayed verbatim; activations made on our side cannot be
rendered, and a history that has lost a search step is rejected once a result for that tool is sent.
In **client-side** retrieval a `tool_search` tool with `execution: "client"` is an ordinary function
we implement, and its result carries the tool definitions. Client-side is the one used, because it
is what `activate_tools` already is.

**What changes in the request.** `activate_tools` is declared as the client-side `tool_search` (same
name, description and parameters); the on-demand tools are not declared at all. The adapter decides
this, so the same request can still fall back to another provider, which renders `activate_tools`
and its own deferral as before.

**Activation delivers the definitions.** Activation is recorded on the tool message that made it
(`activated_tools`), whether `activate_tools` or a skill's `get_note` made it, and the adapter adds
the activated tools' definitions to that message's `function_result`. Both kinds of activation are
therefore native, history is ordinary `function_call` / `function_result` steps, and compacting or
windowing history behaves as on every other adapter: a tool whose activation has dropped out is no
longer declared, and the model activates it again.

**Kill switch and fallback.** `native_tool_retrieval: false` in a model's `llm_parameters` turns it
off. Retrieval is a preview enabled per model; when the API refuses the `tool_search` (a 400 naming
tool search), that request is resent with the narrowed tool list and the client stops asking. A
refusal arrives before any output, so no streamed text is repeated.

## Deliberate simplifications

- Structured output stays on `generateContent`: it is a single call with no history to replay, and
  nothing about it improves on Interactions.
- Parameters with no Interactions equivalent (`safety_settings`, `thinking_budget`, ...) fail at
  startup instead of being approximated; `use_interactions_api: false` is the way to keep them.
- The `generateContent` chat path stays as the escape hatch rather than being deleted, as the Chat
  Completions path did for OpenAI.
- The fallback latches for the client's lifetime rather than retrying: a model gaining retrieval is
  picked up at the next restart.
- Server-side retrieval (`defer_loading`) is not used. It would hand the choice of tool to Gemini
  and bind history to signed search steps, for no saving client-side retrieval does not also give.

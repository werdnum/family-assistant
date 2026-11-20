# Gemini chat on the Interactions API, and native tool search

**What's here:** moving the Gemini chat adapter from `generateContent` to the stateless Interactions
API, and then using Gemini's native deferred tools (`tool_search` + `defer_loading`) on top of it.

## Why

Google documents Interactions as the default Gemini API and `generateContent` as legacy. It is to
`generateContent` what OpenAI's Responses API is to Chat Completions, a move this project has
already made for OpenAI. New capabilities land there first: native deferred tools exist only on
Interactions (`v1beta`); `generateContent` parses the same fields and ignores them.

Native deferral is what Gemini's prompt cache wants. Without it the Gemini adapter narrows the
declared tool list to what the history has activated (`resolve_deferred_tools`); the tool list sits
at the front of the request, so every activation is a cache miss. With native deferral the tool list
is fixed for the conversation and a loaded tool arrives as signed steps appended to history, the
shape the Anthropic adapter already has. Measured on `gemini-3.8-flash` with 41 tools, the first
call of a turn fell from 9,906 to 906 input tokens, and the cached prefix grew on every call of the
loop.

## Milestones

1. **SDK 3** (PR #1717). google-genai 3.6.1, every client pinned to `v1beta` by one factory.
2. **Chat on Interactions** (this PR). Verified by the Gemini provider integration suite re-recorded
   against Interactions, a streamed tool call replayed into the next request, history begun on
   `generateContent` continued on Interactions, the whole tool catalog accepted in one request, and
   recorded steps surviving the database and a chat-API follow-up.
3. **Native tool search.** Behind a per-model flag, deferred tools go to the API deferred with a
   server-run `tool_search` tool, and `activate_tools` is not declared. Verified by an integration
   test that loads a deferred tool, calls it, and finishes the turn from replayed history, and one
   that exercises the fallback.

## How milestone 2 works

**History is replayed, not reconstructed.** Interactions requires the model's steps back exactly as
received, signatures included; a tool call without one is rejected. The adapter records every model
step of a response in the assistant message's `provider_metadata`, tagged `gemini_interactions`, as
the Anthropic adapter records thinking blocks, and replays them verbatim. A message with no recorded
steps (one from `generateContent`, another provider, or a compacted turn) is rendered from its text
and tool calls, with the `generateContent` signature where there is one and the documented
skip-validator signature otherwise. User messages and tool results go through the existing part
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

**The flag.** `native_tool_search: true` in a model's `llm_parameters`. Off by default while the
feature is unannounced; it is per model because Google enables it per model.

**What changes in the request.** Deferred tools are declared with `defer_loading` and their summary
as `short_description`; a `tool_search` tool is added; `activate_tools` is left out. The adapter
decides this, so the same request can still fall back to another provider, which renders
`activate_tools` and its own deferral as today.

**Activation comes from the model's own search.** The server rejects search steps it did not sign,
so activations made on our side (a skill's `get_note`, or `activate_tools` on another provider
earlier in the conversation) cannot be rendered as loaded. They are not rendered at all: the tools
stay deferred and the model searches for them. A skill's `get_note` result already names its tools.
A history that calls a deferred tool with no search before it is accepted and the model searches
again, so a compacted or windowed history cannot break a request.

**Kill switch and fallback.** Setting the flag off is the kill switch. If the API rejects tool
search for a model (a 400 about tool search or deferred loading), that request is retried without
it, and the client stops asking for the rest of its life, logging once. A response that already
streamed text is not retried.

## Deliberate simplifications

- Structured output stays on `generateContent`: it is a single call with no history to replay, and
  nothing about it improves on Interactions.
- Parameters with no Interactions equivalent (`safety_settings`, `thinking_budget`, ...) fail at
  startup instead of being approximated; `use_interactions_api: false` is the way to keep them.
- The `generateContent` chat path stays as the escape hatch rather than being deleted, as the Chat
  Completions path did for OpenAI.
- An MCP server activated by a skill names only its server id, not its tools, so on milestone 3 the
  model finds those tools by searching on their summaries.
- A conversation that falls back from native search to Anthropic does not carry the native
  activations into Anthropic's `tool_addition` blocks; the model there activates again.
- The on-demand catalog in the system prompt still tells the model to use `activate_tools`;
  milestone 3 adjusts its instruction to say to load tools through tool search where
  `activate_tools` is not offered.

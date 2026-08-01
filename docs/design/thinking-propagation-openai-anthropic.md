# Thinking / Reasoning Propagation: OpenAI and Anthropic

Investigation of how reasoning state (extended thinking, encrypted reasoning items, thought
summaries) is captured, persisted and replayed for the `openai` and `anthropic` providers, using the
Google provider's thought-signature handling as the reference implementation.

## Summary

| Provider                               | Reasoning captured             | Persisted | Replayed to model             | Surfaced to user |
| -------------------------------------- | ------------------------------ | --------- | ----------------------------- | ---------------- |
| Google (Gemini)                        | thought signatures + summaries | yes       | yes                           | no               |
| OpenAI — Responses API (`gpt-5.6-sol`) | encrypted reasoning items      | yes       | yes                           | no               |
| OpenAI — Chat Completions (all others) | none                           | n/a       | n/a (API has no such concept) | n/a              |
| Anthropic                              | **none**                       | no        | **no**                        | no               |

The short version: OpenAI's Responses path is complete and correct. Anthropic has no reasoning
support of any kind — not a regression, it was never built — and the gap is latent rather than
visible only because extended thinking is never switched on. Thought summaries that *are* captured
(Google) are stored but never displayed, because the frontend reads a key the backend does not
write.

## How the reference implementation works (Google)

- `google_genai_client.py:1193-1215` (non-streaming) and `:2032-2069` (streaming) pull
  `part.thought_signature` off each `function_call` part and wrap it in `GeminiProviderMetadata`
  attached to the individual `ToolCallItem`.
- `llm_loop.py:519-537` lifts metadata off the first tool call (or the `done` event) onto the
  `AssistantMessage`, serialising it for storage.
- `storage/repositories/message_history.py:1112-1125` writes it to the `provider_metadata` JSON
  column; `:2247-2274` and `:2359-2386` rehydrate it into typed `GeminiProviderMetadata` on load.
- `google_genai_client.py:745-825` re-attaches signatures on the way back out, falling back to the
  `b"skip_thought_signature_validator"` sentinel when a call has none.
- `llm_loop.py:358-392` additionally freezes the system prompt for the whole turn when signatures
  are present, so the signed context is not disturbed.

Thought *summaries* take a separate route: they land in `reasoning_info["thought_summaries"]`
(`google_genai_client.py:1250-1253`, `:2101-2104`) and are persisted alongside the message.

## Findings

### F1 — Anthropic captures, stores and replays no reasoning state at all (latent break)

`anthropic_client.py:502-531` builds assistant turns from `content` and `tool_calls` only. It emits
`text` and `tool_use` blocks; `thinking` and `redacted_thinking` blocks are neither produced on
output nor accepted on input. The response parsers only recognise two block types (`:768-781`
non-streaming, `:1041-1077` streaming — no `thinking_delta` / `signature_delta` branch), and
`LLMOutput` is built without `provider_metadata` (`:791-795`).

Nothing sets a `thinking` parameter today, so this costs nothing right now. But it is a live trap:
`llm_parameters` entries are spread straight into `client.messages.create(**params)`
(`anthropic_client.py:743-749`, `:1009-1015`, wired via `factory.py:159-176`), so adding

```yaml
llm_parameters:
  "claude-": { thinking: { type: enabled, budget_tokens: 4096 } }
```

is enough to enable extended thinking — after which the first tool-use continuation fails, because
Anthropic requires the thinking blocks (with their `signature`) to be replayed in the assistant turn
that carries the `tool_use`. The two profiles that would hit this are `automation_creation` and
`engineer` (`defaults.yaml:894`, `:1633`, both `claude-sonnet-4-6`), plus `complex_tasks`' fallback
to `claude-fable-5` (`defaults.yaml:1470-1471`) — all long tool-use loops, exactly the workload
where thinking would be worth turning on.

A secondary consequence: `max_tokens` is hardcoded to 8192 on both paths, and a thinking budget must
fit inside it.

### F2 — OpenAI reasoning propagation is correct, but scoped by a hardcoded model prefix

The Responses path is only selected by `_uses_responses_api()` (`openai_client.py:187-189`):
direct-OpenAI **and** `model.startswith("gpt-5.6-sol")`. Everything else — including `gpt-5.5`, used
as a fallback in two profiles (`defaults.yaml:405`, `:1428`) — goes through Chat Completions, which
carries no reasoning state by design.

The mechanism itself checks out end to end: `include: ["reasoning.encrypted_content"]` on the
request (`:208`), the whole `response.output` list (reasoning items included) stored as
`provider_metadata["openai_response_output"]` (`:472-476` non-streaming, `:1141-1145` streaming),
persisted through the same `provider_metadata` column as Google's, and replayed verbatim minus
`status` (`:263-277`). `tests/integration/llm/test_streaming.py:343` covers the round trip.

The prefix check is the fragile part — it is model-name sniffing of the kind this codebase has
deliberately avoided elsewhere (the computer-use flag is opt-in config, not a name match). A new
reasoning model silently drops to Chat Completions with no signal.

### F3 — Pre-`include` history can 400 on continuation

`include: ["reasoning.encrypted_content"]` was added in `277af7d9b`, after the Responses path
already shipped. Assistant rows written before that commit hold reasoning items with
`encrypted_content: null`. `_messages_to_responses_input` strips only `status`, so those items are
replayed as-is; with `store=false` the API has no server-side copy to resolve the `rs_…` id against
and rejects the request.

Bounded — it only affects conversations that straddle that commit and are still being continued —
but the fix is one line: drop `reasoning` items that have no `encrypted_content` when `store` is
false.

### F4 — Reasoning state is dropped when a turn produces neither text nor tool calls

`llm_loop.py:546-552` returns early without building an `AssistantMessage` when the model returned
no content and no tool calls, discarding `done_provider_metadata` with it. For the Responses API a
response whose output is reasoning-only (incomplete, or truncated by `max_output_tokens`) loses its
encrypted reasoning, so the retry starts cold. Rare, and the behaviour is otherwise sane; worth a
comment at minimum.

### F5 — Thought summaries are stored but unreachable in the UI

`MessageDisplay.jsx:308-311` renders a "Thinking Summary" block from
`message.reasoning_info.thinking`. No backend writer ever sets `thinking`; Google writes
`thought_summaries` (a list of `{summary, …}` dicts), which the history endpoint passes through
verbatim (`chat_api.py:2382`). The panel is dead code and the summaries are invisible.

Neither OpenAI nor Anthropic contributes summaries at all — OpenAI would need
`reasoning: {summary: "auto"}` plus `response.reasoning_summary_text.delta` handling, Anthropic
would need thinking-block capture from F1.

### F6 — No `thinking` stream event type

`LLMStreamEvent.type` (`llm/__init__.py:810`) has no thinking variant, so even a provider that
produced summaries could not stream them to the web or iOS clients — they can only ride along in the
terminal `done` event's `reasoning_info`. Any future "show me your thinking" UI needs this event
first.

### Non-issues checked

- **Cross-provider metadata contamination is safe.** OpenAI's replay guards on the
  `openai_response_output` key (`:265-267`) and falls through for Gemini metadata; Anthropic ignores
  `provider_metadata` entirely. A conversation that switches provider mid-thread degrades to plain
  content/tool-call replay rather than erroring.
- **Context pruning does not orphan reasoning items.** `prune_messages_for_context`
  (`processing/utils.py:152-183`) drops whole turns at `UserMessage` boundaries and only rewrites
  tool-result *content*, so `function_call` / `function_call_output` pairs and their preceding
  reasoning items stay together.
- **The retry wrapper is transparent.** `retrying_client.py` forwards stream events verbatim, so
  `provider_metadata` survives the retry/fallback layer.

## Recommended work

Ordered by value, each independently shippable.

1. **Anthropic thinking support (F1).** Capture `thinking` / `redacted_thinking` blocks (including
   `signature`) into `provider_metadata`, replay them ahead of `tool_use` blocks in the assistant
   turn, and handle `thinking_delta` / `signature_delta` in the stream loop. Add an explicit
   `thinking` config knob rather than leaving it to raw `llm_parameters` passthrough, and make
   `max_tokens` account for the budget. Until this lands, `llm_parameters` thinking on a Claude
   model should be rejected at startup instead of failing on the first tool call.
2. **Prefix-drop `encrypted_content: null` reasoning items (F3).** One-line guard plus a unit test.
3. **Replace the `gpt-5.6-sol` prefix check with opt-in config (F2).**
4. **Fix the summary key mismatch and add a `thinking` stream event (F5, F6)** if surfacing
   reasoning to users is wanted; otherwise delete the dead frontend branch.
5. **Preserve reasoning metadata on empty turns (F4)**, or document why it is deliberately dropped.

# Thinking / Reasoning Propagation: OpenAI and Anthropic

Investigation of how reasoning state (extended thinking, encrypted reasoning items, thought
summaries) is captured, persisted and replayed for the `openai` and `anthropic` providers, using the
Google provider's thought-signature handling as the reference implementation.

**Status: implemented.** F1–F5 are fixed; the findings below are kept as the record of what was
wrong and why each fix is shaped the way it is. See [What was built](#what-was-built) for the
resulting behaviour.

## Summary

State after the fixes:

| Provider                               | Reasoning captured             | Persisted | Replayed to model             | Surfaced to user |
| -------------------------------------- | ------------------------------ | --------- | ----------------------------- | ---------------- |
| Google (Gemini)                        | thought signatures + summaries | yes       | yes                           | history UI       |
| OpenAI — Responses API (opt-in)        | encrypted reasoning items      | yes       | yes                           | no               |
| OpenAI — Chat Completions              | none                           | n/a       | n/a (API has no such concept) | n/a              |
| Anthropic (thinking enabled)           | thinking blocks + signatures   | yes       | yes                           | no               |
| Anthropic (thinking off — the default) | none                           | n/a       | n/a                           | n/a              |

Originally: OpenAI's Responses path was complete and correct but reachable only through a hardcoded
model prefix; Anthropic had no reasoning support of any kind — not a regression, it was never built.
Switching thinking on would not have errored, but would have silently lost interleaved thinking
across the tool loop, which is verified against the live API in
[Empirical verification](#empirical-verification) below. Thought summaries that *were* captured
(Google) were stored but never displayed, because the frontend read a key the backend does not
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

### F1 — Anthropic captures, stores and replays no reasoning state at all

`anthropic_client.py:502-531` builds assistant turns from `content` and `tool_calls` only. It emits
`text` and `tool_use` blocks; `thinking` and `redacted_thinking` blocks are neither produced on
output nor accepted on input. The response parsers only recognise two block types (`:768-781`
non-streaming, `:1041-1077` streaming — no `thinking_delta` / `signature_delta` branch), and
`LLMOutput` is built without `provider_metadata` (`:791-795`).

Nothing sets a `thinking` parameter today, so this costs nothing right now. It is reachable by
config, though: `llm_parameters` entries are spread straight into `client.messages.create(**params)`
(`anthropic_client.py:743-749`, `:1009-1015`, wired via `factory.py:159-176`), so an
`llm_parameters` entry is enough to switch extended thinking on for the profiles that run Claude —
`automation_creation` and `engineer` (`defaults.yaml:894`, `:1633`, both `claude-sonnet-4-6`) and
`complex_tasks`' fallback to `claude-fable-5` (`defaults.yaml:1470-1471`), all long tool-use loops.

The consequence is degradation, not breakage — see the probe results below. Dropping thinking blocks
from a tool-use continuation is *accepted* by the API. What is lost is interleaved thinking inside
the tool loop, which is most of the reason to enable thinking on these profiles in the first place.

Two hard constraints any implementation must respect:

- **Replay verbatim or not at all.** A thinking block whose `signature` has been altered is rejected
  with `400 messages.N.content.M: Invalid 'signature' in 'thinking' block`. Signatures are verified
  when present, so round-trip fidelity through the `provider_metadata` JSON column is a correctness
  requirement, exactly as it is for Gemini thought signatures.
- **The config shape differs by model generation.** `claude-sonnet-4-6` takes
  `thinking: {type: enabled, budget_tokens: N}`. `claude-fable-5` rejects that outright —
  `400 "thinking.type.enabled" is not supported for this model` — and wants
  `thinking: {type: adaptive}` with `output_config: {effort: ...}`. A single `"claude-"` prefix
  entry in `llm_parameters` therefore cannot serve both, which is an argument for a typed config
  knob that resolves per model rather than raw passthrough.

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
`encrypted_content: null`. The server can resolve a reasoning item by ID only when its originating
response was stored; changing `store` on a later request cannot retroactively store the historical
item. Replaying an unstored item with no encrypted content therefore rejects the request.

Bounded — it only affects conversations that straddle that commit and are still being continued —
but the fix is small: persist whether each response was stored, and drop a `reasoning` item with no
`encrypted_content` unless its originating response is known to have been stored. Older rows have no
storage marker and are conservatively treated as unstored.

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

## Empirical verification

F1 originally claimed that continuing a tool-use conversation without replaying thinking blocks
fails with a 400. That is **wrong**, and the correction came from probing the live API
(`/v1/messages`, 2026-08-01) rather than from the docs. Each probe elicited a `thinking` +
`tool_use` turn, then replayed the assistant turn twice — once verbatim, once with `thinking` and
`redacted_thinking` blocks stripped — and compared.

| Model / mode                            | Thinking replayed     | Thinking stripped            |
| --------------------------------------- | --------------------- | ---------------------------- |
| `claude-haiku-4-5`, `thinking.enabled`  | 200                   | **200** (accepted)           |
| `claude-sonnet-4-6`, `thinking.enabled` | 200                   | **200** (accepted)           |
| `claude-sonnet-4-6`, + interleaved beta | 200, emits `thinking` | 200, **no `thinking` block** |
| `claude-fable-5`, `thinking.adaptive`   | 200, emits `thinking` | 200, emits `thinking`        |

Conclusions drawn from this:

1. **Stripping thinking blocks is accepted everywhere tested.** There is no hard failure to fix, so
   F1 is a quality issue, not an outage waiting to happen.
2. **The cost is interleaved thinking.** Under `interleaved-thinking-2025-05-14` on
   `claude-sonnet-4-6`, replaying thinking produced a `thinking` block on the continuation in 3/3
   trials; stripping produced none in 3/3. Consistent across trials, but it is a behavioural
   observation at n=3, not a guarantee.
3. **Signatures are verified when present.** Truncating and re-padding a `signature` returns
   `400 Invalid 'signature' in 'thinking' block`. Replay must be byte-exact. *Position*, by
   contrast, is not checked: a follow-up probe accepted thinking placed after `text`, after
   `tool_use`, and split across a turn by an intervening `text` block. Only fidelity matters.
4. **`claude-fable-5` uses a different thinking API.** `thinking.type.enabled` is rejected for that
   model in favour of `thinking.type.adaptive` + `output_config.effort`. Its adaptive mode also
   emitted no thinking on the first turn and thinking on the continuation regardless of replay, so
   the interleaving effect in (2) is specific to the `enabled` + interleaved-beta configuration.

Probe scripts are throwaway and live in the session scratchpad, not the repo; the table above is the
durable record.

## What was built

### Anthropic thinking (F1)

`thinking` and `redacted_thinking` blocks are captured into `provider_metadata` as
`{"provider": "anthropic", "thinking_blocks": [...]}` and replayed at the head of the assistant
turn, mirroring the order the API itself emits. Position turns out *not* to be enforced — probing
showed the API accepts thinking after `text` or `tool_use`, and accepts the interleaved shape
`_merge_consecutive_roles` produces when two assistant turns merge — so no ordering machinery is
warranted. Content fidelity is the real constraint. Three details are load-bearing:

- **Blocks are opaque.** They are stored and replayed as whole dicts, never re-derived from their
  parts, because the `signature` is verified and any reconstruction risks changing a byte.
- **Streaming captures from the assembled final message**, not from the deltas. `thinking_delta`
  carries text but no signature; only `get_final_message()` has the complete block.
- **The under-pytest shim had to learn thinking too.** Every Anthropic streaming test goes through
  `_maybe_parse_vcr_stream`, which substitutes a non-streaming call because VCR cannot replay SSE.
  Left alone it would have dropped thinking, so the tests would have run without reasoning state and
  proved nothing about the round trip. This was caught only because the first recorded cassette came
  back with no thinking in it.

Thinking deltas now surface as a `thinking` stream event (F6) so reasoning can never be concatenated
into the assistant's reply. Plumbing that event to the web and iOS transports is deliberately *not*
done — the processing loop ignores unrecognised event types, so nothing changes for clients until
someone builds the UI for it.

Configuration stayed as `llm_parameters` passthrough rather than becoming a bespoke typed knob. The
two shapes are Anthropic's own, they are already validated by the API with a message that names the
alternative, and a local abstraction over them would have to be revised every time a model
generation changes. What *is* validated locally is `budget_tokens` against `max_tokens`, because
that one fails deep in a conversation rather than at the first request.

### OpenAI (F2, F3)

`use_responses_api` in `llm_parameters` selects the Responses API; the `gpt-5.6-sol` name prefix is
gone, and `defaults.yaml` carries the opt-in for that model. Control keys are filtered inside
`_get_model_specific_params`, the single accessor all five request-building paths share, so they
cannot leak into a request from a call site someone forgot to update.

Each response records whether it was stored. Reasoning items with no `encrypted_content` are kept
only when their originating response is known to have been stored, which unblocks conversations
whose history predates the `include: ["reasoning.encrypted_content"]` request even if the current
request changes `store`.

### F4, F5

The empty-turn metadata drop is deliberate and now says so in the code: there is no
`AssistantMessage` to hang metadata on, and replaying an unfinished turn's reasoning would carry a
dead end forward. The history UI renders `thought_summaries` instead of the `thinking` key nothing
ever wrote.

### Coverage

- `tests/integration/llm/test_streaming.py::test_anthropic_streaming_thinking_round_trip` — recorded
  against the live API. The cassette's second request shows the thinking block and its 380-character
  signature replayed ahead of `tool_use`, answered 200. That 200 *is* the assertion that the round
  trip preserved the signature byte-for-byte, since a mangled one is rejected.
- `tests/unit/llm/providers/test_anthropic_thinking.py` — block extraction, replay ordering,
  cross-provider metadata isolation, budget validation.
- `tests/unit/storage/test_thinking_block_persistence.py` — signature survives the database round
  trip, and a message read back from storage still converts into a valid turn.
- `tests/unit/llm/providers/test_openai_responses_input.py` — opt-in gating, control-key leakage,
  and the three `encrypted_content` cases.
- `frontend/src/pages/History/components/__tests__/MessageDisplay.test.jsx` — summaries render, and
  the panel stays hidden when there are none.

## Not done

- **Streaming reasoning to the UI.** The `thinking` event exists but stops at the processing loop.
  Surfacing it needs SSE plumbing plus web and iOS client work.
- **OpenAI reasoning summaries.** Would need `reasoning: {summary: "auto"}` and
  `response.reasoning_summary_text.delta` handling. Only Google populates `thought_summaries` today,
  so only Google's show in the history UI.
- **Anthropic thinking is still off by default.** Everything works when it is switched on; whether
  to switch it on for `automation_creation` and `engineer` is an operator call with a real token
  cost, not something to default silently.

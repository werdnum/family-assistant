# Prompt Caching

> **Partly superseded by [prompt-cache-turn-context.md](prompt-cache-turn-context.md)**, which
> carried out the first "Deferred" item below. `current_time` and `aggregated_other_context` are no
> longer system-prompt placeholders, so the sentinel-probe derivation described under
> [Stable-prefix boundary](#stable-prefix-boundary) is gone: the rendered prompt is wholly static
> and `stable_prefix_len` is simply its length. Everything else here still holds.

## Problem

Every provider we use caches prompts as a **prefix match**: the request is hashed from the start
(`tools` → `system` → `messages`) and a single differing byte at position N invalidates the cache
for everything at or after N. Anthropic requires an explicit `cache_control` breakpoint; OpenAI and
Gemini cache automatically. All three need a byte-stable prefix to hit.

An audit of the request-building path found the prefix changing on essentially every request, so the
effective hit rate was ~0 across all providers:

1. **The tool loop rewrote the system prompt on every iteration.** `llm_loop.py` appended
   `[Processing iteration N/M]` to `messages[0]` each time round. Because `system` renders at the
   front of the prefix, a ten-iteration agentic turn paid ten full-price prefills of the prompt, the
   tool definitions, and every tool result accumulated so far. This was the single most expensive
   invalidator, since agentic turns are where the context is largest.
2. **`current_time` rendered at second resolution inside the system prompt**, so no two turns ever
   shared a prefix — even back-to-back messages in one conversation.
3. **Anthropic sent `system` as one monolithic string**, leaving nowhere to place a breakpoint even
   if the content had been stable.
4. **No cache accounting.** `cache_read_input_tokens` / `cache_creation_input_tokens` were parsed
   and discarded, so none of the above was measurable.

## Approach

The guiding constraint is that **the text the model sees must not change**. Everything here is a
change to prompt *structure* and *request shape*, not content, so there is no behavioral risk to
re-validate beyond the loop's final-iteration instruction.

### Stable-prefix boundary

`ProcessingService._render_system_prompt` now returns the rendered prompt plus a
`stable_prefix_len`: the length of the leading run that does not depend on per-turn inputs. It is
derived by rendering the template a second time with sentinel values for `current_time` and
`aggregated_other_context` and taking the common prefix — whatever two renderings that differ only
in those inputs still share is, by construction, independent of them.

Deriving the boundary rather than hardcoding it matters because the template is operator-editable
per profile: placeholders can appear anywhere, more than once, or not at all. A profile whose
template omits the volatile placeholders is fully stable and gets a boundary at the end; a profile
that leads with `{current_time}` gets a boundary right after the (stable) profile preamble.

The boundary rides on `SystemMessage.stable_prefix_len`, which is advisory metadata: providers that
cannot use it ignore it and send the prompt exactly as before.

### Anthropic breakpoint placement

`AnthropicClient` splits the top-level `system` at that offset into two text blocks and marks only
the leading one `cache_control: {"type": "ephemeral"}`. Concatenating the blocks reproduces the
string previously sent. Because a breakpoint on the last system block also covers everything
rendered before it, this caches the tool definitions along with the static instructions.

This breakpoint uses the one-hour TTL (`"ttl": "1h"`) rather than the default five minutes. The
tools and static instructions (about 30k tokens on the main profiles) are shared by every turn of
every conversation, and people routinely reply more than five minutes apart, so with the default TTL
a large share of turns started cold. A one-hour write costs 2× the input price instead of 1.25×,
which a prefix this widely shared repays within a few reads. Anthropic requires longer-lived
breakpoints to precede shorter ones; this one comes before every conversation breakpoint, which keep
the default TTL.

The conversation breakpoint is placed only on streamed requests, which are the agent loop's. Every
other request — a tool-call review, a structured or JSON output, a summary, an attachment pick — is
one-shot: its tail is unique and no later request extends it, so a breakpoint there writes a cache
entry (at 1.25× the input price) that is never read, and skews the account's cache-hit metric.

A streamed request carries two conversation breakpoints: one at the end, and one where the previous
request's end was (the newest conversation message ahead of the newest assistant turn). A breakpoint
finds an earlier entry only at its own position or within 20 content blocks behind it, and one step
can append more than that — a `tool_addition` is a block per activated tool, so activating one MCP
server's tools is enough. Past the window the read falls back to the system prompt and the whole
history is written again, with no divergence for the cache diagnostics to report. Production calls
with 25 or more blocks between consecutive breakpoints rewrote the history 83% of the time, against
1% at 12 or fewer. The second marker costs nothing: it names a position already written.

A prompt that is stable all the way to the end — what a template with no volatile placeholders
produces — needs no split and is emitted as a single cacheable block. Only when there is no boundary
at all does the client send the plain string as before, so requests that cannot benefit keep their
existing wire format (and existing VCR cassettes keep matching).

### System messages after the first

Only the system messages that open the conversation go into the top-level `system`. One that arrives
later — a delegation completion, a callback trigger, a system row a compacted turn kept — is
rendered where it occurred, as a user text block labelled `[System notice]`. `system` precedes the
whole message history in the cache key, so hoisting a later notice there kept the tools and stable
instructions cached (they sit before the one-hour breakpoint) but rewrote the history's cache entry
every time a notice appeared or dropped out.

The native mid-conversation `system` role is not used because its placement rules depend on what
follows the message (it must follow a user turn and be last or precede an assistant turn), so the
same message would change shape between one request and the next — itself a prefix change, and one
that invalidates later thinking blocks. A labelled user block is valid in every position on every
model.

### Detecting a broken prefix

Two mechanisms, one before deploy and one after:

- **A property test per adapter** (`tests/unit/llm/providers/test_*_prefix_stability.py`) generates
  conversations in the shapes the agent loop produces — tool rounds, activations, new user turns,
  system notices, delegation wakes — from a shared generator
  (`tests/unit/llm/agent_loop_conversations.py`), and asserts each request the loop would send
  extends the one before it. A conversion that rewrites earlier output fails it without anyone
  having to think of the case. For Gemini, which reports nothing when a prefix breaks, this test is
  the only guard.
- **Anthropic cache diagnostics** on every streamed request. The client sends
  `diagnostics.previous_message_id` naming the response that produced the conversation's newest
  assistant turn (kept as `response_id` in that turn's provider metadata), and the API reports where
  the two prompts diverged: `system_changed`, `tools_changed`, `messages_changed`, `model_changed`,
  or why it could not compare. The reason lands in `reasoning_info.cache_miss_reason`, in the
  `family_assistant_llm_cache_misses_total{reason}` counter, and — for an actual divergence — in a
  warning log. Some divergences are expected (the final iteration drops the tool list); what the
  counter is for is noticing a new one, or a familiar one becoming common.

### Loop stability

The iteration counter is gone — it was informational and cost a full prefix invalidation per tool
call. The load-bearing part, the final-iteration "you must answer now" instruction, moved to a
trailing user message, which appends at the end of the prefix and invalidates nothing. That path
already existed for conversations carrying Gemini thought signatures; it is now the only path.

On-demand tool activation still rebuilds the system prompt, but only when the addition actually
changes. Activation also changes the tool list, which invalidates the prefix regardless, so this
costs nothing extra.

An instruction meant for one turn only is sent as **trailing scaffolding**: a user message flagged
`is_turn_scaffolding` that follows the conversation on that turn's requests and is never saved. The
Anthropic client puts the conversation breakpoint on the last message that is *not* scaffolding, so
the cache entry each request writes ends before the instruction, and the next request — which has
moved the instruction or dropped it — reads that entry back whole.

A delegation wake is the case this exists for. Its row is replayed on every later turn with the
lines telling the model to act left out, so an old wake reads as a record rather than a request.
Were the wake's own turn to see the row any other way, the turn after it would diverge at the wake
and rewrite everything from there — the wake's handling of the result, often tens of thousands of
tokens of tool rounds. So the wake's turn sees the row exactly as later turns replay it, and gets
the act-now lines, with the attachment ids it may need, as the loop's `trailing_instruction`.

### Measurement

`MessageReasoningInfo` gained `cached_prompt_tokens` and `cache_write_tokens`, populated by all
three providers and surfaced as `gen_ai.usage.cached_input_tokens` /
`gen_ai.usage.cache_write_input_tokens` span attributes. Anthropic also reports how much of the
write was at the one-hour lifetime (`usage.cache_creation.ephemeral_1h_input_tokens`), recorded as
`cache_write_1h_tokens` — a subset of `cache_write_tokens` — and exported as its own
`cache_write_1h` token bucket, because it is billed at 2× the input price rather than 1.25×.

Two providers reported nothing at all on the streaming path — which is the path the processing loop
uses, so the profiles that matter most were unmeasurable:

- **Gemini** extracted usage only for non-streaming calls, on the stale assumption that streaming
  does not carry it. Streaming chunks are `GenerateContentResponse` objects and do carry
  `usage_metadata`; the newest one seen is now kept, since not every chunk includes it. Gemini's
  `thoughts_token_count` also feeds the existing `reasoning_tokens` field.
- **OpenAI** never asked for streaming usage. Chat Completions omits it unless
  `stream_options: {"include_usage": true}` is set, so the existing extraction was dead code. Two
  bugs were stacked here: the usage chunk arrives last with an **empty `choices` list**, so the
  content guards `continue`d past it and it would have been dropped even once requested.

`stream_options` is sent only to the official API, because an OpenAI-compatible endpoint that
rejects unknown parameters would fail the request outright. Operators whose endpoint does support it
can opt in via `model_parameters`, which is merged after the default and wins.

The Deep Research (`interactions`) path is still uninstrumented: the SDK version in use does not
expose a usage field on those events.

Note the providers do not agree on what `prompt_tokens` means, so a hit rate computed against it
directly is wrong for at least one of them:

| Provider  | `prompt_tokens` covers      | Cache buckets                                                                                                                                              |
| --------- | --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Anthropic | the uncached remainder only | `cached_prompt_tokens` and `cache_write_tokens` are **disjoint**; the real prompt total is the sum of all three                                            |
| OpenAI    | the whole prompt            | `cached_prompt_tokens` is a **subset**, never added on top. The Responses API also reports `cache_write_tokens` (also a subset); Chat Completions does not |
| Google    | the whole prompt            | `cached_prompt_tokens` is a **subset**; no cache-write count                                                                                               |

Anthropic's `total_tokens` is now computed by adding the cache buckets back, so a cached turn no
longer reports a prompt many times smaller than the one actually sent.

Cache fields are omitted only when a provider does not report them. A **reported zero is recorded as
zero**, so a known cache miss stays distinguishable from a provider that says nothing about caching
— otherwise a dashboard that skips unreported values would drop every miss and overstate the hit
rate.

Streaming usage is exported to the span in `RetryingLLMClient.generate_response_stream` rather than
per provider, because providers report it in the `done` event's metadata and the OpenAI client has
no span of its own. Doing it at the retry layer is what makes `gen_ai.usage.*` present for every
provider instead of only the ones that instrument themselves.

## Other providers

Caching on OpenAI and Gemini is driven by the same rule — send each request as an extension of the
last — but the controls differ, so each adapter applies what its API offers. Everything above the
adapters (the stable system prompt, the time stamped on user messages, history-level fixes) applies
to all of them unchanged.

### OpenAI

GPT-5.6 and later accept explicit breakpoints, cache lifetimes and `additional_tools` input items on
the Responses API, with writes billed at 1.25× (30 minutes) or 2.5× (12 hours) and reads at 0.1×. On
those models:

- **The tools and stable system prompt carry a 12-hour breakpoint.** Shared by every conversation on
  a profile and changed only by a note, skill or config edit, they are read back by a turn that
  arrives after the 30-minute default expired. The position never moves, so nothing is re-written at
  the longer lifetime.
- **The agent loop keeps OpenAI's implicit breakpoint**, which caches each tool round at the end of
  the latest message for the next. A non-streamed request is one-shot and runs in explicit-only
  mode, writing nothing past the shared prefix.
- **The `tools` array is fixed.** An activation appends an `additional_tools` item after the outputs
  that activated it, the counterpart of Anthropic's `tool_addition` blocks.

Earlier models, Chat Completions and OpenAI-compatible backends get none of this; they reject the
fields.

### Gemini

Gemini caches implicitly, with no breakpoints, no write charge and no lifetime control, so prefix
stability is the only lever, and the adapter already holds it: every system message is sent where it
occurred, and thought signatures are replayed exactly as received. Explicit caching
(`cachedContents`) exists on Flash but not on Gemini 4 Argon, and was measured at roughly $8–17 a
month net of storage — not yet worth the lifecycle of recreating a cache whenever the prompt
changes.

## Deliberate simplifications

- **An interrupted turn that activated a deferred tool rewrites the cache once.** The
  `tool_addition` system message that surfaces the tool may only precede an assistant turn or end
  the request. When a user message lands directly on the tool results instead of the model's reply,
  the addition moves past it, so the request diverges from the one before at that point. Only an
  interrupted turn produces this shape, and the cost is one rewrite of the conversation's cache
  entry; the property test models the loop's ordinary shape and leaves this one out.
- **System notices lose the operator role.** Rendered as a labelled user block rather than a native
  mid-conversation `system` message, a notice reads to the model as context from the harness rather
  than as an operator instruction. Before this change it was in `system`; nothing in the notices
  requires operator authority, and the placement rules of the native role would make it change shape
  between requests.
- **The conversation history keeps the five-minute lifetime.** A turn that arrives more than five
  minutes after the last one rewrites the history at 1.25×. A one-hour breakpoint at the end of the
  settled history was shipped and taken out again, because it costs more than it saves. Moving the
  breakpoint each turn writes every turn twice: at five minutes during the turn, then again at one
  hour on the next turn, since a one-hour write is charged from the last one-hour entry even when a
  warm five-minute entry covers more. No placement escapes the underlying arithmetic. Writing a span
  at one hour costs an extra 0.75× input, and pays back 1.15× only if the next turn arrives five to
  sixty minutes later, so it breaks even when that happens 65% of the time. Measured on production
  Anthropic turns (September–October 2026), it happens 36% of the time. Even conditioned on the
  previous gap, the most predictable case reaches only 47%. Replaying those turns against simulated
  cache state put every variant tried above the five-minute baseline: the moving breakpoint, one
  moved only when the tail had expired or only after enough new history, one anchored at compaction
  boundaries, and a one-hour tail. Revisit only if the gap distribution changes enough to clear that
  bar.
- **On Gemini, activating a deferred tool costs that request its cache.** Gemini cannot add a tool
  mid-conversation, so the tool list grows on activation and everything after it misses. In
  production every such request missed (160 of 160), costing about $5 a month; sending every
  on-demand tool on every request instead was measured at 3–60× that, per tool server.
- **No long-lived breakpoint at the end of the settled history on OpenAI.** It would move every
  turn, re-writing the turn just finished at 2.5× on the next request whether or not the cache was
  still warm. On deep-tier traffic that costs about four times what it saves, because most
  follow-ups arrive within the 30 minutes the default already covers.
- **OpenAI structured and JSON calls get no cache controls.** They go through Chat Completions,
  where the controls are not offered; they are few, and run on the default implicit caching.
- **Diagnostics compare against the newest assistant turn only.** If that turn was served by another
  provider, or predates response ids being stored, the request opts in without a predecessor and the
  first divergence after it goes unreported.

## Deferred

**Move `current_time` and `aggregated_other_context` out of the system prompt entirely**, to the end
of the message list. *Since done — see
[prompt-cache-turn-context.md](prompt-cache-turn-context.md).* The breakpoint split already lets the
static instructions cache across turns, so this is now an incremental gain: it would additionally
let `system_prompt_docs` and the delegation catalogue — currently rendered *after* the volatile
context, and therefore stranded on the uncached side — join the cached prefix. Reordering changes
where the model sees this content, so unlike everything above it needs validation against the eval
suite. Worth doing only if the telemetry shows the uncached tail is material.

**A breakpoint on the trailing conversation content.** *Since done — see
[prompt-cache-turn-context.md](prompt-cache-turn-context.md).* The system-block breakpoint caches
`tools` + the static system prompt, but nothing in `messages`. During a tool loop the accumulated
assistant turns and tool results are byte-stable (that is what the loop fix bought) yet still
re-read at full price on every iteration, because Anthropic caches only up to the last breakpoint.
Marking the last content block of the most recently appended turn — the documented multi-turn
pattern — lets iteration N reuse iterations 1..N−1, which on a tool-heavy turn is plausibly a larger
saving than the system prompt itself. A second breakpoint ends the replayable history just ahead of
the turn-context block, so the saving carries across turns and not only within one.

The reason it is not in the first change: it shifts the cost profile rather than purely removing
waste. Every request would write an incremental cache entry (1.25× on the delta) to buy 0.1× reads
on the rest — strongly net-positive when the prefix is re-read each iteration, but it wants the
telemetry above to confirm rather than assume. It also interacts with the 20-block lookback below.

**Tool-list churn from on-demand activation** is a hard cache reset (tools render at position 0). If
telemetry shows activation is frequent, Anthropic's `defer_loading` plus tool search would let tool
schemas be appended rather than swapped, preserving the prefix.

**The 20-block lookback window.** *Since done — see "Anthropic breakpoint placement" above.* A
breakpoint searches back at most 20 content blocks for a prior cache entry, so a single iteration
emitting more than 20 blocks silently missed. The second conversation breakpoint removes the
dependence on the window.

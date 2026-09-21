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

A prompt that is stable all the way to the end — what a template with no volatile placeholders
produces — needs no split and is emitted as a single cacheable block. Only when there is no boundary
at all does the client send the plain string as before, so requests that cannot benefit keep their
existing wire format (and existing VCR cassettes keep matching).

### Loop stability

The iteration counter is gone — it was informational and cost a full prefix invalidation per tool
call. The load-bearing part, the final-iteration "you must answer now" instruction, moved to a
trailing user message, which appends at the end of the prefix and invalidates nothing. That path
already existed for conversations carrying Gemini thought signatures; it is now the only path.

On-demand tool activation still rebuilds the system prompt, but only when the addition actually
changes. Activation also changes the tool list, which invalidates the prefix regardless, so this
costs nothing extra.

### Measurement

`MessageReasoningInfo` gained `cached_prompt_tokens` and `cache_write_tokens`, populated by all
three providers and surfaced as `gen_ai.usage.cached_input_tokens` /
`gen_ai.usage.cache_write_input_tokens` span attributes.

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

**The 20-block lookback window.** A breakpoint searches back at most 20 content blocks for a prior
cache entry. A single iteration emitting more than 20 blocks (many parallel tool calls) would
silently miss. Not currently instrumented; the cache-read telemetry would show it as an unexplained
miss.

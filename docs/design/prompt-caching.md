# Prompt Caching

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

When there is no usable boundary the client sends the plain string as before, so requests that
cannot benefit keep their existing wire format (and existing VCR cassettes keep matching).

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

Note the providers do not agree on what `prompt_tokens` means, so a hit rate computed against it
directly is wrong for at least one of them:

| Provider  | `prompt_tokens` covers      | Cache buckets                                                                                                   |
| --------- | --------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Anthropic | the uncached remainder only | `cached_prompt_tokens` and `cache_write_tokens` are **disjoint**; the real prompt total is the sum of all three |
| OpenAI    | the whole prompt            | `cached_prompt_tokens` is a **subset**; no cache-write concept                                                  |
| Google    | the whole prompt            | `cached_prompt_tokens` is a **subset**; no cache-write concept                                                  |

Anthropic's `total_tokens` is now computed by adding the cache buckets back, so a cached turn no
longer reports a prompt many times smaller than the one actually sent.

Cache fields are omitted rather than zeroed when a provider does not report them, so "not reported"
stays distinguishable from a genuine zero-hit turn.

## Deferred

**Move `current_time` and `aggregated_other_context` out of the system prompt entirely**, to the end
of the message list. The breakpoint split already lets the static instructions cache across turns,
so this is now an incremental gain: it would additionally let `system_prompt_docs` and the
delegation catalogue — currently rendered *after* the volatile context, and therefore stranded on
the uncached side — join the cached prefix. Reordering changes where the model sees this content, so
unlike everything above it needs validation against the eval suite. Worth doing only if the
telemetry shows the uncached tail is material.

**Tool-list churn from on-demand activation** is a hard cache reset (tools render at position 0). If
telemetry shows activation is frequent, Anthropic's `defer_loading` plus tool search would let tool
schemas be appended rather than swapped, preserving the prefix.

**The 20-block lookback window.** A breakpoint searches back at most 20 content blocks for a prior
cache entry. A single iteration emitting more than 20 blocks (many parallel tool calls) would
silently miss. Not currently instrumented; the cache-read telemetry would show it as an unexplained
miss.

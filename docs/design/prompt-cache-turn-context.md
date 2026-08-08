# Prompt caching: move per-turn context out of the system prompt

## Problem

Every provider we use caches prompts by **longest common prefix**. Whatever byte first differs
between two requests ends the cache hit, and everything after it is re-read at full price.

Today the system prompt is rendered from a template that interpolates two per-turn values in the
middle of it:

```
# Context
Current time: {current_time}

{aggregated_other_context}
```

So the assembled request looks like this:

| Region                                                                     | Size (production, `default_assistant`) | Stable across turns?                        |
| -------------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------- |
| System prompt, static instructions                                         | ~16,000 chars                          | yes                                         |
| `Current time: …` (second precision)                                       | ~30 chars                              | **no**                                      |
| Aggregated context (notes, calendar, Home Assistant, weather, known users) | ~25,000 chars                          | **no**                                      |
| `system_prompt_docs`, server URL, delegation catalog                       | ~2,500 chars                           | yes, but stranded after the volatile region |
| Conversation history                                                       | ~140,000 chars                         | yes (append-only)                           |

Measured against the live production request buffer (20 requests, `/api/diagnostics/export`), the
common prefix between two consecutive turns of the same conversation is **16,009 characters out of a
46,000-token request**. The timestamp at that offset caps the cacheable prefix at roughly 4k tokens;
the remaining ~90% of every turn-boundary request is reprocessed uncached, including the ~35k tokens
of history that never changed.

Within a single turn's tool loop the prompt is already byte-stable — `llm_loop.py` was deliberately
fixed to stop rewriting the system prompt per iteration — so those requests cache correctly. It is
each **new turn** that pays full freight.

The `stable_prefix_len` breakpoint machinery does not rescue this. It only feeds Anthropic's
explicit `cache_control` breakpoint; the profiles that carry the large prompts run on OpenAI and
Gemini, which cache implicitly by prefix and have no breakpoint to place.

## Approach

Order content by stability: static system prompt first, append-only history next, volatile content
last. Concretely, the volatile material moves out of the system prompt and is delivered as a
**trailing user message** at the end of the turn's message list.

This is not a new pattern here. `llm_loop.py:393-405` already delivers the final-iteration
instruction this way, with the comment *"Delivered as a trailing user message rather than a
system-prompt edit so the cached prefix survives the final iteration too."* This change applies the
same reasoning to the much larger per-turn context block.

After the change:

```
[ system prompt — static per (profile, user) ]
[ history — append-only, persisted ]
[ <turn_context> — volatile, ~7k tokens, never persisted ]
```

The cacheable prefix becomes the entire system prompt plus the entire history. Only the
`<turn_context>` block itself, plus genuinely new messages, are uncached.

On OpenAI and Gemini that is the whole story: they cache implicitly by prefix, so a stable prefix is
a cached prefix. Anthropic caches only up to an explicit `cache_control` breakpoint, and the only
one we set was on the system block — so a stable history bought it nothing on its own. Anthropic
therefore gets two more breakpoints (see below), which is what turns the same prefix stability into
an actual cache hit there.

### Why a user message and not a system message

The block must be a `UserMessage`:

- **Anthropic** (`anthropic_client.py:576`) hoists *every* `SystemMessage` into the top-level
  `system` parameter, which sits before all history. A trailing `SystemMessage` would be teleported
  back to exactly the position we are moving it out of.
- **Gemini** (`google_genai_client.py:665`) converts a `SystemMessage` into a `role="user"` turn
  with a `System: ` prefix, in place — so it would work, but only by accident of that mapping.
- **`prune_messages_for_context`** (`processing/utils.py:160`) hoists all `SystemMessage`s to the
  front of the pruned result, which would move it again.

A `UserMessage` behaves identically across all three providers with no provider changes at all.
Trust framing is handled in the text: the block is wrapped in `<turn_context>` tags and the system
prompt states that it is system-generated and not user-authored, the same treatment
`<attachment_metadata>` already gets.

Two things follow from making that claim, both of which the code has to honour or the framing does
harm rather than good.

The block's contents are not trusted just because the block is: they are notes, calendar summaries
and Home Assistant state, and email intake can reach notes. A note body containing `</turn_context>`
would end the block early, leaving whatever followed it looking like ordinary conversation to a
model that has just been told everything inside the block is system-generated.
`render_turn_context_block` therefore escapes both tags out of provider output.

And the description has to match the grant. `turn_context_guidance` takes
`includes_aggregated_context`, because telling a profile its notes and calendar are in the block
when the block holds only a clock invites it to answer "nothing scheduled" from an empty block
rather than saying it has no calendar access — the opposite of the deny the flag exists to express.
It also takes a `placement`, since a Live API session has no message list and inlines the block into
its system instruction instead of appending it.

### Anthropic needs the breakpoints spelled out

Implicit prefix caching means OpenAI and Gemini pick up the stable prefix for free. Anthropic caches
only as far as an explicit `cache_control` marker, so it gets two, placed in
`_convert_messages_to_anthropic_format`:

- **Ending the replayable history**, just ahead of the turn-context block. Everything before the
  block is what the next turn will send again, byte for byte. A breakpoint *past* the block would
  cache a prefix containing content the next turn does not have — cache writes that can never be
  read.
- **At the very end of the message list.** Within a turn the tool loop appends after the block, so
  this is what lets iteration *n+1* read what iteration *n* accumulated. Without it, a long tool
  loop re-reads its entire accumulated output every iteration, which is worst exactly where it
  matters most: `engineer` is the only Anthropic profile and runs at `max_iterations: 100`.

Two details are load-bearing, both about keeping a message's bytes identical from one turn to the
next:

- The history breakpoint **skips back over the whole run of user messages before the block**, rather
  than landing on the one immediately preceding it. Anthropic requires alternating roles, so those
  messages get merged into the block's turn — and merging rewrites plain string content into a
  one-element block list. The same historical message would then go out as a bare string on the turn
  it is ordinary history and as a list on the turn it sits next to the block. Anchoring on the
  newest message that does *not* merge with the block avoids that. The cost is the current turn's
  own user message falling outside the cached prefix: a message or two of text.
- Marking **never reshapes string content** for the same reason, and **never annotates a thinking
  block**, whose signature the API validates against exactly what it returned.

Three breakpoints including the system block, inside Anthropic's limit of four. Both conversation
breakpoints move as the conversation grows, so each request writes only the delta past the previous
one.

`cache_control` cannot change a response, so the VCR body matcher strips it before comparing.
Otherwise every future adjustment to breakpoint placement would invalidate unrelated recordings and
demand API keys to re-record them.

### Why it is placed once per turn, not repositioned per iteration

An ephemeral message that is not persisted leaves a "hole" in the sequence on the next turn, which
ends the cache hit at that position. Two placements were considered:

- **Place once, at the tail of the initial message list** (chosen). Within the turn every subsequent
  message appends after it, so all intra-turn requests hit the cache fully. At the start of the
  *next* turn the prompt diverges where the block used to be, so the previous turn's assistant and
  tool messages are re-read once. Cost ≈ *D* (one turn's tool output) per turn.
- **Reposition to the very tail on every iteration.** Turn boundaries then cache perfectly, but
  every iteration re-reads the ~7k-token block. Cost ≈ *k × C* where *k* is the iteration count.

With production numbers (*C* ≈ 7k tokens, *k* ≈ 7 iterations, *D* ≈ 11k tokens), placing once costs
~18k tokens per turn against ~49k for repositioning. Placing once is both cheaper and simpler, and
it requires no changes to the tool loop.

### Per-profile opt-in

Template omission is the current opt-out mechanism, and it is load-bearing for privacy: **11 of 17
shipped profiles deliberately leave `{aggregated_other_context}` out of their template**, including
`telephone_external` (external callers) and `media_analyst` (an `[A]`-profile that must not hold
`[B]`). Injecting unconditionally would hand the household's notes and calendar to all of them.

Rather than preserve that as an implicit property of the template — a placeholder that renders to
nothing but changes behaviour elsewhere is exactly the kind of thing a later reader "cleans up",
silently losing context — the opt-in becomes explicit:

`ProcessingConfig.include_aggregated_context: bool = False`

It defaults to **false** so that a profile that is never considered is denied rather than granted,
and is set to `true` on exactly the six profiles whose rendered template carried the placeholder
before this change: `default_assistant`, `data_visualization` and `camera_analyst` (which inherit
the top-level `prompts.yaml` template), plus `event_handler`, `complex_tasks` and `engineer`. It is
set per profile rather than on `default_profile_settings`, so that adding a profile without thinking
about it inherits the deny.

`excluded_context_providers` continues to work as the finer-grained control underneath it. That
makes `media_analyst`'s exclusion list redundant while its flag is false, and it stays: the two
mechanisms are independent, and the list is what keeps the profile safe if somebody later flips the
flag without re-reading why it was off.

The current time is injected for **every** profile regardless of the flag. It is not sensitive, all
but two profiles already interpolate it, and the two that do not (`research`, `research_max`) are
better off with it.

Those two are also the case where "every profile" takes work. Deep Research collapses the prompt
into a single `input` string and the client drops scaffolding on the way, so no block reaches the
model on either of its paths; `DeepResearchProcessingService` therefore folds the clock back into
the system prompt. That is the thing this design moved away from, and it is right here for the same
reason it is wrong elsewhere: what makes prompt interpolation costly is the cache prefix it
invalidates, and a single-shot research submission has no prefix to protect.

### Placeholder removal is a hard error, not a silent no-op

`{current_time}` and `{aggregated_other_context}` are removed from `format_args`, so a template that
still references either raises the existing unknown-placeholder `ValueError`. A new startup
validation renders every profile's prompt once at boot so this surfaces as a startup failure rather
than as a runtime error the first time someone talks to that profile.

### Voice and telephony

`gemini_live_api.py:177-227` and `asterisk_live_api.py:1543-1560` build their own system instruction
with naive `str.replace` substitution. `str.replace` on an absent placeholder is a no-op, so
removing the placeholders would have silently stripped the current time and all context from voice
mode. These are Live API sessions with no message list to append to, and prompt caching is not the
constraint there, so they append the *same rendered block* to their system instruction via the
shared renderer. One renderer, so the two paths cannot drift.

## Consequences for existing machinery

- **`_render_system_prompt`'s probe is deleted.** It rendered the prompt a second time with sentinel
  values and took the common prefix to find the boundary. With the prompt wholly static the boundary
  is `len(content)`, so `_VOLATILE_PROBE` and `_common_prefix_len` go away. `stable_prefix_len`
  itself stays: text appended later (attachment metadata, on-demand tool additions) must land
  *after* the breakpoint, and the field is what records where that is.

- **Turn scaffolding gets a marker.** A trailing synthetic user message is picked up by anything
  that scans backwards for what the user said. `llm_loop` already solved this for the
  final-iteration instruction with an object-identity check, which does not survive the list being
  rebuilt by pruning and cannot cross the `service.py` → `llm_loop.py` boundary. Both messages now
  carry `UserMessage.is_turn_scaffolding` and the identity checks are replaced by it. The predicate
  lives in `llm/messages.py`, beside the field, because the provider layer needs it and `processing`
  imports `llm` rather than the reverse. Four scans had to learn about it:

  - attachment selection (`llm_loop`), which would otherwise match boilerplate rather than the
    request;
  - `prune_messages_for_context`, which would count each block as a turn — at `min_turns=1` keeping
    *only* the block and discarding the user's request;
  - `BaseLLMClient._validate_user_input`, which checks the last user message for emptiness. The
    block is never empty, so without the skip the guard becomes unreachable and an empty trigger (a
    sticker, an unsupported media type) reaches the provider as a generic 400 instead of a typed
    error;
  - `_build_deep_research_create_kwargs`, which collapses the trailing run of user messages into a
    single `input` string — appending the block verbatim to the question being researched.

  The flag is `exclude`d from serialization because it is per-request state with no meaning in a
  stored row. That is *not* what keeps these messages out of the database:
  `MessageHistoryRepository.add_message` reads fields off the model rather than serializing it. What
  keeps them out is that the loop never yields them as messages to save.

- **Ordering constraint.** The block is appended *after* `_inject_trigger_attachment_metadata`,
  which scans backwards for the last `UserMessage`; appending earlier would attach the trigger's
  attachment metadata to the context block instead.

- The `SystemMessage` stays at index 0, which `llm_loop.py:373` and `llm/__init__.py:322` depend on.

- **The block's description is gated on actually sending one.**
  `ProcessingService.sends_turn_context_block` is false for `DeepResearchProcessingService`, whose
  transport drops the block on both its paths, so its system prompt does not promise the model
  something that never arrives.

## Expected impact

Turn-boundary requests go from ~46k uncached tokens to roughly ~10k uncached plus ~36k cached. At
the 90% cached-input discount that is a 60–70% reduction in input cost on the expensive requests,
plus a materially lower time-to-first-token on long conversations.

## Deliberately not doing

- **`user_name` stays in the static prompt.** Moving it would make the system prompt identical
  across all users of a profile, which would help group chats where the sender alternates. But the
  name is referenced throughout 15k characters of instructional prose, and rewriting that risks
  behaviour regressions for a case that is not the common one. Noted as a follow-up.
- **`prompt_cache_key` for OpenAI.** It routes requests to a consistent cache shard and mainly pays
  off at high concurrency. Threading it to the client means changing four `LLMInterface` signatures,
  the retrying wrapper, and all three providers. For a household-scale deployment the default
  prefix-hash routing is adequate; revisit if cache hit rates come back lower than the prefix
  arithmetic predicts.
- **Repositioning the block per iteration**, for the reasons in the cost comparison above.

## Verification

`cached_prompt_tokens` is already recorded per assistant message in `reasoning_info` by all three
providers. `/api/diagnostics/export` now aggregates it into the `summary` block so the before/after
can be read directly from production rather than inferred by diffing prompts.

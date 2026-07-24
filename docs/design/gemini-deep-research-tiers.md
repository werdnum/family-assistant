# Gemini Deep Research Tiers

## Context

Google published the Gemini Deep Research API as a generally documented feature in April 2026. The
public docs (https://ai.google.dev/gemini-api/docs/deep-research) expose two tiers:

- `deep-research-preview-04-2026` — regular tier, optimised for faster turnaround.
- `deep-research-max-preview-04-2026` — max tier, optimised for the most comprehensive, multi-source
  investigations at the cost of longer run times.

Both models ride the same `client.interactions.create()` transport we were already using for the
earlier `deep-research-pro-preview-12-2025` preview. The stream event shape (`interaction.start`,
`content.delta` with `text | thought_summary | image`, `interaction.status_update`,
`interaction.complete`, `error`) is unchanged.

## Decision

Expose the two tiers as separate service profiles rather than adding argument parsing to
`/research`:

- `research` → `deep-research-preview-04-2026`, slash command `/research`.
- `research_max` → `deep-research-max-preview-04-2026`, slash command `/research_max` (underscore,
  not hyphen — Telegram's BotFather only accepts `[a-z0-9_]{1,32}` for command names).

This mirrors the existing `default_assistant` vs `complex_tasks` split and keeps the slash command
surface flat. The old `deep-research-pro-preview-12-2025` model ID is retired; the `research`
profile now targets the documented regular tier.

## Implementation

1. `defaults.yaml` — retarget `research` at the new regular model and add a sibling `research_max`
   profile with the max model. Both profiles keep the same tools/policy stance (no local tools, no
   MCP, deny-by-default) since deep research handles its own web grounding server-side.
2. `src/family_assistant/llm/providers/google_genai_client.py` —
   - `_is_deep_research_model` stays as a substring match on `"deep-research"`, so both new model
     IDs route through the same code path automatically.
   - `agent_config` now includes `visualization: "auto"` alongside `thinking_summaries: "auto"`.
     This is the Google-recommended default and enables charts/diagrams for reports that benefit
     from them.
   - `content.delta` handler now has an explicit branch for `delta.type == "image"` that logs at
     debug level and skips. Surfacing visualisation images as attachments is deferred until we have
     a concrete UI requirement for them.
3. `src/family_assistant/web/routers/chat_api.py` — fallback description branch for the new
   `research_max` profile.
4. Tests in `tests/llm/test_google_deep_research.py` cover:
   - `_is_deep_research_model` matching both 04-2026 model IDs (plus the legacy preview for
     compatibility).
   - `agent_config` including `visualization: "auto"` on the max profile.
   - An `image` delta in the stream being ignored without breaking the stream.

## Non-goals

- **Image attachment plumbing.** Visualisation images from deep research are not yet surfaced to
  users. If demand materialises, the follow-up is to convert `image` deltas into `AttachmentPart`
  values on the streamed message.
- **collaborative_planning flag.** The Google API accepts `collaborative_planning: true` in
  `agent_config`, but our current chat surfaces aren't set up for the "plan review" turn that mode
  introduces. Left off for now.
- **Per-profile `agent_config` overrides.** Both profiles share the same `visualization: "auto"`
  default. If future tuning requires divergence, the natural next step is to plumb `agent_config`
  through `processing_config` rather than hardcoding it in the client.

## Open questions

- Telegram and email-intake surfaces do not yet route to `/research_max`. They will pick it up
  automatically via slash-command matching, but we haven't audited whether the longer latency is
  acceptable on the Telegram bot's response deadlines.

## Addendum: delegation is now submit-then-poll

When `research`/`research_max` are used as a `delegate_to_service` target (rather than a direct
`/research` chat), the delegation worker submits the interaction and polls it to terminal instead of
blocking a worker for the whole research run — see
[deep-research-pollable-delegation.md](deep-research-pollable-delegation.md). Direct chat usage is
unaffected and still streams via the transport described above.

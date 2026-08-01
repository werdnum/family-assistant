---
name: anthropic-api-dev
description: Build and update applications that use Claude API models, Messages API, multimodal input, tool use, structured output, thinking, streaming, prompt caching, or batches. Use for Anthropic model selection, model migrations, SDK integration, and current API capability checks.
---

# Anthropic API Development

Use Anthropic's official Claude Platform documentation as the source of truth. Fetch the relevant
model, migration, and API pages before changing an integration because thinking defaults and
accepted sampling parameters can differ between model generations.

## Model selection

Read [references/current-models.json](references/current-models.json) before selecting or changing a
model. The generated snapshot contains the latest-model comparison from Anthropic's public catalog.
Treat dateless Claude 4.6-and-later IDs as pinned releases, not rolling aliases.

Refresh all provider snapshots with `python scripts/refresh-provider-model-skills.py`.

## Integration workflow

1. Confirm the target model's input/output modalities and thinking behavior.
2. Fetch its model page and migration guide before changing request parameters.
3. Preserve thinking or signature blocks across tool turns when the model emits them.
4. Keep provider-specific IDs distinct from Bedrock and Google Cloud identifiers.
5. Verify multi-turn tool use with a focused live or recorded integration test.

Official sources:

- Models: https://platform.claude.com/docs/en/about-claude/models/overview.md
- API reference: https://platform.claude.com/docs/en/api/overview
- Model IDs: https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions

---
name: openai-api-dev
description: Build and update applications that use OpenAI API models, the Responses API, multimodal input, tools, structured output, reasoning, streaming, or embeddings. Use for OpenAI model selection, model migrations, SDK integration, and current API capability checks.
---

# OpenAI API Development

Use the official OpenAI developer documentation as the source of truth. Search and fetch the exact
guide or API reference needed before changing an integration; do not infer current behavior from a
model name alone.

## Model selection

Read [references/current-models.md](references/current-models.md) before selecting or changing a
model. It is OpenAI's own models page, mirrored verbatim, covering both the featured models and the
full catalog. Preserve an explicitly requested or intentionally pinned model unless the task asks
for a migration.

The mirror's header says when it was taken. If that date is not recent, or the answer matters, fetch
the URL in the header instead — it is the same page, current. Refresh all mirrors with
`python scripts/refresh-provider-model-skills.py`.

## Integration workflow

1. Confirm the target endpoint and required modalities or tools.
2. Fetch the relevant official guide and API schema.
3. Prefer the Responses API for new agentic, multimodal, or tool-using integrations.
4. Keep migrations scoped; update prompts or request fields only when the target model requires it.
5. Verify with a focused live or recorded integration test when credentials are available.

Official sources:

- Models: https://developers.openai.com/api/docs/models.md
- API reference: https://developers.openai.com/api/reference
- Libraries: https://developers.openai.com/api/docs/libraries

# Message History Querying

## Goal

Give the assistant reliable access to older conversation history without bloating every LLM request
with long transcripts.

The target behavior is:

- Answer questions like "what did I say about passports last month?"
- Find exact operational history like "when did you last call `add_calendar_event` for me?"
- Retrieve surrounding conversation context once a relevant message is found.
- Preserve user, processing-profile, and conversation isolation rules.

## Current State

Message history is already persisted in `message_history` with useful metadata:

- interface type and conversation ID
- timestamp, role, user ID
- turn ID and thread root ID
- processing profile and subconversation ID
- content, tool calls, tool responses, attachments, and errors

The current `get_message_history` tool exposes only recent messages from the current conversation,
filtered by `limit` and `max_age_hours`. It is useful for extending the short-term context window,
but it is not a search/retrieval capability.

The document index already provides hybrid semantic and keyword search over `documents` and
`document_embeddings`. It supports source types, embedding types, metadata filters, visibility
labels, and PostgreSQL vector search. SQLite returns no semantic/keyword results.

## Recommendation

Use a layered retrieval design rather than choosing only SQL or only the document index.

1. Add structured message-history queries for exact filters and audit-style questions.
2. Add semantic/keyword search by projecting selected message turns into the existing document
   index.
3. Add a hydrate step that retrieves the full turn/thread context from `message_history` after
   search returns candidate message IDs.

This gives the model both:

- deterministic lookup for time ranges, roles, tools, conversations, attachments, and errors
- semantic recall for fuzzy user language like "that thing we discussed about school forms"

The document index should be reused for semantic retrieval, but it should not become the source of
truth for message history. `message_history` remains canonical; indexed rows are derived search
artifacts.

## Why Not Only SQL-Like Queries?

SQL-style querying is excellent for:

- "show me messages from yesterday"
- "find tool failures"
- "what reminders did you schedule?"
- "messages with attachments"
- "assistant responses in this conversation"

It is weak for:

- vague references
- paraphrases
- recurring topics with changing wording
- "what did we decide about X?" when X was never named consistently

SQL-like access alone would push too much query planning onto the model and still miss semantic
matches.

## Why Not Only Reuse The Document Index?

The document index is good for fuzzy search, but raw chat history has structure that should not be
flattened away:

- turns group user, assistant, and tool messages
- threads and subconversations enforce context boundaries
- tool calls need exact name/argument/result access
- timestamps and roles matter
- attachments and errors have operational meaning

If all message retrieval goes through `search_documents`, the assistant gets snippets without a
guaranteed way to reconstruct the exact turn, tool result, or surrounding exchange. Indexing should
find candidates; message-history queries should hydrate and verify them.

## Proposed Model-Facing Tool

Replace the existing `get_message_history` tool with one richer read-only tool. There is no need to
preserve the old LLM tool interface for backwards compatibility.

### `get_message_history`

Structured and semantic lookup against message history.

Parameters:

- `query`: optional natural language or keyword text
- `search_mode`: `structured`, `semantic`, or `hybrid`
- `conversation_id`: optional conversation filter
- `scope`: default `same_user`; optionally `current_conversation` or `all_accessible` when policy
  allows
- `roles`: optional list of `user`, `assistant`, `tool`, `system`, `error`
- `tool_names`: optional list
- `start_time` / `end_time`: optional ISO datetimes
- `has_attachments`: optional boolean
- `has_error`: optional boolean
- `processing_profile_id`: optional, default current profile unless explicitly broadened
- `subconversation_id`: optional, default main conversation unless explicitly broadened
- `limit`: bounded, default around 20
- `include_context`: optional number of neighboring messages per result

Implementation notes:

- Use SQLAlchemy symbolic queries where possible.
- Use PostgreSQL full-text search for `content` when available.
- For SQLite tests/dev, use `LIKE` as a fallback for keyword matching.
- For semantic/hybrid mode, reuse the vector search stack with `source_type = "message_history"`.
- Store one indexed document per turn, not one per raw row.
- Use `documents.source_id = "message_turn:{turn_id}"` when `turn_id` exists.
- For older rows without a turn ID, use a stable fallback like `"message_row:{internal_id}"`.
- Put message IDs, conversation ID, interface type, user ID, timestamp range, thread root,
  processing profile, subconversation ID, roles, and tool names in document metadata.
- Search returns candidate turn IDs/message IDs, then hydrates from `message_history`.
- Return compact structured JSON, not a prose transcript.

## Indexing Shape

Index turn-level text because it matches how conversations are understood.

For each turn:

```text
User: ...
Assistant: ...
Tool add_calendar_event args: ...
Tool add_calendar_event result: ...
```

Do not index everything equally:

- Include user and assistant text.
- Include tool names and concise arguments/results.
- Include attachment descriptions and filenames.
- Exclude or heavily truncate large binary-derived payloads, stack traces, provider metadata, and
  very large tool outputs.
- Store exact large values only in `message_history`, then hydrate on demand.

Embedding types:

- `message_turn` for full turn content
- optionally `message_user_text` for user-authored content only
- optionally `message_tool_trace` for tool-call-heavy retrieval

## Security And Access Control

Message history is sensitive data. Treat both tools as `READ_ONLY`, `SENSITIVE_DATA`,
`OUTPUT_TRUSTED`, matching the current `get_message_history` metadata.

Default access should be conservative:

- same user by default where user identity is available
- optional conversation filter when the model needs to narrow to a specific conversation
- current processing profile and main subconversation unless explicitly broadened

Broader scopes should be profile-policy controlled. For untrusted-readonly profiles, read access may
be allowed, but state-changing tools must remain unavailable under the Rule of Two model.

The document index entries need visibility labels equivalent to the source message history scope.
Search must also re-check permissions during hydration; do not rely only on vector metadata filters.

Redaction is out of scope because message-history redaction is not supported.

## Implementation Plan

### 1. Replace Existing Recent-History Tool

- Expand `get_message_history` into the single message-history retrieval tool.
- Remove the old narrow interface rather than preserving it.
- Keep the default scope to same-user history, with `conversation_id` available as a narrowing
  filter.
- Add tests for current-conversation, same-user, and policy-denied broadened searches.

### 2. Add Structured Repository Query

- Add a typed query input object in the message-history repository layer.
- Implement filters for conversation, user, role, tool name, time range, attachments, errors,
  profile, and subconversation.
- Add pagination/limit bounds.
- Add functional tests with SQLite and PostgreSQL where possible.

### 3. Expose The Expanded `get_message_history`

- Expose the structured repository query through `get_message_history`.
- Return JSON with compact message summaries and stable IDs.
- Support `include_context` by fetching neighboring rows or full turns.
- Update the tool schema, registration metadata, and config entries for the new parameter shape.

### 4. Add Message-History Indexing

- Create a message-history indexer task that indexes stored turns in bounded batches.
- Upsert turn-level documents into the existing `documents` table with
  `source_type = "message_history"`.
- Dispatch embeddings through the existing embedding pipeline or a small dedicated path if the full
  document pipeline is too document-specific.
- Use a queued background task with a limit, for example `index_message_history_batch`.
- If the batch hits the limit and more work remains, enqueue/reschedule another batch.
- Use the same mechanism for initial backfill and ongoing catch-up after new turns are stored.

### 5. Add Semantic Mode To `get_message_history`

- Generate query embeddings with the existing embedding generator.
- Use the existing vector search path with `source_types = ["message_history"]` and metadata
  filters.
- Hydrate results through `message_history` by turn/message IDs.
- Return snippets plus exact timestamps and IDs.

### 6. Prompt And User Docs

- Update `prompts.yaml` so the assistant knows when to use:
  - structured mode for exact filters
  - semantic mode for fuzzy recall
  - hybrid mode when both are useful
- Update `docs/user/USER_GUIDE.md` because this is user-visible functionality.

### 7. Verification

- Unit/functional tests for repository filters.
- Tool tests for current-conversation, same-user, and denied/broadened scopes.
- Vector-search tests for indexed turns on PostgreSQL.
- SQLite fallback tests for structured lookup.
- End-to-end test: ask a question requiring older history outside the default prompt window.
- Run `scripts/format-and-lint.sh`, targeted tests, then `poe test`.

## Open Decisions

1. Indexing granularity: start with turn-level only, or also add user-message-only embeddings now?
2. How should ongoing indexing be triggered after a turn completes: directly enqueue a bounded
   indexing task from message persistence, or emit an indexing event consumed by the task worker?
3. What exact max batch size should the queued background indexer use?

## Suggested First Increment

Start by expanding `get_message_history` with structured same-user querying and optional
conversation filtering. It uses the canonical database, works on SQLite and PostgreSQL, and
immediately improves exact retrieval. Then add bounded queued indexing and semantic/hybrid modes
using the existing document index.

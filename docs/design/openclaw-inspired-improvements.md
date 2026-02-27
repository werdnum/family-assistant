# Proposed Improvements: Toward OpenClaw-Class Capabilities

## Date: 2026-02-27

## Context

This document proposes concrete improvements to Family Assistant, inspired by the OpenClaw/NanoClaw
ecosystem but grounded in FA's existing architecture and philosophy. These are ordered by
impact-to-effort ratio — the first proposals build directly on existing primitives with minimal new
machinery.

Channel breadth (WhatsApp, Discord, etc.) is explicitly out of scope — the `ChatInterface` protocol
is ready when needed, but it's not a capability gap that matters for the current use case.

______________________________________________________________________

## Proposal 1: Proactive Context Compaction with Summarization

### Problem

Context overflow is handled purely reactively. When a `ContextLengthError` fires, the
`prune_messages_for_context` function does a single retry:

1. Replace old tool results with `[Tool result truncated — originally N chars]`
2. If >3 turns exist, drop all but the 3 most recent

If that's still too long, the user sees "Our conversation has grown too long. Please start a new
conversation." — and the conversation's thread of reasoning is lost.

There is no token counting, no pre-emptive compaction, and no summarization. Dropped turns are gone,
not summarized.

Additionally, there's a bug: OpenAI streaming errors use `error_type="context_length"` but
`_ERROR_TYPE_TO_EXCEPTION` maps `"ContextLengthError"` (the class name). OpenAI streaming context
errors silently degrade to generic `RuntimeError`s, bypassing the retry path entirely.

### What OpenClaw Does

- Pre-emptive guard: estimates token usage before each LLM call
- Auto-compaction trigger at 80% of context window
- LLM-generated summarization of older messages
- Summarized context replaces the original messages, preserving the thread

### Interface Analysis: Conversation Patterns and Constraints

The five chat interfaces have fundamentally different conversation lifecycles, which shapes when and
how compaction should apply:

#### Telegram (`interface_type="telegram"`)

- **History window**: 10 messages / `history_max_age_hours` (default 24h, typically configured ~2h)
- **Conversation model**: One long-running conversation per `chat_id`. Users send messages
  throughout the day; the history window creates an implicit "session" boundary.
- **Threading**: Telegram reply chains create thread trees (`thread_root_id`). When a user replies,
  the full thread is loaded instead of recent history — thread history is **unbounded by the message
  limit** and can grow arbitrarily large through deep reply chains.
- **Message saving**: The Telegram handler saves the user message to history _before_ calling
  `handle_chat_interaction`, so the trigger message is already in DB when history is fetched.
- **Compaction relevance**: **High**. Long reply threads are the primary risk. A 20-message thread
  with tool calls in each turn can easily exceed context limits, and currently the only recourse is
  the hard prune-to-3-turns fallback.

#### Web UI (`interface_type="web"`)

- **History window**: `web_max_history_messages` (default: falls back to `max_history_messages` = 5)
  / `web_history_max_age_hours` (default: falls back to `history_max_age_hours` = 24h). Proposal 3
  notes these as 100 messages / 30 days, suggesting production config overrides the defaults.
- **Conversation model**: Per-session conversations identified by a client-generated
  `conversation_id`. Users explicitly start new conversations via the UI. Conversations can span
  hours or days if the user returns to the same session.
- **Streaming**: Web uses `handle_chat_interaction_stream()` which yields `LLMStreamEvent` objects
  via SSE. The compaction path must work within the streaming flow — a summarization LLM call in the
  middle of streaming would add noticeable latency before the first token.
- **Subconversations**: Web supports `subconversation_id` for branching within a conversation.
  History queries filter by subconversation, so compaction must be subconversation-aware.
- **Compaction relevance**: **High**. With the large history window (potentially 100 messages), long
  web sessions with tool-heavy interactions are the most likely to hit context limits. Streaming
  latency is the main UX concern.

#### A2A / Agent-to-Agent (`interface_type="a2a"`)

- **History window**: Uses the default (non-web) limits.
- **Conversation model**: Each A2A task creates a conversation, identified by a UUID. Typically
  short-lived: a single request → response with possibly a few tool calls.
- **State**: A2A tasks maintain their own task history in `a2a_tasks` table, separate from the
  message history used for context.
- **Compaction relevance**: **Low**. A2A interactions are typically single-turn. If a complex A2A
  task exceeds context, it's likely a task design problem, not a compaction problem.

#### System Callbacks / Task Worker (`interface_type` inherited from scheduling context)

- **History window**: Uses the interface type from the original scheduling context (usually
  "telegram" since callbacks are scheduled from Telegram conversations).
- **Conversation model**: A scheduled callback fires into an existing conversation. The callback
  saves a system trigger message, then calls `handle_chat_interaction` which loads recent history
  from the target conversation. The callback's context includes both the recent conversation history
  and the callback-specific trigger.
- **Compaction relevance**: **Low-Medium**. Callbacks typically produce short interactions (a
  reminder + response). However, if a callback fires into a conversation that's already near the
  context limit, it inherits that problem.

#### API (`interface_type="api"`)

- **History window**: Uses the default (non-web) limits, though `interface_type` can be overridden
  per request via `payload.interface_type`.
- **Conversation model**: Similar to Web but without streaming (uses `handle_chat_interaction`
  non-streaming path). The API caller manages conversation IDs.
- **Compaction relevance**: **Medium**. Same risks as Web but without the streaming latency concern.

### Design Constraints from Interface Analysis

1. **`prune_messages_for_context` is synchronous** — it's a pure function that operates on
   `Sequence[LLMMessage]`. To add summarization (which requires an async LLM call), we either need
   to make the function async or move the summarization to the caller (`process_message_stream`).
   Since `process_message_stream` is already async and is the single call site, moving summarization
   there is cleaner.

2. **Thread history bypasses the message limit** — When processing a Telegram reply,
   `_get_history_limits_for_interface` is called but the result is overridden by
   `get_by_thread_id()` which returns the _entire_ thread. Proactive compaction must handle this
   case: the token estimate should run _after_ thread history is resolved, not just after
   `get_recent` returns.

3. **Streaming adds latency constraints** — For the web streaming path, a synchronous summarization
   call before streaming starts would delay time-to-first-token. Options:

   - Accept the latency (summarization via Haiku is fast, ~1-2s)
   - Summarize asynchronously and include the summary in the _next_ turn instead of the current one
   - Only trigger proactive compaction for the non-streaming path; rely on reactive compaction for
     streaming (acceptable since reactive compaction already retries)

   Recommendation: Accept the latency. Haiku summarization of ~50 conversational turns takes \<2s,
   and the alternative (losing context) is worse.

4. **Subconversation awareness** — The history for a given conversation is filtered by
   `subconversation_id`. Compaction should operate on the same filtered set. Since compaction
   operates on the already-fetched `messages_for_llm` list (post-filtering), this is automatic.

5. **The bug affects only OpenAI streaming errors** — Non-streaming OpenAI errors raise
   `ContextLengthError` directly. Google/Gemini streaming errors use different error type strings.
   The fix needs to add `"context_length"` to the mapping (the key used by OpenAI's streaming error
   classifier at `openai_client.py:594`).

### Proposed Design

**Milestone 1: Fix the bug + improve reactive pruning** (Small)

1. **Fix the OpenAI error type mapping**: Add `"context_length": ContextLengthError` to
   `_ERROR_TYPE_TO_EXCEPTION` (in addition to the existing `"ContextLengthError"` key — both are
   needed since non-streaming errors may use the class name form).

2. **Add an async summarization utility**: A single function in a new module
   `src/family_assistant/llm/summarize.py`:

   ```python
   async def summarize_conversation(
       messages: Sequence[LLMMessage],
       llm_client: LLMInterface,
       max_summary_tokens: int = 500,
   ) -> str:
       """Summarize a sequence of conversation messages using a fast model."""
   ```

   The function formats messages into a simple transcript and asks the LLM to produce a concise
   summary preserving key facts, decisions, and open questions. Uses the existing `LLMInterface`
   (the same client, which may have a cheap fallback model configured).

3. **Integrate summarization into the reactive path**: In `process_message_stream`, when
   `ContextLengthError` is caught and `prune_messages_for_context` is called:

   - Before the prune, extract the messages that _will_ be dropped (turns beyond the 3 most recent)
   - Call `summarize_conversation` on the dropped messages
   - Insert the summary as a `SystemMessage` at position 1 (after the main system prompt):
     `"[Earlier conversation summary: ...]"`
   - This means `prune_messages_for_context` remains a pure synchronous function (it still drops
     turns as before), but the caller enriches the result with the summary

4. **Test coverage**:

   - Unit test: `_ERROR_TYPE_TO_EXCEPTION` correctly maps `"context_length"`
   - Unit test: `summarize_conversation` produces output given mock messages
   - Functional test: `ContextLengthError` during streaming triggers prune + summarize + retry

**Milestone 2: Proactive compaction** (Medium)

1. **Token estimation heuristic**: Add `estimate_token_count(messages: Sequence[LLMMessage]) -> int`
   using a character-based heuristic (~4 chars/token for English text, with a multiplier for tool
   call JSON which tends to be more token-dense). No external tokenizer needed.

2. **Context window configuration**: Add `context_window_tokens: int` to `ProcessingServiceConfig`.
   Default to a conservative value (e.g., 128k). This should be configurable per service profile
   since different models have different limits.

3. **Pre-LLM compaction check**: In both `handle_chat_interaction` and
   `handle_chat_interaction_stream`, after `messages_for_llm` is fully assembled (including thread
   history resolution, system prompt, and context provider fragments), but before calling
   `process_message_stream`:

   - Estimate total token count
   - If > 70% of `context_window_tokens`, trigger compaction:
     - Identify turns to summarize (oldest turns, keeping the 5 most recent)
     - Call `summarize_conversation` on the older turns
     - Replace the older turns with the summary `SystemMessage`
   - Log the compaction event with before/after token estimates for observability

4. **Compaction applies after thread resolution**: This is critical for the Telegram thread case.
   The check runs on the final `messages_for_llm` list, which has already been populated from
   `get_by_thread_id()` for reply chains. So a 30-message thread gets caught and compacted before it
   hits the LLM.

5. **No persistent compaction**: Summaries are ephemeral — they exist only in the in-memory message
   list for the current LLM call. The full message history remains intact in the database. This
   avoids complexity around storing compacted state and is consistent with how history is rebuilt
   from DB on each interaction.

### Scope

| File                                                         | Changes                                                                             |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| `processing.py` L106-115                                     | Add `"context_length"` key to `_ERROR_TYPE_TO_EXCEPTION`                            |
| `processing.py` `process_message_stream` ~L726-738           | Integrate summarization into reactive prune path                                    |
| `processing.py` `handle_chat_interaction` ~L2092-2164        | Add proactive compaction check (M2)                                                 |
| `processing.py` `handle_chat_interaction_stream` ~L2543-2589 | Same proactive check for streaming (M2)                                             |
| `processing.py` `ProcessingServiceConfig`                    | Add `context_window_tokens` field (M2)                                              |
| `llm/summarize.py` (new)                                     | `summarize_conversation()` function                                                 |
| `config_models.py` `ProcessingConfig`                        | Add `context_window_tokens` field (M2)                                              |
| `llm/providers/openai_client.py` L594                        | Verify error type string (already correct; the bug is in the mapping, not emission) |

### Complexity

Milestone 1: Small. Milestone 2: Medium.

______________________________________________________________________

## Proposal 2: Structured Identity Notes ("What I Know About You")

### Problem

Notes with `include_in_prompt=True` already act as persistent context — the user can store "About
Me" or "Family Preferences" and it appears in every system prompt. But there's no structured
mechanism for the assistant to _evolve_ its understanding of the user. The LLM is told "take a
concrete action such as adding a note" but there's no convention for what that note should look like
or how it should be maintained.

OpenClaw has `IDENTITY.md` (user preferences, environmental knowledge) and `SOUL.md` (behavioral
patterns, personality) that the agent actively maintains and evolves based on interactions.

### Proposed Design

This is primarily a **prompt engineering + skill** change, not a code change.

**A new builtin skill**: `identity-maintenance.md` that teaches the assistant how to maintain a
structured identity note. The skill would define:

- A well-known note title (e.g., "User Profile" or "About My Family")
- A structured format:
  ```
  ## People
  - [Name]: [relationship], [preferences, important dates, etc.]

  ## Routines
  - [Morning/evening routines, schedules, etc.]

  ## Preferences
  - [Communication style, dietary restrictions, etc.]

  ## Home
  - [Address, room layout, device names, etc.]
  ```
- Instructions for when to update it (after learning new information, correcting stale info)
- Instructions to merge rather than replace (append new facts, update changed ones)

**A system prompt addition**: A line like "You maintain a structured profile of the user and family
in a note called 'User Profile'. When you learn new information about the user, update this note.
Use the 'Identity Maintenance' skill for formatting guidance."

**Optional code enhancement**: A `NotesContextProvider` change to always place the identity note
_first_ in the context fragments, ensuring it's never truncated by context limits. Could also give
it a distinct header in the prompt (e.g., `## What I Know About You` vs. the generic
`Relevant notes:` header).

### Complexity

Small. Mostly creating the skill file and updating `prompts.yaml`.

______________________________________________________________________

## Proposal 3: Conversation Memory Across Sessions

### Problem

Telegram conversations have a 2-hour / 10-message history window. Web has 30 days / 100 messages.
When these windows expire, the assistant has no recollection of previous conversations — it starts
fresh each time. For an always-on personal assistant, this is a significant gap.

OpenClaw's memory system indexes conversation transcripts into a vector store, making all past
conversations searchable. The agent can recall "we talked about X last week" by doing a semantic
search over its own conversation history.

### Proposed Design

**Leverage the existing indexing pipeline.** Family Assistant already has:

- A full document indexing pipeline (chunking, embedding, vector search)
- `VectorRepository` with hybrid RRF search
- Background task dispatch for embedding generation

The missing piece: conversation transcripts aren't indexed.

**Milestone 1: Index conversation summaries**

- After a conversation session ends (detected by the history age window expiring, or a new
  conversation starting), generate a summary of the completed session
- Index the summary into the vector store as a document (with metadata: date, interface, user,
  conversation_id)
- Add a `memory_search` context provider or tool that searches past conversation summaries

**Milestone 2: Auto-recall relevant history**

- Before each conversation, search the vector store for past conversations relevant to the current
  message
- Include top-k results as additional context fragments
- This gives the assistant continuity: "Last Tuesday we discussed ..."

### Scope

- New: conversation session boundary detection (could be as simple as: "if last message in this
  conversation is older than `history_max_age_hours`, summarize and index it")
- New: post-conversation summarization task (queued via `TaskWorker`)
- Extend: `NotesContextProvider` or new `ConversationMemoryProvider` to inject relevant past
  conversations

### Complexity

Medium. The indexing and search infrastructure already exists — this is primarily about feeding
conversation transcripts into it.

______________________________________________________________________

## Proposal 4: Multi-Step Workflows with Approval Gates

### Problem

Complex multi-step tasks ("research this, draft an email, get my approval, then send it") rely
entirely on the LLM maintaining intent across tool call iterations. The LLM might:

- Lose track of the overall goal after many tool calls
- Skip approval steps (security concern for the Rule of Two model)
- Fail to retry appropriately on transient errors
- Hit the `max_iterations` limit partway through

OpenClaw's Lobster workflow engine separates orchestration from intelligence: LLMs handle creative
steps, the workflow engine handles sequencing, retrying, and approval gates.

### Proposed Design

Extend the existing Starlark scripting engine (`MontyEngine`) to support workflow primitives:

**New Starlark built-in functions:**

```python
# Pause execution and wait for user approval
approved = require_approval("I'm about to send an email to [recipient]. Proceed?")
if not approved:
    return {"status": "cancelled_by_user"}

# Execute an LLM step (delegates to a service profile)
result = llm_step("Summarize the following document: ...", profile="research_profile")

# Execute a tool directly
email_result = tool_call("send_email", to="user@example.com", subject="...", body=result)
```

**Workflow persistence:** Since workflows may span user approval waits (minutes/hours), workflow
state needs to persist across process restarts. This maps naturally to the existing `TaskWorker` +
`TasksRepository` — each workflow step is a queued task, with the workflow state stored in the task
payload.

**How it integrates:**

- User asks: "Research X, draft an email about it, and send it after I approve"
- LLM creates a Starlark workflow script using the `execute_script` tool
- The workflow runs: `llm_step("research X")` → `llm_step("draft email")` → `require_approval(...)`
  → `tool_call("send_email", ...)`
- Each step is observable, retryable, and auditable

### Scope

- `scripting/apis/`: New built-in functions (`require_approval`, `llm_step`)
- `task_worker.py`: Workflow step handler
- `storage/repositories/`: Workflow state persistence (could reuse `WorkerTasksRepository`)

### Complexity

Medium-Large. The Starlark engine and task worker exist, but wiring up approval gates and cross-step
state is non-trivial.

______________________________________________________________________

## Already Implemented: AI Worker Sandbox

The AI Worker Sandbox (originally Proposal 5 in the comparison doc) is now fully implemented:

- `spawn_worker` tool with Claude Code and Gemini CLI agent options
- `WorkerBackend` protocol with Kubernetes Jobs backend (production) and Docker backend (local dev)
- Persistent shared workspace with `workspace_files` tools for reading results
- Webhook-based result notification
- Security: `V1SecurityContext` with capability drops, non-root execution, read-only root filesystem

This closes the "Container/Sandbox Isolation" gap identified in the comparison doc. The
implementation aligns well with both OpenClaw's sandboxed tool execution and NanoClaw's container
isolation model.

______________________________________________________________________

## Summary: Recommended Ordering

| #   | Proposal                            | Effort | Impact | Builds On                                           |
| --- | ----------------------------------- | ------ | ------ | --------------------------------------------------- |
| 1   | Context compaction + summarization  | S→M    | High   | `prune_messages_for_context`, LLM client            |
| 2   | Structured identity notes           | S      | High   | Skills system, `NotesContextProvider`, prompts.yaml |
| 3   | Conversation memory across sessions | M      | High   | Indexing pipeline, vector search, TaskWorker        |
| 4   | Multi-step workflows                | M→L    | Medium | Starlark engine, TaskWorker, automations            |

Proposals 1-3 are the highest-leverage changes: they make the assistant _smarter over time_ (it
remembers, it evolves, it doesn't lose context). Proposal 4 makes it _more capable_ (it can handle
complex workflows with deterministic orchestration).

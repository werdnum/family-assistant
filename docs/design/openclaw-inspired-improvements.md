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

### Proposed Design

**Milestone 1: Fix the bug + improve reactive pruning**

- Fix the OpenAI error type mapping (`"context_length"` → `ContextLengthError`)
- Before dropping turns in `prune_messages_for_context`, generate a summary of the dropped turns
  using a fast/cheap model call (e.g., Haiku). Insert the summary as a `SystemMessage` after the
  main system prompt: `"[Earlier conversation summary: ...]"`
- This preserves the conversational thread while reducing tokens

**Milestone 2: Proactive compaction**

- Add rough token estimation (character-based heuristic is fine — ~4 chars/token for English). No
  need for exact tokenizer integration.
- Before each LLM call, estimate current context size vs. model's context window
- When exceeding a threshold (e.g., 70%), trigger summarization of older turns before the LLM call
  rather than waiting for a ContextLengthError
- Log the compaction for observability

### Scope

- `processing.py`: `prune_messages_for_context()`, `process_message_stream()`
- `processing.py`: `_ERROR_TYPE_TO_EXCEPTION` dict
- New: lightweight summarization utility (a single function that calls LLM with a "summarize this
  conversation so far" prompt)

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

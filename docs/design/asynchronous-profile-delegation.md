# Asynchronous Profile Delegation

**Status**: Draft **Date**: 2026-06-04

## Problem

`delegate_to_service` currently calls the target profile directly and waits for
`target_service.handle_chat_interaction()` to finish. This works for short specialist calls, but it
blocks the main assistant turn for long research, browsing, data analysis, and complex reasoning
work. The user has no useful control of the main conversation while the delegated profile is still
running.

We want asynchronous subagents with this behavior:

1. The main assistant delegates work to another profile.
2. If the delegated work finishes quickly, the tool returns the delegated result inline, preserving
   the current user experience.
3. If the delegated work takes longer than a configured threshold, control returns to the main
   conversation with a stable reference to the ongoing delegated task.
4. When the delegated task eventually finishes, the conversation is notified with the result and the
   reference.

The user-facing concept may be called `delegate_to_profile`, but the current code path is
`delegate_to_service`. This design keeps one implementation behind `delegate_to_service`; a future
rename or alias should not fork the behavior.

## Goals

- Preserve synchronous behavior for fast delegations.
- Make long delegations durable across request cancellation and process restarts.
- Keep delegated profile scratch history isolated by `subconversation_id`.
- Notify the original conversation when an async delegated run reaches a terminal state.
- Provide an actionable reference ID that the assistant can use to check status.
- Apply uniformly to local `ProcessingService` targets and remote A2A targets, since both implement
  `DelegatableService`.

## Non-Goals

- Do not design a new remote-agent protocol. Remote profile support already belongs to the A2A
  delegation layer.
- Do not stream every subagent token into the main conversation in the first implementation.
- Do not support arbitrary in-memory confirmation callbacks after the main turn has returned.
- Do not expose the delegated profile's full internal message history in the main chat feed.
- Do not replace `spawn_worker`, `worker_tasks`, or AI worker sandbox result handling.

## Current Architecture

Relevant pieces already exist:

- `src/family_assistant/tools/services.py::delegate_to_service_tool` validates the target profile,
  optional confirmation, attachment references, and `allowed_delegation_sources`, then calls
  `target_service.handle_chat_interaction()` directly.
- `DelegatableService` in `src/family_assistant/processing/protocol.py` abstracts local
  `ProcessingService` and remote A2A services behind `handle_chat_interaction()`.
- `TaskWorker` and `storage.tasks` provide a durable database-backed task queue.
- `MessageNotifier` and `/api/v1/chat/events` provide web live-update tickles when messages are
  committed through a `DatabaseContext` that has the notifier attached.
- Delegated history already uses a fresh `subconversation_id` so the target profile's internal
  messages are isolated from the main conversation history.
- `spawn_worker` already provides a separate asynchronous worker path for isolated Claude Code or
  Gemini CLI jobs. It stores `worker_tasks`, creates workspace task directories, launches a worker
  backend, and wakes the LLM through a webhook/event listener when the external job completes.

Two constraints matter for this design:

1. Tool execution happens inside the main turn's database context. A background task inserted into
   that transaction is not visible to the worker until the main turn commits. Async delegation must
   enqueue through an isolated transaction before waiting for completion.
2. The current live-message query returns all messages after a timestamp for a conversation without
   filtering out non-null `subconversation_id`. Async implementation must prevent delegated scratch
   messages from appearing directly in the main chat feed.

## Proposed Design

### Core Idea

Make each delegation a durable "delegation run" and back it with one task queue item.
`delegate_to_service` enqueues the run, then waits up to the configured handoff deadline for that
same run to finish. It never starts a second execution.

```
main LLM
  |
  | delegate_to_service(target, request)
  v
delegate_to_service_tool
  |
  | create delegation_run + delegated_profile_run task in isolated transaction
  v
TaskWorker executes target profile
  |
  +-- finishes before deadline -> tool returns result inline
  |
  +-- still running at deadline -> tool returns delegation_id reference
                               -> worker later sends completion notification
```

This keeps the synchronous fast path while making the slow path durable.

### Relationship to `spawn_worker`

Async profile delegation and `spawn_worker` both return control to the conversation while work
continues, but they are different execution products and should stay separate.

| Dimension    | Async profile delegation                                              | `spawn_worker`                                                    |
| ------------ | --------------------------------------------------------------------- | ----------------------------------------------------------------- |
| Executor     | Family Assistant `DelegatableService` profile                         | External Claude Code or Gemini CLI container                      |
| Data access  | FA tools/data allowed by the target profile policy                    | No FA tools, notes, calendar, documents, or Home Assistant access |
| Inputs       | Chat content parts, attachment references, conversation/user metadata | Workspace paths and a task prompt                                 |
| Outputs      | `ChatInteractionResult` text and attachment IDs                       | Workspace output files, summary, exit code, backend status        |
| History      | Stored in message history under `subconversation_id`                  | Stored in `worker_tasks` plus workspace task directory            |
| Completion   | Main-conversation assistant notification from the profile result      | Webhook event wakes the LLM; LLM uses `read_task_result`          |
| Status tools | `get_delegation_status`, `list_delegations`                           | `read_task_result`, `list_worker_tasks`, `cancel_worker_task`     |

Use async profile delegation when the task needs Family Assistant context or tools: notes, calendar,
documents, Home Assistant, profile-specific MCP tools, or profile-specific reasoning policy. Use
`spawn_worker` when the task is standalone computing or coding over files that have already been
materialized into the shared workspace.

The implementation should not reuse the `worker_tasks` table for delegation runs. `worker_tasks`
tracks external backend jobs and workspace artifacts; `delegation_runs` tracks profile-to-profile
chat work, `subconversation_id` isolation, policy rechecks, and `ChatInteractionResult` propagation.
The two systems can share notification infrastructure and UI patterns, but not persistence or result
contracts.

### Configuration

Add delegation timing settings to `ToolsConfig`:

```python
class ToolsConfig(BaseModel):
    delegate_handoff_after_seconds: float = 15.0
    delegate_handoff_max_seconds: float = 120.0
    delegate_status_poll_seconds: float = 0.25
```

Add optional tool parameters:

- `handoff_after_seconds`: overrides the configured threshold for this call, clamped to
  `0..delegate_handoff_max_seconds`.
- `delivery_hint`: optional `"auto" | "background"`; `"background"` is equivalent to
  `handoff_after_seconds=0`.

Default behavior is `auto`: wait briefly, then hand off if still running.

### Storage

Add a dedicated `delegation_runs` table rather than overloading the generic `tasks.payload` JSON as
the user-facing source of truth.

Proposed columns:

- `delegation_id`: stable public reference, e.g. `delegation_01hx...`, unique.
- `task_id`: corresponding row in `tasks.task_id`, unique.
- `status`: `queued | running | waiting_for_confirmation | completed | failed | cancelled`.
- `source_profile_id`: profile that called `delegate_to_service`.
- `target_service_id`: target profile.
- `interface_type`, `conversation_id`, `user_id`, `user_name`.
- `source_turn_id`, `source_tool_call_id`.
- `subconversation_id`: delegated profile history isolation key.
- `request_text`: delegated request text.
- `content_parts_json`: validated delegated content parts, including attachment references.
- `attachment_ids_json`: original attachment references for indexing and audit.
- `handoff_after_at`: absolute deadline after which completion should notify the conversation.
- `handed_off_at`: set when the tool has returned an async reference.
- `started_at`, `completed_at`, `updated_at`.
- `result_text`: final text returned by the target service.
- `result_attachment_ids_json`: attachments returned by the target service.
- `result_message_internal_id`: main-conversation completion notification message, if sent.
- `error`: terminal error details.
- `notified_at`: set after completion notification is delivered or recorded.

The task queue payload should be small:

```json
{
  "delegation_id": "delegation_...",
  "interface_type": "web",
  "conversation_id": "...",
  "user_name": "..."
}
```

The worker loads the full run row from `delegation_runs`.

### Tool Flow

`delegate_to_service_tool` changes from direct execution to enqueue-and-wait:

1. Validate service registry, target existence, source-target policy, and optional confirmation.
2. Validate attachment references and build `content_parts`.
3. Generate `delegation_id`, `task_id`, and `subconversation_id`.
4. Insert `delegation_runs` and enqueue `delegated_profile_run` in an isolated database context
   using `exec_context.db_context.engine`.
5. Poll the run row until either:
   - it reaches `completed` or `failed`, or
   - the handoff deadline passes.
6. If terminal before the deadline, return the same `ToolResult` shape as today's synchronous
   delegation.
7. If still running at the deadline, set `handed_off_at` and return:

```text
Delegation is still running.
Reference: delegation_...
Target profile: research
The conversation will be notified when it finishes.
```

The main assistant then summarizes that status to the user as its normal final response.

### Worker Flow

Register a new task type, `delegated_profile_run`, in `TaskWorker`.

The handler:

1. Loads the `delegation_runs` row by `delegation_id`.
2. Re-checks that the target service exists and the source profile is still allowed to delegate to
   it. This prevents stale queued work from bypassing changed policy.
3. Marks the run `running`.
4. Calls `target_service.handle_chat_interaction()` with:
   - `interface_type` and `conversation_id` from the original turn.
   - `trigger_content_parts` from the run row.
   - `user_name` and `user_id` from the original turn.
   - a fresh `subconversation_id` from the run row.
   - `chat_interface` and `chat_interfaces` from the worker context.
   - no in-memory confirmation callback in phase one.
5. Stores terminal status, result text, result attachments, and error details on the run row.
6. Sends a main-conversation notification if the run was handed off or completed after
   `handoff_after_at`.
7. Marks `notified_at` and stores the notification message ID.

Completion notification text should be concise and explicit:

```text
Delegated task delegation_... completed via research.

<target profile result>
```

For failures:

```text
Delegated task delegation_... failed via research.

<user-safe error summary>
```

The full delegated profile trace remains under `subconversation_id`; only the summary notification
is written to the main conversation.

### Message Delivery

Background completion must use the same delivery contracts as reminders and callbacks:

- For web conversations, save a normal assistant message and trigger `MessageNotifier` after commit,
  plus push notification if configured.
- For Telegram and other interfaces, call the appropriate `ChatInterface.send_message()` and update
  the stored `interface_message_id` when possible.

Implementation should also pass `MessageNotifier` into task-worker database contexts. Today web
request contexts attach the notifier, but worker-created contexts do not. Without that injection,
web users may only see completion after polling or refresh.

The live-message endpoints should filter out delegated scratch messages:

- Default chat feed: `subconversation_id IS NULL`.
- Debug/context views may opt into `subconversation_id="*"` or a specific delegation run.
- Completion notifications are saved with `subconversation_id=NULL`.

### Status Tools

The reference ID should be actionable. Add small management tools:

- `get_delegation_status(delegation_id)`: returns status, target profile, age, and result preview or
  error when terminal.
- `list_delegations(status: optional, limit: optional)`: lists recent runs for the current
  conversation.

Cancellation can be a later milestone:

- Pending queued tasks can be cancelled by marking the run and task cancelled.
- Running local profiles need cooperative cancellation support in `TaskWorker`.
- Remote A2A targets can map cancellation to `tasks/cancel` when available.

### Confirmations

Phase one should fail fast if an async background delegated profile reaches a tool that requires
confirmation and no durable confirmation callback is available. Returning a clear error is better
than silently dropping the confirmation or hanging the background run.

Later, add a durable background confirmation callback that:

1. Creates a confirmation request in the confirmation service.
2. Sends a main-conversation message asking for approval.
3. Parks the delegated run in `waiting_for_confirmation`.
4. Resumes the run after approval using the existing durable confirmation execution path.

This is a separate workflow because the original in-memory callback cannot be serialized after the
main turn has returned.

### Race Handling

Important races and intended behavior:

- **Worker completes before the deadline**: tool observes terminal status and returns inline. Worker
  does not send a separate notification unless `handed_off_at` is already set.
- **Worker completes after the deadline but before the tool marks handoff**: worker compares current
  time to `handoff_after_at` and sends notification.
- **Tool crashes after enqueue**: run remains durable. If it completes after `handoff_after_at`, the
  worker notifies. If it completes before the deadline and the caller disappears, a cleanup job can
  notify stale unclaimed completions in a later hardening pass.
- **Process restarts while running**: task row remains `processing` until existing task recovery
  behavior handles it. If the current task queue does not reclaim stale locks, add that before
  relying on async delegation for long-running work.

### Observability

Add OpenTelemetry spans and logs for:

- `delegation.enqueue`
- `delegation.wait_inline`
- `delegation.handoff`
- `delegation.worker.execute`
- `delegation.notify`

Useful attributes:

- `delegation.id`
- `delegation.target_service_id`
- `delegation.source_profile_id`
- `delegation.status`
- `conversation.interface`
- `conversation.id`
- `conversation.subconversation_id`

## Implementation Plan

### Milestone 1: Storage and Repository

1. Add `delegation_runs` table and Alembic migration.
2. Add repository methods:
   - `create_run`
   - `mark_running`
   - `mark_handed_off`
   - `mark_completed`
   - `mark_failed`
   - `get_by_delegation_id`
   - `list_for_conversation`
3. Unit-test status transitions and conversation scoping.

### Milestone 2: Worker Handler

1. Add `delegated_profile_run` task handler.
2. Resolve local or remote targets through `processing_services_registry`.
3. Execute with the stored `subconversation_id`.
4. Persist terminal results.
5. Send completion notification when appropriate.
6. Add functional tests for success, failure, attachments, and notification delivery.

### Milestone 3: Tool Handoff Behavior

1. Update `delegate_to_service_tool` to enqueue and wait instead of calling the target directly.
2. Add timing config and optional tool parameters.
3. Preserve existing fast-path `ToolResult` behavior.
4. Return async reference text after timeout.
5. Update existing delegation tests to cover both inline completion and async handoff.

### Milestone 4: Main Feed Isolation and Live Updates

1. Filter live-message sync endpoints to `subconversation_id IS NULL` by default.
2. Inject `MessageNotifier` into task-worker database contexts and web send paths.
3. Add tests proving delegated scratch messages do not appear in normal chat history or SSE.
4. Add tests proving completion notifications do appear in the main conversation.

### Milestone 5: Status Tools and User Docs

1. Add `get_delegation_status` and `list_delegations` tools.
2. Register tools in code and config.
3. Update `docs/user/USER_GUIDE.md` with async delegation behavior.
4. Update `prompts.yaml` so the assistant understands:
   - delegation may return a reference instead of a result,
   - it should give the reference to the user,
   - it can check status with the status tool,
   - the conversation will be notified on completion.

## Test Strategy

Required coverage:

- Fast delegation completes before the handoff threshold and returns inline.
- Slow delegation returns a reference and does not block the main turn past the threshold.
- Slow delegation eventually posts a main-conversation completion notification.
- Failure in the target profile posts a failure notification and marks the run failed.
- Attachments passed into a delegated run remain available to the target profile.
- Attachments produced by the target profile are included in inline results and async completion
  notifications.
- Delegated scratch messages with non-null `subconversation_id` are hidden from the normal chat
  feed.
- `get_delegation_status` is scoped to the current conversation.
- Policy changes between enqueue and execution are rechecked by the worker.
- `poe test` passes after implementation.

## Open Questions

1. Should `delegate_to_service` expose `delivery_hint`, or should all control come from profile
   config? The implementation can start with config-only and add the argument later if the model
   needs explicit control.
2. Should early-completed but unclaimed runs be notified by a periodic cleanup job? This only
   matters when the main turn crashes between enqueue and inline return.
3. How much of the delegated trace should be exposed in debug UI? The main feed should hide it, but
   a profile/debug view should make it inspectable by `delegation_id`.

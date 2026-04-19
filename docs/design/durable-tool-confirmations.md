# Durable Tool Confirmations

## Status

Proposed design for review.

This design replaces the earlier idea of modeling email intake actions as a secondary LLM-authored
"planned action" entity. The durable object should instead be a pending tool confirmation: an exact
tool invocation that policy has determined needs user approval before it can execute.

The core security boundary is deterministic confirmation by an authenticated user. LLM
summarization, extraction, or rewriting is useful for UX, but it does not remove taint from
untrusted input and must not be treated as an authorization boundary.

## Goals

- Persist pending confirmations so approval state survives process restarts and can be surfaced by
  more than one interface.
- Preserve existing in-context confirmation UX for Web and Telegram.
- Support cross-interface approval as a normal routing case: a request belongs to a user, and any
  authorized interface for that user may surface or resolve it.
- Store and execute the exact confirmed tool name and arguments, not an LLM-generated summary of
  intent.
- Provide a clean foundation for email intake, scheduled callbacks, and other async sources that
  cannot wait on an in-memory confirmation future.
- Keep v1 small enough to land safely without making the entire processing loop durable.

## Non-Goals

- Do not introduce a general "planned action" abstraction for email.
- Do not treat LLM summaries as trusted or sanitized.
- Do not attempt full conversation suspension and resumption in v1.
- Do not let approval rerun the LLM over the original untrusted input.
- Do not add arbitrary cross-user or cross-interface approval. The approving identity must map to
  the same target user as the pending request.

## Current State

The tool stack already has policy decisions and confirmation-aware advertisement:

- `PolicyEnforcingToolsProvider` evaluates tools as `allow`, `deny`, or `confirm`.
- Confirm-required tools are advertised only when `can_confirm=True`.
- Execution calls `ToolExecutionContext.request_confirmation_callback` and waits for a boolean.
- Web and Telegram each maintain their own in-memory pending confirmation state.

This works for same-interface live chat, but the state is process-local and callback-shaped. That is
not sufficient for email intake or other async sources because there may be no live interface
waiting for the answer.

## Design Principle

Confirmation requests are pending tool invocations.

A confirmation request should answer:

- Who is allowed to approve this?
- Which exact tool call will execute if approved?
- Which source caused the request?
- Which interface(s) may surface it?
- Has it expired, been rejected, been approved, executed, or failed?

It should not answer "what did the LLM intend in natural language?" beyond a user-facing rendering
of the exact tool call.

## Data Model

Add a `confirmation_requests` table.

Required fields:

- `id`: stable request id, suitable for URLs and callback payloads.
- `target_user_id`: authenticated user who may approve or reject the request.
- `status`: `pending`, `approved`, `rejected`, `expired`, `executing`, `executed`, `failed`.
- `tool_name`: exact tool to execute.
- `tool_args_json`: exact JSON arguments to execute after approval.
- `tool_args_digest`: canonical digest of `tool_name` plus normalized `tool_args_json`.
- `tool_schema_version`: tool-provided semantic version when available.
- `tool_descriptor_digest`: canonical digest of the tool descriptor/schema presented when the
  confirmation was created.
- `tool_call_id`: LLM tool call id when available.
- `processing_profile_id`: profile used to create and later execute the request.
- `source_interface`: `web`, `telegram`, `email`, `task_worker`, etc.
- `source_conversation_id`: original conversation id when available.
- `source_turn_id`: original turn id when available.
- `source_message_id`: interface message id, email id, worker task id, or other source reference.
- `confirmation_prompt`: rendered human-readable prompt.
- `metadata_json`: source metadata, risk tags, policy reason, and display hints.
- `expires_at`.
- `created_at`, `updated_at`.
- `resolved_at`.
- `resolved_by_user_id`.
- `resolved_via_interface`.
- `execution_task_id`: deterministic task id for the queued execution, such as
  `confirmation_tool_execution:{id}`.
- `execution_idempotency_key`: stable idempotency key derived from the confirmation id for tools
  that can pass one to downstream APIs.
- `execution_token`: per-attempt token written when a worker claims execution.
- `execution_lease_expires_at`: heartbeat/lease expiry for active execution.
- `execution_started_at`, `execution_finished_at`.
- `execution_result_json`.
- `execution_error`.

Constraints:

- Only `pending` requests may transition to `approved`, `rejected`, or `expired`.
- Only `approved` requests may transition to `executing`.
- Only `executing` requests may transition to `executed` or `failed`.
- Approval and rejection must be idempotent from the user's perspective. Replaying an approval for
  an already resolved request should not execute the tool twice.
- Execution must validate that the current tool descriptor is compatible with
  `tool_schema_version`/`tool_descriptor_digest`. If compatibility cannot be proven, execution fails
  closed and notifies the user that the tool changed after approval.
- Terminal execution writes must match the active `execution_token` so stale workers cannot
  overwrite newer recovery decisions.

## Service API

Introduce a central confirmation service, backed by the repository:

```python
class ConfirmationService:
    def __init__(
        self,
        *,
        db_context_factory: Callable[[], AbstractAsyncContextManager[DatabaseContext]],
    ) -> None: ...

    async def create_request(
        *,
        target_user_id: str,
        tool_name: str,
        tool_args: dict[str, object],
        tool_call_id: str | None,
        processing_profile_id: str,
        source_interface: str,
        source_conversation_id: str | None,
        source_turn_id: str | None,
        source_message_id: str | None,
        confirmation_prompt: str,
        expires_at: datetime,
        metadata: dict[str, object],
    ) -> ConfirmationRequest: ...

    async def approve_and_enqueue_execution(
        *,
        request_id: str,
        approving_user_id: str,
        approving_interface: str,
    ) -> ConfirmationRequest: ...

    async def reject(
        *,
        request_id: str,
        rejecting_user_id: str,
        rejecting_interface: str,
    ) -> ConfirmationRequest: ...

    async def list_pending_for_user(
        *,
        user_id: str,
    ) -> list[ConfirmationRequest]: ...

    async def mark_expired(self, *, now: datetime) -> int: ...
```

The service owns authorization checks for approval and rejection. UI routers and Telegram handlers
should not directly mutate rows.

Approval of an executable confirmation is not a plain status update. It is a single database
transaction that both resolves the confirmation and enqueues the execution task.

That method should update `pending -> approved` with a compare-and-set predicate and insert a
`confirmation_tool_execution` task with a deterministic task id such as
`confirmation_tool_execution:{request_id}` in the same transaction. If the transaction rolls back,
neither the approval nor the task enqueue is visible. If an approval is replayed after the request
is already resolved, the service must not enqueue a second execution task.

There should not be a general public `approve()` method for executable confirmations, because that
would make it too easy to create an `approved` row without durable execution work. Non-executable
confirmation prompts, if they are ever needed, should use a separate API and status model.

The service should own its write transactions through a context factory. It must not rely on the
long-lived `ToolExecutionContext.db_context` from the active conversational turn for durable
confirmation rows or execution enqueueing.

## Queue-Backed Execution Invariant

The reliable execution boundary is the existing database task queue.

For every confirmation that can cause a tool side effect:

1. Creating the confirmation only creates a `pending` request.
2. Accepting the confirmation authorizes the user, moves `pending -> approved`, and enqueues exactly
   one `confirmation_tool_execution:{request_id}` task in the same database transaction.
3. Tool execution happens only in the task worker handler for `confirmation_tool_execution`.
4. Interface handlers may wait for and display the result, but they do not call the wrapped tool
   directly after approval.

This uses the queue properties that already exist in the application:

- `tasks.task_id` is unique, so duplicate approval attempts cannot create duplicate execution work.
- Enqueueing through the same `DatabaseContext` transaction gives atomic handoff: approval and
  queued work become visible together or not at all.
- The worker claims work through the queue's atomic dequeue path (`SELECT FOR UPDATE SKIP LOCKED` on
  PostgreSQL, atomic update on SQLite).
- Worker wakeup is an optimization. Even if the wakeup event is missed, the durable task remains
  visible to polling workers.

The queue solves the important crash window where approval commits but no executor is durable. It
does not, by itself, make arbitrary external side effects exactly-once if a process dies after a
downstream API call but before recording success. The confirmation id should be passed as a stable
idempotency key to tools and downstream APIs where that is supported. For non-idempotent tools, v1
should prefer "no duplicate side effect" over automatic retry once the handler has crossed the
execution boundary.

## Transaction Boundaries And Visibility

Durable confirmations must not be written through a transaction that stays open for the whole
conversation turn.

Today, `DatabaseContext` starts a transaction when entered and commits only when it exits. Current
chat processing commonly passes one `db_context` through trigger persistence, context gathering, LLM
streaming, tool execution, confirmation waiting, and generated-message persistence. If a durable
confirmation row or execution task is inserted through that ambient transaction, other interfaces
and task workers cannot see it until the whole turn exits. That breaks the confirmation path:

- Web or Telegram may receive a request id for a row that their approval endpoint cannot read yet.
- `approve_and_enqueue_execution()` may enqueue a task that workers cannot see until the live turn
  finishes waiting.
- A live turn may wait for a worker result while the worker is blocked by uncommitted state from the
  same turn.

This is a PostgreSQL correctness issue because uncommitted rows are invisible across connections. It
is also a SQLite test/dev issue: nested "isolated" contexts may share the same connection or be
blocked by the outer transaction, so relying on a second context while the conversational context is
open will produce deadlocks, missing rows, or misleading tests.

The confirmation implementation needs explicit transaction boundaries:

- Creating a confirmation request must happen in a short transaction that commits before the
  interface emits the confirmation request event or starts waiting.
- Rejection must happen in a short transaction that commits before notifying waiters.
- Approval must happen in a short transaction that commits `pending -> approved` and the
  deterministic task enqueue before any waiter expects worker execution.
- Worker execution must load and move `approved -> executing` in a short transaction that commits
  before calling the wrapped tool. That transition writes a fresh `execution_token` and lease
  expiry.
- Long-running execution must heartbeat by extending `execution_lease_expires_at` in short
  transactions using the same `execution_token`.
- Terminal recording (`executed` or `failed`) must happen in a later short transaction after the
  tool returns or fails, guarded by the same `execution_token`.

The practical implementation shape is:

1. Refactor conversational processing so it does not keep one write transaction open across LLM
   streaming, tool execution, or confirmation waits. Use phase-scoped transactions instead: persist
   the incoming user turn and gather committed context, run the LLM/tool loop without an ambient
   transaction, and persist generated messages/results through short transactions.
2. Give tool execution a database context factory, not just a single active `db_context`, so tools
   and confirmation services can perform their own scoped reads and writes.
3. Add a task-worker execution mode for `confirmation_tool_execution` that is not wrapped in one
   processing transaction for the full handler duration. The queue claim can remain transactional,
   but the confirmation request's `approved -> executing` transition must commit before any external
   side effect.
4. Keep SQLite support honest by testing this with no hidden nested transaction dependency. If the
   SQLite engine uses a single shared connection in tests, the live confirmation path must still
   work because there is no outer conversational transaction open while waiting.

## V1 Execution Model

V1 keeps existing live-turn behavior while making pending requests durable.

### Live Web Or Telegram Turn

1. The assistant calls a confirm-required tool.
2. `PolicyEnforcingToolsProvider` calls the existing confirmation callback.
3. The callback creates a durable confirmation request in a short transaction and commits it.
4. The current interface surfaces that committed request in-context.
5. The processing coroutine waits on an in-memory waiter for that request id.
6. Rejection transitions the durable row from `pending -> rejected` and notifies the waiter.
7. Approval calls `approve_and_enqueue_execution()`, which transitions `pending -> approved` and
   enqueues the execution task in the same transaction.
8. The task worker executes the stored tool invocation and records `executed` or `failed`.
9. If the original process is still alive, the waiter is notified when the request reaches a
   terminal execution state and returns the recorded tool result to the assistant loop.

The durable row is the source of truth. The in-memory waiter is only an optimization for the live
conversation that created the request; it is not the executor.

The implementation should let the confirmation callback return or otherwise expose the confirmation
request id to the provider. Keeping a boolean-only internal interface would make it too easy to
confuse "approved" with "executed". A compatibility adapter can preserve existing call sites while
the durable path uses a richer result internally.

If the process restarts while a live turn is waiting, v1 does not need to resume the original
conversation automatically. The durable request remains pending if the user had not approved it yet.
If the user already approved it, the durable task remains queued or processing and the result is
recorded for later notification or inspection.

### Async Source Or No Live Waiter

For email intake and other async sources:

1. The assistant or worker reaches a confirm-required tool call.
2. The callback creates a durable confirmation request.
3. The worker stops or returns a "waiting for confirmation" result.
4. Web and Telegram can list or notify the target user about the pending request.
5. Approval atomically enqueues execution of the stored tool invocation.
6. The result is recorded and optionally sent to the user through a deterministic notification path.

This path executes the stored tool name and args directly. It does not ask the LLM to reinterpret
the email or produce a new action.

## Deferred Execution

Add a task type such as `confirmation_tool_execution`.

Payload:

```json
{
  "confirmation_request_id": "confirm_...",
  "approving_user_id": "user_..."
}
```

Approval and enqueue:

1. Authenticates the approving user.
2. Opens one database transaction.
3. Moves `pending -> approved` using a compare-and-set predicate that includes target user and
   expiry checks.
4. Enqueues a `confirmation_tool_execution` task using the existing task queue in that same
   transaction.
5. Stores the deterministic task id on the confirmation row.
6. Commits the transaction, making approval and queued execution visible together.

The handler:

1. Loads the request.
2. Verifies the task id matches `execution_task_id`.
3. Verifies that the current tool descriptor/schema is compatible with the stored
   `tool_schema_version` and `tool_descriptor_digest`. If not, it records `failed` without executing
   the tool.
4. Reconstructs a `ToolExecutionContext` from stored source fields and processing profile.
5. Re-evaluates policy before execution.
6. In a short transaction, atomically moves `approved -> executing`; if that transition does not
   apply, it exits without executing the tool. This transition writes `execution_token` and
   `execution_lease_expires_at`.
7. Executes the stored tool through the same policy-enforcing provider, passing
   `execution_idempotency_key` where the tool API supports it and passing the confirmation approval
   signal described below.
8. In a later short transaction, records `executed` with the result or `failed` with the error,
   guarded by `execution_token`.
9. Sends a deterministic notification to the target user if a notification route is configured.

The handler should treat `approved -> executing` as the default side-effect boundary. Failures
before that transition should raise normally so the task queue can retry. Failures after that
transition should be recorded on the confirmation request and should not ask the queue to retry the
same non-idempotent operation by default. Specific tools can opt into safe retry only when they have
a downstream idempotency key or another tool-specific exactly-once mechanism.

Policy must still be enforced at execution time. If policy has changed since request creation, the
execution should fail closed unless the implementation explicitly stores and validates a policy
snapshot. V1 should fail closed, record the policy-denial reason, and notify the user that the
approved action could not be executed because the current tool policy no longer allows it.

Execution-time policy re-evaluation must not create a second confirmation prompt for the same
approved invocation. The queued execution context should carry:

- `approved_confirmation_request_id`
- `approved_tool_name`
- `approved_tool_args_digest`

`PolicyEnforcingToolsProvider` should still apply `deny` decisions normally. If the policy result is
`confirm`, it may treat the stored approval as satisfying confirmation only when the current tool
name and normalized args digest exactly match the approved confirmation. Any mismatch fails closed.
This is an approval bypass for the single already-confirmed invocation, not a broad bypass of tool
policy.

The service also needs a stale-execution cleanup path. If a worker crashes after moving a request to
`executing`, a periodic cleanup task should identify requests whose `execution_lease_expires_at` is
in the past and transition them to `failed` with an explicit timeout/crash reason. The cleanup
update must be compare-and-set guarded by the observed `execution_token` and lease timestamp. Active
long-running workers must extend the lease before it expires; terminal `executed`/`failed` writes
must also match `execution_token`. This prevents a cleanup task from incorrectly marking an active
execution failed and then racing with the worker's eventual terminal write.

There should not normally be stale `approved` rows without an execution task. If such rows exist
because of a migration bug or manual database edit, an audit/repair task may enqueue the
deterministic task id after verifying no task exists, but that is a safety net rather than the
primary reliability mechanism.

## Relationship To Tool Policy

`can_confirm` currently means "this active interaction has a confirmation callback." With durable
confirmations, the more useful distinction is:

- `can_confirm_in_context`: the current interaction can surface and wait for confirmation now.
- `can_create_durable_confirmation`: the current processing context can create a pending request for
  an authenticated user.

For v1, keep the public provider API as close as possible to the current `can_confirm` shape, but
thread enough context through execution to create durable requests. A later cleanup can split the
capability names if it improves clarity.

For email intake, confirm-required tools should be available only when the system can map the email
to a target user and create durable confirmations for that user. If user mapping is unavailable,
confirm-required tools should be hidden or denied.

## Rendering

The confirmation prompt must expose the exact action being approved:

- Tool name or friendly action label.
- Destination or affected object.
- Full user-visible arguments that matter for authorization.
- Source context such as "from email subject X" or "from Telegram conversation Y".
- Clear warning when values came from untrusted external content.

Renderers may use friendly formatting, but approval applies to `tool_name` plus `tool_args_json`.
The renderer output is not the executable payload.

Existing specialized renderers for calendar modification/deletion can continue to exist. They should
write their rendered prompt into the durable request when the request is created.

## Authorization

Approval rules:

- The approving principal must authenticate as `target_user_id`.
- Interface-specific identifiers, such as Telegram chat id or Web session user id, must map to that
  same user.
- A request created for one user must not be visible to or approvable by another user.
- Request ids in callback payloads are bearer-ish handles and must not be sufficient by themselves.
- Expired requests cannot be approved.

This makes cross-interface approval normal and safe: the source interface does not matter once the
request is assigned to a target user.

## Email Intake Implications

Email intake should no longer create durable "email action proposals" as a separate action model.
Instead, email processing should run through the normal assistant/tool path with an email-specific
profile and durable confirmation capability.

Important constraints:

- Mailgun verification and user mapping establish which user submitted the email.
- Forwarded email body remains untrusted input.
- Email profile policy should deny broad dangerous surfaces such as browser, code execution, worker,
  delegation, destructive operations, and general external communication.
- Calendar writes, note writes, reminders, and messages to known users can be confirm-required.
- Confirmation should display exact tool args and source metadata before execution.
- Approval executes the stored tool invocation, not a planner summary.

## Conversation Resumption

Full conversation resumption is a future enhancement.

The likely v2 shape:

1. Persist pending tool calls as part of turn state.
2. When a tool request is approved, append the resulting tool message to history.
3. Resume the assistant loop with the stored messages, reconstructed activated tools, and original
   profile.
4. Handle multiple pending tool calls from one assistant response deterministically.

This is valuable because the assistant can explain the result after approval, ask follow-up
questions, or continue a multi-step task. It is not required for the first durable confirmation
milestone.

## Implementation Milestones

### Milestone 1: Durable Confirmation Core And Queue Handoff

- Add table, migration, repository, and service.
- Add or expose a database context factory for confirmation operations instead of using the active
  conversational transaction.
- Add `approve_and_enqueue_execution()` using one database transaction for `pending -> approved` and
  `confirmation_tool_execution:{request_id}` enqueue.
- Add the `confirmation_tool_execution` handler with request-level state transitions and terminal
  result recording.
- Add descriptor/schema digesting and exact approved-invocation matching for queued execution.
- Add execution leases/heartbeats and token-guarded terminal writes.
- Add status transition tests, including wrong-user rejection and idempotency.
- Add expiry cleanup logic.

### Milestone 2: Transaction Boundary Refactor

- Stop holding one `DatabaseContext` transaction open across the full conversational turn.
- Persist incoming user messages, generated messages, tool attachments, and confirmation state
  through phase-scoped transactions.
- Add task-worker support for a `confirmation_tool_execution` handler that commits
  `approved -> executing` before executing the wrapped tool and commits terminal status after.
- Verify this on both SQLite and PostgreSQL before wiring live adapters to durable confirmations.

### Milestone 3: Web Adapter

- Make Web confirmation creation and resolution go through `ConfirmationService`.
- Preserve existing SSE events and current user experience.
- Keep the in-memory waiter map only as a process-local bridge from durable request id to the
  waiting coroutine and recorded task result.

### Milestone 4: Telegram Adapter

- Make Telegram button confirmations resolve durable requests.
- Validate Telegram chat/user mapping before approval.
- Keep existing button UX.

### Milestone 5: Email Intake Integration

- Replace email action proposal planning with normal tool calls under an email intake profile.
- Allow confirm-required tools only when inbound email maps to a user and durable confirmations are
  available.
- Add functional tests for forwarding a booking email, creating a pending calendar write, approving
  it through Web or Telegram, and executing the stored tool args.

## Test Plan

Core tests:

- Create pending request with exact tool args.
- Created confirmation is visible from a separate database context before the live turn starts
  waiting.
- Approval by target user transitions `pending -> approved` and inserts the deterministic execution
  task in one transaction.
- Approved execution task is visible to a separate worker context immediately after approval
  returns.
- Reject by target user.
- Deny approval by a different user.
- Expire pending request.
- Replayed approval does not enqueue a second task.
- Simulated enqueue failure rolls the approval back, leaving the request pending.
- Policy change between creation and execution fails closed.
- Tool descriptor/schema change between creation and execution fails closed.
- Queued execution of an already-approved invocation does not prompt for confirmation a second time.
- Queued execution fails closed if the stored approval digest does not match the tool name or args
  being executed.
- Task handler exits without execution if the request is no longer `approved`.
- Task handler failure before `approved -> executing` is retryable by the task queue.
- `approved -> executing` is committed before the wrapped tool starts, observable from a separate
  database context.
- Task handler failure after `approved -> executing` records `failed` without duplicating a
  non-idempotent side effect.
- Stale `executing` cleanup moves an old expired-lease request to `failed` with an explicit
  timeout/crash reason.
- Stale `executing` cleanup does not fail an active request whose worker heartbeats/extends the
  lease.
- A stale worker cannot overwrite a cleanup decision because terminal writes are guarded by
  `execution_token`.
- Run the transaction visibility tests on both SQLite and PostgreSQL; SQLite must not depend on a
  nested context sharing the outer conversational transaction.

Web tests:

- Existing in-context confirmation still appears during chat streaming.
- Approval enqueues the durable execution task and the live stream resumes from the recorded task
  result when the same process is still waiting.
- Wrong authenticated Web user cannot approve.

Telegram tests:

- Existing confirmation buttons still approve/reject requests and approval runs the queued execution
  path.
- Callback id alone is insufficient without matching Telegram user mapping.

Email/deferred tests:

- Email-mapped user can create a pending confirmation request.
- Approval through another interface executes the stored tool invocation through the queued task.
- The LLM is mocked; database, confirmation service, and fake tool provider are real or fake rather
  than heavily mocked.

## Open Questions

- Which interfaces should receive proactive notifications for pending requests, and how should the
  user configure preferred confirmation routes?
- Should we store a policy snapshot at request creation or always re-evaluate current policy at
  execution time? V1 should re-evaluate and fail closed.
- How much source text should be shown in confirmation prompts for tainted email content?

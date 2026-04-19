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
- Preserve the current Web and Telegram confirmation UX when the original process is still alive.
- Support cross-interface approval: a request belongs to a user, and any authorized interface for
  that user may surface or resolve it.
- Store and execute the exact confirmed tool name and arguments, not an LLM-generated summary of
  intent.
- Use the existing task queue for durable execution after approval.
- Keep v1 small enough to implement safely.

## Non-Goals

- Do not introduce a general "planned action" abstraction for email.
- Do not treat LLM summaries as trusted or sanitized.
- Do not attempt full conversation suspension and resumption in v1.
- Do not build a second task-state machine inside confirmations.
- Do not add schema-version, descriptor-digest, execution-lease, or per-confirmation retry machinery
  unless implementation uncovers a concrete need.

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
- Where did the request come from?
- Has the request been approved, rejected, or expired?
- Which queued task will execute it after approval?

Execution state belongs to the existing `tasks` table. Message context belongs to `message_history`.
The confirmation table should not duplicate either of those systems.

## Data Model

Add a `confirmation_requests` table.

Required v1 fields:

- `id`: stable request id, suitable for URLs and callback payloads.
- `target_user_id`: authenticated user who may approve or reject the request.
- `status`: `pending`, `approved`, `rejected`, or `expired`.
- `tool_name`: exact tool to execute.
- `tool_args_json`: exact JSON arguments to execute after approval.
- `tool_call_id`: LLM tool call id when available, used only to route the result back to a live
  waiter.
- `source_message_internal_id`: nullable reference to `message_history.internal_id` for the user,
  email, or assistant message that caused the confirmation.
- `confirmation_prompt`: rendered human-readable prompt shown to the user.
- `expires_at`.
- `created_at`, `updated_at`.
- `resolved_at`.
- `resolved_by_user_id`.
- `resolved_via_interface`.
- `execution_task_id`: deterministic task id for the queued execution, such as
  `confirmation_tool_execution:{id}`.

The `source_message_internal_id` replaces a pile of duplicated source fields. If the implementation
needs interface type, conversation id, turn id, profile id, tool call metadata, or original content,
it should load the linked `message_history` row. If a source has not been persisted to message
history yet, persist it first or leave the reference null and accept that the request can only use
the fields on the confirmation row.

Fields deliberately not in v1:

- `tool_args_digest`, `tool_schema_version`, and `tool_descriptor_digest`: these defend against rare
  semantic drift between approval and execution. In v1, if the current tool cannot validate the
  stored args, execution should fail and notify the user. Add versioning later only if this becomes
  a real operational problem.
- `execution_idempotency_key`: use `confirmation_request.id` directly as a stable idempotency value
  if a specific tool/downstream API supports one.
- `execution_token`, `execution_generation`, and `execution_lease_expires_at`: these duplicate task
  queue ownership. If stale processing tasks are a problem, fix the task queue centrally rather than
  adding a parallel lease model to confirmations.
- `metadata_json`: avoid a vague grab bag in v1. Put source context in `message_history` and put
  user-visible approval text in `confirmation_prompt`.

Constraints:

- Only `pending` requests may transition to `approved`, `rejected`, or `expired`.
- Approval and rejection must be idempotent from the user's perspective. Replaying an approval for
  an already resolved request must not enqueue a second execution task.
- The approving identity must match `target_user_id`.

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
        source_message_internal_id: int | None,
        confirmation_prompt: str,
        expires_at: datetime,
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
transaction that:

1. Authenticates the approving user as `target_user_id`.
2. Moves `pending -> approved` with an expiry check.
3. Enqueues one `confirmation_tool_execution:{request_id}` task in the existing task queue.
4. Stores that task id on the confirmation row.

If the transaction rolls back, neither the approval nor the task enqueue is visible. If approval is
replayed after the request is already resolved, the service must not enqueue a second execution
task. The unique `tasks.task_id` constraint is the backstop for that guarantee.

The service should own its write transactions through a context factory. It must not rely on the
long-lived `ToolExecutionContext.db_context` from the active conversational turn for durable
confirmation rows or execution enqueueing.

## Queue-Backed Execution

The reliable execution boundary is the existing database task queue.

For every confirmation that can cause a tool side effect:

1. Creating the confirmation only creates a `pending` request.
2. Accepting the confirmation authorizes the user, moves `pending -> approved`, and enqueues exactly
   one task in the same database transaction.
3. Tool execution happens only in the task worker handler for `confirmation_tool_execution`.
4. Interface handlers may wait for the handler to deliver a live result or later notification, but
   they do not call the wrapped tool directly after approval.

This uses queue properties that already exist in the application:

- `tasks.task_id` is unique, so duplicate approval attempts cannot create duplicate execution work.
- Enqueueing through the same `DatabaseContext` transaction gives atomic handoff: approval and
  queued work become visible together or not at all.
- The worker claims work through the queue's atomic dequeue path.
- Worker wakeup is an optimization. Even if the wakeup event is missed, the durable task remains
  visible to polling workers.

If the current task queue has a gap around stale `processing` tasks, fix that as a generic task
queue issue. Durable confirmations should not introduce their own lease/heartbeat system.

The task payload should be small:

```json
{
  "confirmation_request_id": "confirm_..."
}
```

The payload should not carry authority facts such as `approving_user_id`; the confirmation row is
the source of truth for `target_user_id`, `resolved_by_user_id`, `resolved_at`, and
`resolved_via_interface`.

## Transaction Boundaries And Visibility

Durable confirmations must not be written through a transaction that stays open for the whole
conversation turn.

Today, `DatabaseContext` starts a transaction when entered and commits only when it exits. Current
chat processing commonly passes one `db_context` through trigger persistence, context gathering, LLM
streaming, tool execution, confirmation waiting, and generated-message persistence. If a durable
confirmation row or execution task is inserted through that ambient transaction, other interfaces
and task workers cannot see it until the whole turn exits.

The confirmation implementation needs explicit short transactions:

- Creating a confirmation request commits before the interface emits the confirmation event or
  starts waiting.
- Rejection commits before notifying waiters.
- Approval commits `pending -> approved` and the task enqueue before any waiter expects worker
  execution.
- The task worker uses the normal task queue claim and task status transitions.

The narrow implementation path is to give `ConfirmationService` a DB context factory and have the
confirmation callback call that service outside the ambient turn transaction. A larger refactor to
phase-scoped conversational transactions may still be useful, but it is not required just to make
durable confirmations visible.

SQLite tests should not rely on nested contexts sharing an outer transaction. PostgreSQL tests
should prove the created confirmation and approval task are visible from a separate context before
the live turn completes.

## Compatibility With Existing Sync Confirmations

Existing Web and Telegram confirmations behave like synchronous tool calls from the assistant loop's
point of view: the tool asks for approval, the current turn waits, and if the user approves the tool
result is returned to the same assistant loop so the assistant can continue naturally.

Durable confirmations should preserve that behavior when the original process is still alive:

1. The assistant calls a confirm-required tool.
2. The callback creates and commits a durable confirmation request.
3. The interface shows the confirmation in-context.
4. The current process waits in memory for that request id and `tool_call_id`.
5. Approval enqueues `confirmation_tool_execution:{request_id}`.
6. The task worker executes the stored `tool_name` and `tool_args_json`.
7. If the original process is still alive, the waiter receives the tool result and returns it to the
   assistant loop as the result for the original `tool_call_id`.

The in-memory waiter is only a bridge back to the live conversation. It is not the durable state and
it is not the executor.

If the process restarts while a live turn is waiting, v1 does not resume the original conversation
automatically:

- If the request is still `pending`, it remains pending until `expires_at`.
- If the user rejects it, the durable row records `rejected` and no tool executes.
- If the user approves it, the queued task still executes the exact stored invocation.
- If no live waiter exists when execution finishes, the task handler sends or exposes a
  deterministic notification/result for the target user instead of trying to continue a vanished
  assistant turn.

This is intentionally degraded but deterministic. Full conversation resumption can be added later
using message history and `tool_call_id`.

## Deferred Execution Handler

Add a task type such as `confirmation_tool_execution`.

The handler:

01. Loads the confirmation request.
02. Exits without executing if the request is not `approved`.
03. Loads the linked `message_history` row when `source_message_internal_id` is present.
04. Reconstructs a normal `ToolExecutionContext` from the linked message context where available.
05. Re-evaluates current tool policy.
06. If current policy denies the tool, fails the task and notifies the user.
07. If current policy still requires confirmation, treats the approved confirmation row as
    satisfying that confirmation for this one stored tool invocation.
08. Executes `tool_name` with `tool_args_json`.
09. Delivers the tool result to any live waiter before the task completes.
10. If no waiter exists, sends or exposes a deterministic notification/result for the target user.

The handler should not ask the LLM to reinterpret the original email or message. Approval executes
the stored tool name and args directly.

Tool drift is handled pragmatically in v1: if the current tool cannot validate or execute the stored
args, the task fails and the user is told that the approved action could not be completed. More
elaborate descriptor/version checks can be added later if we see real failures from deploy-time tool
changes.

## Relationship To Tool Policy

For v1, keep the public provider API as close as possible to the current `can_confirm` shape, but
thread enough context through execution to create durable requests for an authenticated user.

For email intake, confirm-required tools should be available only when the system can map the email
to a target user and create durable confirmations for that user. If user mapping is unavailable,
confirm-required tools should be hidden or denied.

Execution-time policy should still be checked. The approved row satisfies a `confirm` decision for
the single stored invocation, but it does not bypass a later `deny` decision.

## Rendering

The confirmation prompt must expose the exact action being approved:

- Tool name or friendly action label.
- Destination or affected object.
- User-visible arguments that matter for authorization.
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
- Confirmation should display exact tool args and source context before execution.
- Approval executes the stored tool invocation, not a planner summary.

## Conversation Resumption

Full conversation resumption is a future enhancement, not part of v1.

V1 distinguishes execution from conversational continuation:

- Execution is durable and queue-backed.
- Same-process continuation is best effort through the in-memory waiter.
- Lost-waiter continuation degrades to deterministic notification or a pending-result surface.

The likely v2 shape:

1. Persist pending tool calls as part of turn state, probably in or adjacent to `message_history`.
2. When a tool request is approved, append the resulting tool message to history.
3. Resume the assistant loop with the stored messages and original profile.
4. Handle multiple pending tool calls from one assistant response deterministically.

V1 should avoid blocking this path, but it should not pre-build it.

## Implementation Milestones

### Milestone 1: Durable Confirmation Core And Queue Handoff

- Add table, migration, repository, and service.
- Add `approve_and_enqueue_execution()` using one database transaction for `pending -> approved` and
  `confirmation_tool_execution:{request_id}` enqueue.
- Add basic list/approve/reject/expire operations.
- Add status transition tests, including wrong-user rejection and replayed approval.

### Milestone 2: Execution Handler

- Add the `confirmation_tool_execution` task handler.
- Execute the stored tool invocation from the approved confirmation row.
- Re-check current policy and fail closed on `deny`.
- Treat the approved row as satisfying `confirm` for that one stored invocation.
- Notify the live waiter when present; otherwise expose or send the deterministic result.

### Milestone 3: Web And Telegram Adapters

- Make Web confirmation creation and resolution go through `ConfirmationService`.
- Make Telegram button confirmations resolve durable requests.
- Preserve existing in-context UX.
- Keep in-memory waiter maps only as process-local bridges from durable request id to the waiting
  coroutine.

### Milestone 4: Email Intake Integration

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
- Task handler exits without execution if the request is not `approved`.
- Policy change to `deny` between creation and execution fails closed.
- Queued execution of an approved invocation does not prompt for confirmation a second time.
- Current-tool validation failure for stored args records task failure and notifies/exposes the
  result.
- Lost sync waiter does not reject or approve the pending request automatically.
- Approving a request after its live waiter is gone executes the stored invocation and records a
  notification/pending-result state instead of trying to resume the vanished turn.
- Rejecting a request after its live waiter is gone records `rejected` without tool execution.
- Run transaction visibility tests on both SQLite and PostgreSQL.

Web tests:

- Existing in-context confirmation still appears during chat streaming.
- Approval enqueues the durable execution task and the live stream resumes from the delivered tool
  result when the same process is still waiting.
- If the stream disconnects or the process restarts before approval, the request remains visible
  until expiry and later approval follows the notification/pending-result fallback.
- Wrong authenticated Web user cannot approve.

Telegram tests:

- Existing confirmation buttons still approve/reject requests and approval runs the queued execution
  path.
- If the bot restarts before the button press, the durable callback path can still approve/reject
  the request by id and user mapping; the original assistant loop is not required to be alive.
- Callback id alone is insufficient without matching Telegram user mapping.

Email/deferred tests:

- Email-mapped user can create a pending confirmation request.
- Approval through another interface executes the stored tool invocation through the queued task.
- The LLM is mocked; database, confirmation service, task queue, and fake tool provider are real or
  fake rather than heavily mocked.

## Future Considerations

- Full conversation resumption after approval.
- Tool descriptor/version checks if delayed approvals across deploys become a real problem.
- A generic task queue stale-processing recovery mechanism if existing task behavior is
  insufficient.
- Tool-specific idempotency integration for downstream APIs that support it.
- A richer result surface if task status plus notification/pending-result UI is not enough.

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
- `execution_started_at`, `execution_finished_at`.
- `execution_result_json`.
- `execution_error`.

Constraints:

- Only `pending` requests may transition to `approved`, `rejected`, or `expired`.
- Only `approved` requests may transition to `executing`.
- Only `executing` requests may transition to `executed` or `failed`.
- Approval and rejection must be idempotent from the user's perspective. Replaying an approval for
  an already resolved request should not execute the tool twice.

## Service API

Introduce a central confirmation service, backed by the repository:

```python
class ConfirmationService:
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

    async def approve(
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

For deferred execution, approval should be a single database transaction that both resolves the
confirmation and enqueues the execution task:

```python
async def approve_and_enqueue_execution(
    *,
    request_id: str,
    approving_user_id: str,
    approving_interface: str,
) -> ConfirmationRequest: ...
```

That method should update `pending -> approved` with a compare-and-set predicate and insert a
`confirmation_tool_execution` task with a deterministic task id such as
`confirmation_tool_execution:{request_id}` in the same transaction. If the transaction rolls back,
neither the approval nor the task enqueue is visible. If an approval is replayed after the request
is already resolved, the service must not enqueue a second execution task.

## V1 Execution Model

V1 keeps existing live-turn behavior while making pending requests durable.

### Live Web Or Telegram Turn

1. The assistant calls a confirm-required tool.
2. `PolicyEnforcingToolsProvider` calls the existing confirmation callback.
3. The callback creates a durable confirmation request.
4. The current interface surfaces that request in-context.
5. The processing coroutine waits on an in-memory waiter for that request id.
6. When the user approves or rejects, the durable row transitions from `pending` to `approved` or
   `rejected`, and the waiter is notified.
7. If approved, the provider marks the same request `executing` before calling the wrapped tool.
8. After the wrapped tool returns or raises, the provider records `executed` or `failed` on the same
   request before returning the tool result to the assistant loop.

The durable row is the source of truth. The in-memory waiter is only an optimization for the live
conversation that created the request.

The implementation should let the confirmation callback return or otherwise expose the confirmation
request id to the provider. Keeping a boolean-only internal interface would make it too easy to
approve a live request without recording its terminal execution state. A compatibility adapter can
preserve existing call sites while the durable path uses a richer result internally.

If the process restarts while a live turn is waiting, v1 does not need to resume the original
conversation automatically. The durable request remains pending and can be resolved later by the
deferred execution path.

There is one additional crash window: the user can approve a live request, moving it to `approved`,
and then the process can die before the live waiter marks it `executing`. A periodic recovery task
should find `approved` requests older than a short configured timeout and enqueue the same
`confirmation_tool_execution:{request_id}` deferred task. The deferred worker will then perform the
normal `approved -> executing -> executed/failed` transition. If the live waiter already won the
transition, the deferred task exits without executing.

### Async Source Or No Live Waiter

For email intake and other async sources:

1. The assistant or worker reaches a confirm-required tool call.
2. The callback creates a durable confirmation request.
3. The worker stops or returns a "waiting for confirmation" result.
4. Web and Telegram can list or notify the target user about the pending request.
5. Approval starts deferred execution of the stored tool invocation.
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
2. In one database transaction, moves `pending -> approved`.
3. In that same transaction, enqueues a `confirmation_tool_execution` task using the existing task
   queue.
4. Uses a deterministic task id derived from the confirmation request id so duplicate approval
   attempts cannot create multiple queued executions.

The handler:

1. Loads the request.
2. Atomically moves `approved -> executing`; if that transition does not apply, it exits without
   executing the tool.
3. Reconstructs a `ToolExecutionContext` from stored source fields and processing profile.
4. Executes the stored tool through the same policy-enforcing provider.
5. Records success or failure.
6. Sends a deterministic notification to the target user if a notification route is configured.

This gives exactly-once execution handoff from confirmation acceptance to the task worker: approval
and task enqueue commit atomically, duplicate approvals cannot enqueue duplicate work, and duplicate
worker attempts must win the `approved -> executing` transition before executing. For tools with
non-idempotent external side effects, no queue can make a process crash after the external side
effect but before recording success perfectly safe; tool implementations should pass stable
idempotency keys where downstream APIs support them.

Policy must still be enforced at execution time. If policy has changed since request creation, the
execution should fail closed unless the implementation explicitly stores and validates a policy
snapshot. V1 should fail closed, record the policy-denial reason, and notify the user that the
approved action could not be executed because the current tool policy no longer allows it.

The service also needs a stale-execution cleanup path. If a worker crashes after moving a request to
`executing`, a periodic cleanup task should identify requests whose `execution_started_at` is older
than a configured timeout and transition them to `failed` with an explicit timeout/crash reason.
This prevents a confirmation from being orphaned in `executing` forever and gives the user a clear
failure state.

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

### Milestone 1: Durable Confirmation Core

- Add table, migration, repository, and service.
- Add status transition tests, including wrong-user rejection and idempotency.
- Add expiry cleanup logic.

### Milestone 2: Web Adapter

- Make Web confirmation creation and resolution go through `ConfirmationService`.
- Preserve existing SSE events and current user experience.
- Keep the in-memory waiter map only as a process-local bridge from durable request id to the
  waiting coroutine.

### Milestone 3: Telegram Adapter

- Make Telegram button confirmations resolve durable requests.
- Validate Telegram chat/user mapping before approval.
- Keep existing button UX.

### Milestone 4: Deferred Execution

- Add `confirmation_tool_execution` task.
- Execute stored tool invocations after approval when no live waiter handles the request.
- Record execution result and send deterministic user notification.

### Milestone 5: Email Intake Integration

- Replace email action proposal planning with normal tool calls under an email intake profile.
- Allow confirm-required tools only when inbound email maps to a user and durable confirmations are
  available.
- Add functional tests for forwarding a booking email, creating a pending calendar write, approving
  it through Web or Telegram, and executing the stored tool args.

## Test Plan

Core tests:

- Create pending request with exact tool args.
- Approve by target user.
- Reject by target user.
- Deny approval by a different user.
- Expire pending request.
- Replayed approval does not execute twice.
- Policy change between creation and execution fails closed.
- Stale `approved` cleanup enqueues deferred execution and does not duplicate live execution.
- Stale `executing` cleanup moves an old request to `failed` with an explicit timeout/crash reason.

Web tests:

- Existing in-context confirmation still appears during chat streaming.
- Approval moves the durable row through `approved`, `executing`, and `executed` or `failed` while
  resuming waiting tool execution.
- Wrong authenticated Web user cannot approve.

Telegram tests:

- Existing confirmation buttons still approve/reject live tool execution.
- Callback id alone is insufficient without matching Telegram user mapping.

Email/deferred tests:

- Email-mapped user can create a pending confirmation request.
- Approval through another interface executes the stored tool invocation.
- The LLM is mocked; database, confirmation service, and fake tool provider are real or fake rather
  than heavily mocked.

## Open Questions

- Which interfaces should receive proactive notifications for pending requests, and how should the
  user configure preferred confirmation routes?
- Should we store a policy snapshot at request creation or always re-evaluate current policy at
  execution time? V1 should re-evaluate and fail closed.
- How much source text should be shown in confirmation prompts for tainted email content?

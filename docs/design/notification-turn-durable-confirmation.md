# Durable Confirmations for Notification Turns

## Status

Approved (scope and mechanism confirmed with the user).

## Problem

The assistant cannot call confirm-gated tools on certain task-worker turns because those turns are
invoked with `request_confirmation_callback=None`. In the LLM loop,
`can_confirm = request_confirmation_callback is not None` (`processing/llm_loop.py`), and when
`can_confirm` is `False` the policy engine downgrades every `CONFIRM` decision to `DENY`
(`tools/policy.py::_apply_confirmation_capability`). So the tool is neither advertised nor
executable.

This is most visible when a delegated task completes and the task worker wakes the *source* profile
to notify it of the result (`task_worker._wake_source_profile_for_delegation`): the wakeup turn was
passed `request_confirmation_callback=None`, so the notified conversation could not request approval
for anything.

Durable confirmations already exist precisely for non-interactive contexts: they record a pending
tool invocation, notify the user on their primary channel, and execute later via the
`confirmation_tool_execution` task once approved (see
[durable-tool-confirmations.md](durable-tool-confirmations.md)). Automation scripts already use this
via `build_script_confirmation_callback`. There is no reason a *notified conversation* should be
more restricted than a script.

## Goals

- Let the assistant request (durable) confirmation on notification-style task-worker turns:
  - delegated-task completion notifications, and
  - scheduled callbacks / reminders (and script `wake_llm`).
- Use the **deferred / non-blocking** durable mechanism: record the request, notify the owner, and
  return a "pending approval" result so the turn finishes instead of holding a worker until the user
  decides.

## Non-Goals

- No change to the *synchronous* delegated-run path, which already gets an interactive durable
  confirmation callback (`_build_delegation_confirmation_callback`).
- No change to the inline web/Telegram live-turn confirmation UX.

## Mechanism

Introduce a single shared builder,
`services.deferred_tool_confirmation.build_deferred_confirmation_callback`, that returns a
`RequestConfirmationCallback` which defers each confirm-gated call to
`create_deferred_tool_confirmation`, addressed to a known owner user. When no owner is known the
callback reports the tool as not run (it cannot be approved), mirroring the existing
legacy-automation behavior.

`build_script_confirmation_callback` is refactored to delegate to this builder.

### Delegated-task completion

`_wake_source_profile_for_delegation` passes
`build_deferred_confirmation_callback(target_user_id=run["user_id"], ...)` instead of `None`. The
source user owns the resulting durable confirmation.

### Scheduled callbacks / reminders

`llm_callback` payloads gain an optional `created_by_user_id`, populated at the construction sites
where the owner is known (the `schedule_reminder` / `schedule_future_callback` tools, script
`wake_llm`, automation scheduling, and follow-up reminders carry it forward). `handle_llm_callback`
builds the deferred callback from `payload.get("created_by_user_id")`; legacy payloads without an
owner degrade gracefully (confirm-gated tools report they cannot be approved).

### Result delivery for source-less confirmations

Deferred confirmations carry no `source_message_internal_id` (see above), so after approval the
result notification cannot thread back via a source message. The execution context already falls
back to the request's recorded `origin_interface_type` / `origin_conversation_id`, and
`_resolve_confirmation_result_delivery` now does the same: when there is no source row it delivers
the result to the recorded origin conversation on any interface, falling back to the user's primary
Telegram chat only when no origin (or its interface) is available. This also improves the
pre-existing automation-script path, which previously could only deliver source-less results to
Telegram.

## Compatibility

- Turns that previously hard-denied confirm-gated tools now advertise them; calling one with no
  resolvable owner returns a "not run / cannot be approved" result rather than a silent denial.
- Already-queued legacy `llm_callback` tasks have no `created_by_user_id` and degrade gracefully.

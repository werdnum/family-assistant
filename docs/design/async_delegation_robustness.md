# Async Profile Delegation — Robustness Fixes

## Status

Implemented. Addresses the findings from the high-effort code review of
`codex/implement-async-profile-delegation` (commits `4809f1333`, `5d2c8df09`).

## Background

`delegate_to_service` hands a request to another processing profile. The run is persisted in
`delegation_runs`, enqueued as a `delegated_profile_run` task, and executed by the `TaskWorker`. The
caller waits inline for up to `handoff_after_seconds`; if the run is not terminal by then it "hands
off" and returns an async reference, and the worker notifies the conversation when the run finishes.

## Root cause shared by the correctness findings

`handle_delegated_profile_run` executes inside the worker's single long-lived `process_context`
transaction (`task_worker.py`, opened at the worker loop and committed only on handler return),
under the generic `asyncio.wait_for(handler, timeout=TASK_HANDLER_TIMEOUT=300s)`. Every state
transition (`mark_running` → `mark_completed`/`mark_failed` → notify + `mark_notified`) is written
into that one uncommitted transaction. Meanwhile the caller observes the run through *isolated*
contexts against a wall-clock deadline. Consequences:

- **Stranded `running` (C4).** At 300s the handler is cancelled and `_process_task` catches the
  `TimeoutError` *inside* the transaction, so it commits `mark_running`. The run is stuck `running`
  forever with no notification, and there is no reaper.
- **Retry duplicates side effects (C6).** `max_retries_override=1`; after a timeout/crash the run is
  non-terminal, the terminal guard at the top of the handler does not stop re-execution, so the
  entire delegated turn runs again (duplicate messages, calendar writes, confirmations).
- **PostgreSQL handoff blocks (C2).** `mark_running`'s row lock is held for the whole run; the
  caller's `mark_handed_off` (isolated) blocks behind it, so the "async" handoff hangs synchronously
  until the run commits.
- **Handoff/notify race drops the result (C1).** The worker skips notification using a
  `completed_at < handoff_after_at` time heuristic observed inside the uncommitted transaction,
  while the caller marks handed off after its deadline with no recheck — the completed result is
  stranded; the mirror window double-delivers.
- **Notification lost on retry (C3).** `mark_completed` commits; the notification send then fails;
  the exception is caught inside the transaction so the terminal status commits with the retry; the
  retry sees a terminal status and returns without ever re-attempting the notification.

## Design

### 1. Commit state transitions independently of the worker transaction

The delegated run's own bookkeeping (`mark_running`, `mark_completed`/`mark_failed`, notification +
`mark_notified`) is committed through `create_isolated_context()` — the same mechanism `create_run`
already uses — so transitions are durable and immediately visible to observers, and no row lock is
held across the long LLM turn. Only `target_service.handle_chat_interaction` keeps using the
worker's `db_context` for its own message-history writes.

This directly fixes C2 (no lock held across the run) and is the substrate for C3/C4/C6.

### 2. Entry guards make the handler at-most-once and notification-complete

On entry, `handle_delegated_profile_run` loads the run and:

- **terminal** → if `notified_at` is `None`, (re)attempt the terminal notification, then return;
  otherwise return. Fixes C3 (a notification whose first delivery failed is retried).
- **`running`** (a previous attempt started but did not finish — crash or 300s timeout) → mark the
  run failed with an "interrupted" error and notify; do **not** re-execute. Fixes C6: the expensive
  delegated turn, which has non-idempotent external side effects, is at-most-once.
- **`queued`** → proceed: `mark_running` (isolated commit), run the interaction, mark terminal
  (isolated commit), notify.

`asyncio.CancelledError` from the 300s handler timeout is intentionally **not** caught (doing async
DB work in a cancellation handler is fragile): it propagates so the task is retried, and the retry's
`running` entry guard fails the stranded run and notifies. The stale-run reaper (below) is the
backstop when no retry occurs. Together these close C4.

### 3. Notification keyed on an atomic handoff claim, not a time heuristic

The notify decision becomes: **the worker notifies iff the caller handed off.** If the caller is
still waiting inline (`handed_off_at IS NULL`), it will deliver the result itself; the worker must
not also notify.

The caller and worker synchronize on the run row:

- **Caller**, at its deadline, runs `mark_handed_off` conditioned on
  `handed_off_at IS NULL AND status NOT IN (terminal)`. If it updates a row, it returns the async
  reference (the worker will notify). If it updates nothing (the run went terminal first), it
  reloads and delivers the result inline.
- **Worker**, after marking terminal, reloads and notifies iff `handed_off_at IS NOT NULL`.

The two paths serialize on the row, so exactly one of {inline delivery, async notification} happens.
This removes the `completed_at < handoff_after_at` heuristic and the `handoff_after_at` column
entirely. Fixes C1 (no drop, no double-delivery).

### 4. Stale-run reaper (defense in depth, C21)

A reaper mirrors `worker_tasks.mark_stale_tasks`: delegation runs left `running` (or `queued`)
beyond a threshold are marked failed and notified. This guarantees no run is stranded even if a
process is killed before the `running` entry guard runs on retry.

### 5. Web confirmations for background runs (C7)

Only a `"telegram"` entry is registered in `confirmation_ui_managers`, so
`_build_delegation_confirmation_callback` returns `None` for web runs and confirmation-gated tools
fail. A new `WebConfirmationUIManager` implements the `ConfirmationUIManager` protocol by reusing
the exact durable web-confirmation flow currently inlined in `web/turn_producer.py`
(`confirmation_service.create_request` → `confirmation_result_waiters.register` →
`web_confirmation_manager.request_confirmation` → optional `stream_hub` publish →
`wait_for_confirmation_resolution`). It is registered under `"web"` near the telegram registration.
`turn_producer`'s `web_confirmation_callback` is refactored to delegate to the same manager so there
is a single web-confirmation implementation. No special-casing is added to the delegation handler —
once `"web"` is registered, the existing callback factory works for web.

### 6. Attachment visibility on PostgreSQL (C8)

`delegate_to_service` validates attachments against the caller's *uncommitted* turn transaction but
enqueues the task via an isolated commit, so on PostgreSQL the worker can start before the turn
commits and fail to resolve attachments registered earlier in the same turn. Fix: the delegated
run's attachment references are carried in `content_parts_json` (already committed with the run row
via the isolated create), and resolution/validation is performed against committed state. Where the
worker resolves result attachments it uses its own committed context.

## Cleanups bundled in

- **C10** whitespace-only error → guard `splitlines()[-1]` against empty.
- **C11** surface the inline failure detail instead of a generic message; advertise retrievability.
- **C12/C25** stop string-keyed `getattr` on typed config fields; read attributes directly so the
  type checker covers the security-relevant `allowed_delegation_sources` check and config defaults
  live in one place.
- **C13** drop dead columns `source_tool_call_id`, `attachment_ids_json`, and the now-unused
  `handoff_after_at` (migration edited in place — new on this branch, unshipped).
- **C14** trim `DelegationRunStatus` to the states actually written; one terminal-status set.
- **C15** flip the conftest `register_delegation_handler` default / register by default.
- **C16** single shared web-confirmation implementation (see §5).
- **C17** _kept deliberately._ The web completion notification keeps its explicit add_message + push
  \+ hub-tickle path rather than calling `WebChatInterface.send_message`: the notification is written
  from the TaskWorker inside an isolated transaction, while `WebChatInterface.send_message` opens
  its own database context, so the swap the review suggested is not safe. The notification now runs
  inside one isolated context for durability.
- **C18** state transitions return their updated row (RETURNING) instead of update-then-refetch;
  remove the duplicated fail path.
- **C19** index set trimmed to those actually queried plus a `(conversation_id, created_at)`
  composite for `list_for_conversation`.
- **C20** batch the delegated-attachment metadata lookups instead of N+1 sequential awaits.
- **C22** the delegation-runs JSON normalizers were simplified (list-first); the broader
  cross-module extraction of one shared JSON-list helper (notes / worker_tasks / scripts /
  delegation) is left out of scope here as it touches unrelated modules.
- **C23** `_now` uses the `exec_context.clock or SystemClock()` idiom.
- **C24** collapse the triplicated subconversation filter in `message_history` into one helper.

## Testing

Extend `tests/functional/automations/test_async_delegation.py` and add cases for: handoff/notify
single-delivery, terminal-on-entry re-notification, `running`-on-entry interruption (no
re-execution), web confirmation via the new manager, and the reaper. Run on both SQLite and
PostgreSQL via `db_engine`.

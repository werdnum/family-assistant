# Multi Task-Worker Pool

## Status

Implemented.

## Motivation

The app historically started exactly **one** `TaskWorker`, whose loop dequeues and processes **one**
task at a time, sequentially. That single slot deadlocks any handler that, while occupying the
worker, parks waiting on an **in-process future** that can only be resolved by **another** queued
task.

The concrete case is async profile delegation. `handle_delegated_profile_run` can request a
confirmation-gated tool and then wait on a confirmation. Approving that confirmation enqueues a
separate `confirmation_tool_execution` task. With a single worker, that second task can never run —
the only worker is blocked waiting for the confirmation — so the delegated run is stuck until it is
killed by the handler timeout. `docs/design/async_delegation_robustness.md` ("Known constraints")
names this directly: the real fix is "to run delegated turns off the single task-handler slot."

A pool of in-process workers fixes it: while worker A is parked on the future, worker B services the
queue (including the task that resolves the future), unblocking A.

## In-process only

Workers must run **in the same process and event loop**. They coordinate confirmations through
shared in-memory state — the `ConfirmationResultWaiterRegistry` and the module-global
`web_confirmation_manager` — and the futures involved cannot cross process boundaries. Cross-process
workers are therefore **not** viable for this mechanism, and are out of scope.

## Design

### Configurable pool size

`AppConfig.task_worker_count` (integer, **default 2**, validated `>= 1`) controls how many workers
start. `Assistant` builds N identically-configured workers from a single `_build_task_worker(...)`
builder so every worker gets the same handler set and shared dependencies (processing service,
confirmation waiters/managers, etc.). The workers are interchangeable: any worker can pick up any
queued task. Their `run()` tasks are tracked in `Assistant.task_worker_tasks` (index-aligned with
`Assistant.task_workers`).

### Dedicated engine per worker

Each worker gets its **own** database engine to the same database (`Assistant._worker_engine`). This
is required for the deadlock fix to actually unblock on SQLite: the worker loop holds a DB
transaction open for the whole handler execution (the handler runs with the worker's `db_context`),
and SQLite's shared `StaticPool` hands out a **single** connection. Without a dedicated engine, a
worker parked inside its transaction would hold that one connection and serialize the entire pool —
exactly the deadlock we are trying to remove. Independent engines give each worker its own
connection, mirroring how separate processes would each have their own connection while keeping the
in-memory registries shared.

In-memory SQLite (`:memory:`) is the one exception: a fresh engine to `:memory:` gets a brand-new
empty database, so it cannot be shared. In that case the single shared engine is reused (in-memory
SQLite is test-only). Dedicated engines are disposed on shutdown.

### Per-worker wake events with fan-out

Previously, workers that called `run()` without an explicit event shared one module-global
`asyncio.Event`; one worker's `event.clear()` after waking swallowed the notification for its
siblings. Now:

- Each worker waits on its **own** `asyncio.Event`, registered with the storage layer
  (`register_worker_wake_event` / `unregister_worker_wake_event`).
- Enqueueing an immediate task calls `notify_workers()`, which fans out to **every** registered
  worker event (plus the legacy global event, for back-compat and tests).
- When a worker **claims** a task it calls `notify_other_workers(its_own_event)` to wake siblings —
  a single dequeue claims only one task, and a sibling that lost a claim race would otherwise wait
  out the poll interval before draining the rest of the queue.
- Each worker **clears** its event at the top of the loop (before the dequeue), not after the wait.
  A notification that arrives during a dequeue that returned nothing therefore survives to the next
  wait instead of being lost.

Tasks were never actually lost before (a 5s poll caught them); these changes fix wake **latency**
under concurrency. Tests that pass an explicit `wake_up_event` to `run()` still work: the event is
registered for fan-out too.

### N-worker health monitoring

`_monitor_task_worker_health` periodically calls `_check_and_restart_workers`, which checks each
worker **individually**. A worker whose `run()` task is done (crashed, exited, or cancelled) is
restarted in place — the same `TaskWorker` instance (same engine, same handlers) is given a fresh
`run()` task at the same pool index — without disturbing healthy siblings. A worker with no activity
beyond `WORKER_INACTIVITY_TIMEOUT` is cancelled and restarted the same way.

### Per-task-type handler timeout overrides

`TaskWorker` accepts `handler_timeout_overrides: dict[task_type -> seconds]`; task types not present
fall back to the instance default (`TASK_HANDLER_TIMEOUT = 300s`). The default overrides
(`DEFAULT_TASK_HANDLER_TIMEOUT_OVERRIDES`) pre-populate `"delegated_profile_run": 600` so a
delegated run that parks on a human confirmation is not cancelled at 300s. Raising this timeout is
safe now that sibling workers keep servicing the queue while one worker is parked. The
`delegated_profile_run` task type is registered on a feature branch and need not exist for the
override to be harmless — the override only applies when a task of that type is actually processed.

## Validation

- New tests in `tests/functional/tasks/test_task_worker_pool.py` run on both SQLite and PostgreSQL
  via the `db_engine` fixture: two workers process tasks concurrently (overlap, not serialized); a
  worker parked on an in-process future is unblocked by a sibling that runs the resolving task (the
  generic deadlock fix); per-task-type timeout override is honoured and the default still applies
  elsewhere; enqueue promptly wakes an idle sibling; the config default is 2 and `< 1` is rejected;
  the health monitor restarts one dead worker among N.
- SQLite concurrency was specifically validated: the dedicated-engine design is what makes a parked
  worker stop blocking its siblings on SQLite's single shared `StaticPool` connection.

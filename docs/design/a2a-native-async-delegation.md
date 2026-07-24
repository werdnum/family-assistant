# Native Async Delegation for the A2A Path

## Status

Implemented (two-sided async; see the addendum). `PollableDelegationService` and its
`DelegationTransientError`/`DelegationPermanentError`/`DelegationTaskNotFoundError` taxonomy
(originally A2A-specific exception classes) were later generalized to a second, local implementer —
see [deep-research-pollable-delegation.md](deep-research-pollable-delegation.md). The design below
is written against the original A2A-only implementation; the mechanism it describes is now
provider-agnostic.

A background send lost to a server restart leaves its `a2a_tasks` row non-terminal; the delegating
client recovers it via its `max_async_seconds` cap (poll → cap → `tasks/cancel`, which marks the row
`canceled` — protected from a late overwrite by `update_task_status`'s terminal-CAS guard). An
age-based server-side reaper was deliberately not added: it cannot distinguish a legitimately
long-running send from a lost one without a heartbeat, and would false-fail the former (permanently,
given the CAS guard).

**Task-id ownership (A2A spec §3.4.2).** The client does **not** pre-generate or send a task id on
the initial `message/send`; per the spec a client-supplied `taskId` must reference an *existing*
task, so a compliant remote rejects an unknown one. Instead the submit carries no task id, the
remote assigns one, and the worker persists the returned id (reconciled via `update_remote_task`).
The run is claimed `awaiting_remote` with a **NULL** `remote_task_id` *before* submitting (so the
retry-guard and wall-clock cap apply); a submit whose response is lost leaves the id NULL and the
next poll re-submits to (re)create the task. The cost is a narrow duplicate-on-recovery window: a
submit that actually created a remote task but whose response was lost will be re-submitted,
creating a second task and orphaning the first. This is accepted in exchange for spec compliance
(and works against any A2A agent, not only the FA server, which previously tolerated client ids by
being idempotent on them).

To keep that window from widening under concurrency, the reaper re-submits a NULL-id run only once
it is past the **submit grace** (`RemoteServiceConfig.timeout_seconds`, the per-HTTP-call timeout):
past it the first submit has definitely returned, so the run is genuinely stuck (its only poll was
lost) rather than mid-submit, and re-submitting cannot race an in-flight submit into a duplicate.
Within the grace a NULL-id run is left to its own enqueued poll (or the durable
`delegated_profile_run` retry). Runs that already carry a remote id are always re-attached (a lost
poll just re-polls; no duplicate). Duplicate poll *loops* for one run (e.g. a re-attaching retry
plus a surviving poll) are harmless — `tasks/get` is idempotent and the terminal transition is
CAS-guarded, so only one finalize/notify wins.

## Background

Asynchronous profile delegation (see
[asynchronous-profile-delegation.md](asynchronous-profile-delegation.md) and
[async_delegation_robustness.md](async_delegation_robustness.md)) lets the assistant hand a request
to another profile, end its turn, and have the result delivered back into the conversation when it
finishes. A2A remote delegation
([a2a-client-profile-delegation.md](a2a-client-profile-delegation.md)) lets a "profile" actually be
a remote agent reached over the A2A protocol.

**These two features already compose — but only coarsely.** The delegation tool
(`delegate_to_service_tool`, `src/family_assistant/tools/services.py`) and the worker
(`handle_delegated_profile_run`, `src/family_assistant/task_worker.py`) are target-agnostic: they
resolve the target from `processing_services_registry` and call `handle_chat_interaction`.
`RemoteA2AService` is registered in that same registry and implements that method. So, with
`async_delegation_enabled=True` and outside a script, an A2A delegation already:

1. Creates a durable `delegation_run` row,
2. Enqueues a `delegated_profile_run` task,
3. The worker runs `RemoteA2AService.handle_chat_interaction` in the background,
4. The result is posted to the conversation later.

The async *handoff* works. What does not work well is **how** it is async: the worker thread blocks
on the A2A HTTP/SSE connection (`A2AClientWrapper.send_message` → `_poll_until_terminal`) for the
entire remote run.

### Gaps in the current (coarse) behaviour

| #   | Gap                                                            | Cause                                                                                                                                                                                                                                                                                                                                     |
| --- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | A worker concurrency slot is held for the full remote duration | `send_message` blocks until the remote task is terminal; the worker `await`s it                                                                                                                                                                                                                                                           |
| 2   | Hard timeout ceiling (~300 s)                                  | `A2AClientWrapper` timeout = `RemoteServiceConfig.timeout_seconds` (default 300 s); handler timeout is 600 s. Remote tasks longer than the client timeout fail even though A2A natively supports long-running tasks via `tasks/get`/`tasks/resubscribe`                                                                                   |
| 3   | Worker crash orphans remote work                               | The remote A2A `task_id`/`context_id` is never persisted. On restart the "running" entry-guard *fails* the FA run without retry (to avoid duplicate side effects) while the remote agent keeps running invisibly. No way to re-attach                                                                                                     |
| 4   | No cancellation propagation                                    | Reaping/timing-out the FA run never calls `tasks/cancel` on the remote → orphaned remote compute. (Note: the FA A2A *server* already implements `tasks/cancel` — `a2a_api.py:_handle_cancel_task` + `app.state.a2a_cancel_events` — and the SDK client exposes `cancel_task`; only the delegation layer never invokes it.)                |
| 5   | `input_required` is a dead end                                 | A2A multi-turn `input_required` converts to an error; no path to surface the question and continue                                                                                                                                                                                                                                        |
| 6   | No async-delegation tests over A2A                             | The loopback integration test (`tests/functional/web/api/test_a2a_client_integration.py`) covers the *synchronous transport* round-trip (`A2AClientWrapper → FA A2A server → ProcessingService`, in-process via `ASGITransport`) but **not** the async delegation handoff (durable run → worker → conversation post) over a remote target |

## Goals

- Stop blocking a worker slot for the lifetime of a remote A2A run.
- Support remote tasks that run longer than a single HTTP timeout.
- Survive worker/process restarts by re-attaching to the in-flight remote task.
- Propagate cancellation to the remote agent when the FA run is abandoned.
- Full end-to-end test coverage against a fake A2A agent.

### Non-goals

- Multi-turn `input_required` continuation (gap #5). Tracked separately; this design keeps the
  current behaviour (terminal error) but leaves room for it (see *Future work*).
- Changing local (in-process) delegation, which has no network task to poll.
- Streaming partial remote output into the conversation.

## Design

The core change: for A2A targets, **submit-then-poll** instead of **block-until-done**. We split the
single blocking worker call into (a) a short submit that returns the remote `task_id`, and (b) a
lightweight, self-rescheduling poll task that advances the run to terminal without holding a worker.

Local targets are unchanged — they keep the existing single-shot `handle_delegated_profile_run`
path, because there is no remote task to poll.

### Service capability marker

`RemoteA2AService.kind == "remote"`; local services are `ProcessingService` (kind `"local"`). The
delegation tool and worker branch on this marker (not `isinstance`, to keep the protocol seam) to
decide between the submit-then-poll path and the existing blocking path. We add a narrow protocol
method so the worker never imports `RemoteA2AService` directly:

```python
class PollableDelegationService(Protocol):
    async def submit_async(self, ...) -> RemoteSubmission: ...      # returns remote task_id/context_id
    async def poll_async(self, remote_task_id, remote_context_id) -> ChatInteractionResult | Pending: ...
    async def cancel_async(self, remote_task_id, remote_context_id) -> None: ...
```

`RemoteA2AService` implements it; `ProcessingService` does not (the worker checks for the capability
and falls back to the current path).

### Schema changes

Add three nullable columns to `delegation_runs` (Alembic migration, autogenerate then hand-check;
SQLite + PostgreSQL):

- `remote_task_id TEXT NULL` — the A2A task id returned by the remote agent on submit.
- `remote_context_id TEXT NULL` — the A2A context id (we already compute one; persist it).
- `poll_attempts INTEGER NOT NULL DEFAULT 0` — for backoff and a safety cap.

Add a new status to `DelegationRunStatus`: **`awaiting_remote`** — submitted to the remote agent,
not yet terminal, being polled. This is deliberately distinct from `running` so the
fail-without-retry "running" entry-guard does **not** apply to it (re-attach is safe and desired,
unlike re-executing a half-done local turn). `awaiting_remote` is **non-terminal**, so existing
`mark_handed_off` / `find_terminal_unnotified` / `reap_stale` predicates treat it correctly, with
one change: the stale reaper must cancel the remote before failing an `awaiting_remote` run (see
*Reaper*).

New repository methods on `DelegationRunsRepository`:

- `mark_awaiting_remote(delegation_id, remote_task_id, remote_context_id, now)` — `queued` →
  `awaiting_remote`, conditioned on `status == "queued"` (mirrors `mark_running`'s guard).
- `bump_poll_attempt(delegation_id, now)` → returns new count.
- `list_awaiting_remote(limit)` — for the sweep backstop.

### New task type: `delegated_profile_run` (submit) + `delegation_poll`

**Submit (reuse `delegated_profile_run`).** The existing handler grows a branch: if the target is
`Pollable`, call `mark_awaiting_remote` (NULL `remote_task_id` — see *Task-id ownership* in the
status note), `submit_async` (no client task id), persist the remote-assigned
`remote_task_id`/`remote_context_id` via `update_remote_task`, enqueue the first `delegation_poll`
task scheduled `+poll_interval`, and **return** (releasing the worker). A poll that finds a NULL id
(submit response lost) or a not-found task re-submits to (re)create the task and reconcile the new
id. If the target is local, the current blocking path runs unchanged.

**Poll (new `delegation_poll` task).** Per-run, self-rescheduling:

```
handle_delegation_poll(payload={delegation_id}):
    run = get_by_delegation_id(delegation_id)
    if run is None or run.status in TERMINAL: return        # already done / cancelled
    if run.status != "awaiting_remote": return              # defensive
    target = registry.get(run.target_service_id)
    result = await target.poll_async(run.remote_task_id, run.remote_context_id)
    if result is Pending:
        attempts = bump_poll_attempt(delegation_id)
        if attempts > MAX_POLL_ATTEMPTS: fail + cancel_async + notify; return
        enqueue delegation_poll (+backoff(attempts))        # reschedule, release worker
        return
    # terminal
    mark_completed/mark_failed(...)
    notify_delegation_if_needed(...)
```

`poll_async` does exactly one `tasks/get` (no internal blocking loop) and maps the A2A state via the
existing `a2a_task_to_chat_result`, returning a `Pending` sentinel for non-terminal states. Each
poll holds a worker slot for one quick HTTP round-trip, not the whole remote run (fixes gap #1).
There is no client-side timeout ceiling on total remote duration — only a `MAX_POLL_ATTEMPTS` /
wall-clock cap we control (fixes gap #2).

**Why per-run self-rescheduling rather than one recurring sweep:** it reuses the existing durable
task queue (scheduling, retries, recovery) with no global scan, and each run's cadence is
independent. A recurring `delegation_run_cleanup`-style sweep remains as the **backstop** only (see
*Reaper*), catching runs whose poll task was somehow lost.

### Error classification (submit & poll)

The client (`A2AClientWrapper._call_jsonrpc`) sorts every failure into three buckets so the worker
knows whether to fail fast, keep polling, or re-submit:

- **`A2AClientError` (transient)** — transport errors (timeout, connection reset) and HTTP 5xx. The
  request may have reached the remote (response lost) or the remote may recover. On submit the run
  stays `awaiting_remote` and a poll is scheduled; on poll it reschedules. The wall-clock cap bounds
  the retry window.
- **`A2APermanentError` (deterministic)** — HTTP 4xx (bad auth / bad request) and non-recoverable
  JSON-RPC errors. Retrying will not help, so the worker fails the run immediately with the real
  error rather than waiting out the cap.
- **`A2ATaskNotFoundError` (a subclass of `A2APermanentError`)** — HTTP 404 or the A2A
  task-not-found JSON-RPC code (`-32001`). Reachable when the remote lost the task (e.g. a restart).
  Rather than failing, the poll handler **re-submits** (no client task id, so the remote assigns a
  fresh one which is reconciled in) and keeps polling. The same path serves a poll that finds a NULL
  `remote_task_id` (a first submit whose response was lost). This closes the gap where a momentary
  outage *around submit* would otherwise become a permanent failure. Each re-submit bumps the
  poll-attempt counter and reschedules with backoff so a persistently-missing remote paces down
  rather than spinning at the base interval.

Submit and re-submit share one classifier (`_handle_submit_failure`): transient → poll, permanent →
fail fast. The re-submit loop is bounded by the same wall-clock cap as ordinary polling.

### Re-attach on restart (fixes gap #3)

Because the remote-assigned `remote_task_id` is persisted and the run sits in `awaiting_remote` (not
`running`), restart recovery is automatic: the durable `delegation_poll` task is picked up by any
worker and calls `tasks/get` against the stored remote task id. Nothing re-executes the remote side.
A run still at a NULL id (submit response lost before the id was persisted) is recovered by the poll
re-submitting. If the poll task itself was lost, the reaper backstop re-enqueues a poll (not a fail)
for `awaiting_remote` runs that are young, and only fails+cancels ones past the wall-clock cap.

### Cancellation (fixes gap #4)

`cancel_async` calls the SDK `Client.cancel_task`, which the FA A2A server already honours
(`a2a_api.py:_handle_cancel_task` → `a2a_tasks.cancel_task`, plus the per-task
`app.state.a2a_cancel_events` signal that interrupts an in-flight server-side run). The loopback
harness therefore exercises real end-to-end cancellation, not a stub. It is invoked when:

- The reaper fails an `awaiting_remote` run past the cap → cancel remote first, then `mark_failed`.
- (Future) a user-initiated cancel tool transitions the run.

Cancellation is best-effort and idempotent; a failed cancel is logged, not fatal (the remote may
already be terminal).

### Reaper / backstop changes (`handle_delegation_run_cleanup`)

`reap_stale` currently fails `queued`/`running` runs older than `created_before`. Extend the sweep
to also handle `awaiting_remote`:

- `awaiting_remote` **and** younger than the wall-clock cap **and** no live poll task in flight →
  re-enqueue a `delegation_poll` (self-heal lost poll tasks).
- `awaiting_remote` **and** older than the cap → `cancel_async` + `mark_failed` + force-notify.

`running` (local) keeps today's fail-without-retry semantics. This split is the one behavioural
subtlety to get right; it is covered by dedicated tests below.

### Timeout / config

- `RemoteServiceConfig.timeout_seconds` becomes a *per-HTTP-call* timeout (submit and each poll),
  not a total-run ceiling. Add `RemoteServiceConfig.max_async_seconds` (default e.g. 3600) as the
  total wall-clock cap enforced by the poller/reaper.
- Poll cadence: `poll_interval_seconds` (default e.g. 10 s) with light exponential backoff up to a
  max (e.g. 60 s). Configurable on `RemoteServiceConfig`.

## Milestones

Each milestone is independently testable and leaves the tree green.

### M1 — Tests pin current coarse behaviour (no production change)

**Reuse the existing loopback harness** rather than building a fake A2A agent. The `a2a_test_server`
/ `a2a_client_wrapper` / `remote_service` fixtures in
`tests/functional/web/api/test_a2a_client_integration.py` already wire
`A2AClientWrapper → FA's own A2A server (in-process `ASGITransport`) → ProcessingService`. Extend
that harness (or a sibling test module) up the stack: register the `remote_service` in a
`processing_services_registry`, call `delegate_to_service_tool` against it with
`async_delegation_enabled=True`, and prove end-to-end that an A2A delegation today creates a durable
run → the worker executes it → the result is posted to the conversation. This closes gap #6 and
gives a regression net before refactoring. **Deliverable on its own.**

Because the loopback uses a real `ProcessingService` behind the A2A server, a slow/long agent is
produced by scripting the mock LLM (`RuleBasedMockLLMClient`) to delay or to require multiple turns
— no fake transport needed. The same real server backs the `tasks/get` polling and `tasks/cancel`
paths exercised in M3/M4.

### M2 — Schema + capability seam

Migration for the three columns + `awaiting_remote` status; new repo methods
(`mark_awaiting_remote`, `bump_poll_attempt`, `list_awaiting_remote`) with unit tests on both
engines via `db_engine`. Define the `PollableDelegationService` protocol and the `Pending` sentinel.
No behaviour change yet (columns unused). Lint + tests green.

### M3 — Submit-then-poll for A2A

Implement `RemoteA2AService.submit_async` / `poll_async` / `cancel_async` on top of the A2A client
(add a non-blocking submit and a single-shot `get_task`/`cancel_task` to `A2AClientWrapper`). Branch
`handle_delegated_profile_run` on the `Pollable` capability; add the `delegation_poll` handler and
register it (`assistant.py`, mirroring `delegated_profile_run`). Local path untouched. Tests: a fake
remote agent that stays non-terminal for N polls then completes; assert the worker is released
between polls (no slot held), the run reaches `awaiting_remote`, then `completed` + notified.

### M4 — Restart re-attach + reaper/cancellation

Extend `handle_delegation_run_cleanup` for `awaiting_remote` (re-enqueue young, cancel+fail old).
Wire `cancel_async` into the cap path. Tests: kill/restart simulation (drop the poll task, run the
reaper, assert a new poll is enqueued and the run still completes); cap-exceeded path asserts
`cancel_task` was called and the run failed+notified.

### M5 — Config, docs, polish

Add `max_async_seconds` / `poll_interval_seconds` to `RemoteServiceConfig` + defaults; update
`docs/user/USER_GUIDE.md` and the delegation tool description / system prompt so the model knows
remote delegations may run long and will still be delivered. Update
[a2a-client-profile-delegation.md](a2a-client-profile-delegation.md) cross-reference. Final
`poe test`.

## Risks & Mistakes to Avoid

- **`awaiting_remote` must be non-terminal but reaper-distinct from `running`.** If it accidentally
  inherits the fail-without-retry guard, restarts will orphan remote work (the exact bug we're
  fixing). Pin with a test.
- **Idempotency of poll.** Two workers must not double-notify. `mark_completed`/`mark_failed`
  already return `None` if the row isn't in the expected state; gate notification on a successful
  terminal transition (the existing pattern at `task_worker.py:944`).
- **Submit failure vs. enqueue.** If `submit_async` fails after the run row exists, fail the run
  (don't strand it in `queued`) — mirror the existing tool error handling.
- **Loopback fidelity.** The remote agent in tests is FA's own A2A server behind a real
  `ProcessingService` (loopback), so serialization, `tasks/get`, and `tasks/cancel` are real. The
  one thing to engineer is a *non-terminal-then-terminal* sequence (a slow/multi-turn mock LLM) so
  the poller actually polls more than once — an instant completion proves nothing for M3/M4.
- **Migration on both engines.** Use symbolic SQLAlchemy; verify the new column + status enum on
  SQLite and PostgreSQL (`db_engine` parametrization).

## Open Questions

1. Poll cadence and cap defaults — 10 s / 3600 s reasonable, or driven by the agent card?
2. Should `poll_async` prefer SDK `resubscribe` (SSE) over `get_task` (poll) when the agent card
   advertises streaming? Polling is simpler and restart-safe; `resubscribe` is lower-latency but
   reintroduces a held connection. Recommendation: poll for v1, leave `resubscribe` as an
   optimization.
3. Do we want a user-facing `cancel_delegation` tool now, or defer with gap #5?

______________________________________________________________________

## Addendum: two-sided architecture (supersedes the single-sided plan above)

The plan above is **client-side only** — and during implementation we confirmed that is not enough.
FA's own A2A server (`web/routers/a2a_api.py`) processes both `message/send` and `message/stream`
**synchronously**: it creates the `a2a_tasks` row as `working`, runs `handle_chat_interaction`
*inline*, then returns the already-terminal task in the same response. A synchronous server can
never hand back a non-terminal task to poll, so:

- The client's submit-then-poll only avoids blocking when the **remote** server backgrounds the
  task. Against a synchronous server, "submit" blocks for the whole run.
- The loopback test (FA client → FA server) can never exercise the poll path, because FA's server
  completes before `message/send` returns.

So native async delegation has **two sides**, and we implement both.

### Server side — async task processing (spec `blocking` flag)

The A2A spec's `MessageSendConfiguration.blocking` controls this exactly. We honour it in
`_handle_send_message`:

- `blocking == true` (default, absent) → **unchanged** synchronous behaviour. All nine existing
  `TestSendMessage` tests keep passing; no behaviour change for current callers.
- `blocking == false` → create the `a2a_tasks` row as `working`, spawn a background asyncio task
  that runs `handle_chat_interaction` on its **own** `db_context` and transitions the row to
  terminal, and **return the `working` task immediately**. `tasks/get` then reflects live state and
  `tasks/cancel` interrupts the background task.

Background-task lifecycle (mirrors the existing `a2a_cancel_events` pattern):

- Strong references held in a new `app.state.a2a_background_tasks: dict[str, asyncio.Task]` so the
  task is not GC'd (asyncio holds only weak refs); the task removes itself on completion.
- `tasks/cancel` sets the existing `cancel_events[task_id]` **and** cancels the asyncio task; the
  handler maps `CancelledError` → `canceled` row (respecting `update_task_status`'s
  `status != "canceled"` guard).
- App shutdown cancels all tracked background tasks.
- A reaper for `a2a_tasks` rows stuck `working` past a TTL (none exists today; `cleanup_old_tasks`
  is implemented but never wired). Mirrors the delegation-run reaper.

The streaming path (`message/stream`) is left as-is — it already emits `working` then `completed`
over SSE and supports cooperative cancel; the synchronous `send_message` client path keeps using it.

### Client side — non-blocking submit via non-streaming `message/send`

`A2AClientWrapper.send_message` always streams (`ClientConfig(streaming=True)`), which blocks. For
async we add **direct JSON-RPC** methods on the wrapper (full control, no SDK streaming ambiguity,
trivially testable over `ASGITransport`):

- `submit(content_parts, *, context_id) -> Task` — one-shot `message/send` with
  `configuration.blocking=false`; returns the (typically `working`) task. If a synchronous remote
  ignores `blocking` and returns terminal, the caller handles it as instant-complete.
- `get_task(task_id) -> Task` — one-shot `tasks/get`.
- `cancel_task(task_id) -> Task` — one-shot `tasks/cancel`.

The existing SDK-based `send_message` stays for the synchronous delegation path (backward compat).

### Revised milestone order (implementation)

1. **Server async** — `blocking=false` background processing + task tracking + cancel + `a2a_tasks`
   reaper. Tests: `blocking=false` → `working` → poll `tasks/get` → `completed`; cancel mid-flight →
   `canceled`. Existing sync tests untouched.
2. **Client async methods** — `submit`/`get_task`/`cancel_task` direct JSON-RPC on the wrapper. Unit
   \+ loopback tests (incl. the non-terminal-then-terminal poll sequence the async server now makes
   possible).
3. **Schema + repo** — `remote_task_id`, `remote_context_id`, `poll_attempts`, `awaiting_remote`
   status, repo methods. Both engines.
4. **`RemoteA2AService` + protocol** — `submit_async`/`poll_async`/`cancel_async`,
   `PollableDelegationService`, `Pending` sentinel. Loopback tests.
5. **Worker + reaper** — branch `handle_delegated_profile_run` on the pollable capability; add
   `handle_delegation_poll`; register it; extend `handle_delegation_run_cleanup` for
   `awaiting_remote`. **End-to-end async delegation over the loopback** (submit → working×N →
   completed → posted to conversation; restart re-attach; cancel-on-cap).
6. **Config + docs** — `RemoteServiceConfig.max_async_seconds` / `poll_interval_seconds` + defaults;
   USER_GUIDE; delegation tool description / prompt; cross-references.

### Decisions locked in (from the open questions)

- Poll via `tasks/get` (not `resubscribe`) for v1 — restart-safe, no held connection. The async
  server makes a real `tasks/get` available in the loopback.
- Config (`remote_a2a` profile): `poll_interval_seconds` (default 10 s, light backoff to 60 s) and
  `max_async_seconds` (the wall-clock cap). `max_async_seconds` is decoupled from the per-HTTP-call
  `timeout_seconds` and defaults to one hour (`DEFAULT_REMOTE_MAX_ASYNC_SECONDS = 3600 s`), so a
  long-running async delegation is not killed at the single-request timeout boundary. Lower or raise
  it explicitly per remote. The cap only reaps a genuinely orphaned remote run; the assistant can
  poll a still-running delegation via `get_delegation_status` within this envelope.
- No user-facing `cancel_delegation` tool in v1 (cancellation is internal: reaper/cap); revisit with
  the `input_required` continuation work.

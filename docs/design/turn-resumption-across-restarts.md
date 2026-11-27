# Resuming in-progress turns across server restarts

**Status:** Milestones 1 (web streaming turns, graceful hand-off, crash recovery), 2 (Telegram
turns) and 4 (delegation wake turns) implemented. Milestone 3 (task-worker handlers) is proposed.
**Date:** 2026-10-02

## Problem

A deploy or a crash kills every turn that is running at that moment. For a web turn the user is left
with their prompt, whatever tool rows had already been saved, and no reply. Nothing retries the
turn. The next turn on that conversation sees the gap only as the `ABANDONED_TOOL_CALL_RESULT`
placeholder that `_repair_unmatched_tool_calls` puts in. A deploy is the most common case, and it
hits long-running turns (`complex_tasks`, deep research, delegations) hardest, because those are the
turns most likely to be running when the pod is replaced.

Graceful shutdown makes this worse rather than better:

- uvicorn installs its own SIGTERM handler. That handler replaces the one `__main__` installs, so
  `shutdown_event` is only set *after* uvicorn has drained its connections.
- The SSE streams wait on `shutdown_event`. So the drain waits on the streams, and the streams wait
  on the drain. Kubernetes breaks the deadlock with SIGKILL after 30 seconds.
- In practice every deploy is therefore a crash.

## What already makes this tractable

Under [commit-as-you-go](db-commit-as-you-go.md) a streaming turn's progress is durable as it
happens. Each of the following is committed when it is produced:

- the user row,
- each assistant row that carries tool calls,
- each tool result,
- each steering message.

The state needed to continue a turn is therefore already in `message_history`, keyed by `turn_id`.
Rebuilding the LLM context from history is also already what every turn does. A resumed turn is "run
the loop again on this turn's history without adding a new trigger".

## Design

### A turn holds a lease, and the lease is a task

When a turn starts, it arms a **lease**: a `resume_interrupted_turn` row in the task queue.

- The row is `pending` and is scheduled a short way into the future (`LEASE_SECONDS`).
- Its payload has what a relaunch needs that history does not record:
  - interface and conversation,
  - `turn_id`,
  - user id and name,
  - processing profile,
  - the model-tier envelope the turn was admitted with,
  - the resume attempt number.
- The web endpoint writes the user row and the lease in one transaction. A turn whose prompt is
  durable is therefore always a turn that can be recovered.

While the turn runs, one heartbeat per process pushes the `scheduled_at` of all its live leases
forward in a single `UPDATE`. When the turn ends in any way the user can see (complete, failed,
stopped), its lease row is deleted.

A lease is therefore due only when the process that owned it stopped heartbeating. That covers two
cases:

- **Crash** (SIGKILL, OOM, node loss): the lease expires on its own. Whichever process is running
  then resumes the turn within `LEASE_SECONDS` of the crash.
- **Graceful shutdown**: the process suspends its turns and then hands their leases off. Hand-off
  means setting them due immediately, so the replacement pod resumes them within one worker poll.

Using the task queue rather than a new table means claiming, retries, priority lanes and
`SKIP LOCKED` are reused as they are. This matters because a rolling update runs the old and new
pods side by side. The queue's claim is exactly what keeps the two from both resuming a turn.

### Graceful shutdown suspends turns at a safe point

On SIGTERM, `shutdown_event` is set at once. A `uvicorn.Server` subclass forwards its exit signal,
so the SSE streams close and uvicorn's drain completes. Then the process suspends its turns:

1. **Ask every live turn to suspend.** The loop already checks `should_interrupt()` at its iteration
   boundaries. These are before each LLM call and after each tool round. A suspension request reuses
   those checks.
   - An LLM call has no side effects, so cutting it short loses nothing that the resumed turn will
     not regenerate.
   - A tool that is running is allowed to finish, so that its result is recorded rather than
     becoming "unknown whether it took effect".
2. **Wait up to `SUSPEND_GRACE_SECONDS`** for turns to reach a boundary.
3. **Cancel any turn still running.** This is usually one waiting on a confirmation, or a long tool.
   Such a turn resumes with the abandoned-call placeholder for that tool.
4. **Stop the task workers, then hand off the leases.** The order matters. If the leases were handed
   off first, the old pod's own workers could claim them and then be cancelled with them.

A suspended turn is not a stopped turn:

- it does not write the "Stopped" marker,
- it does not reject its confirmations,
- it does not publish `turn_ended`.

The process is exiting, and the client's follow stream reconnects to the new pod.

### Resuming

The `resume_interrupted_turn` handler first decides whether the turn should continue. It looks only
at durable state:

| State of the turn                                                                 | Outcome                                                                                |
| --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Already has a terminal reply (it finished after the heartbeat lapsed)             | Nothing to do                                                                          |
| Something newer has happened in the conversation (a new prompt, a delivered wake) | Closed with an "interrupted" marker in place: continuing would interleave              |
| No trigger row, but rows of its own and no terminal reply                         | Closed with an "interrupted" marker in place                                           |
| A durable confirmation from the turn is still pending                             | Closed with an "awaiting approval" marker: the confirmation's own path runs the action |
| Still live in this process (its heartbeat lapsed but the turn is running)         | Lease re-armed, nothing else                                                           |
| Has already been resumed `MAX_RESUME_ATTEMPTS` times                              | Closed with an "interrupted" marker, so that a crash loop ends                         |
| Otherwise                                                                         | Relaunched                                                                             |

A turn closed "in place" gets its marker stamped with its own last activity, so it reads as the end
of that turn rather than as news after the turns that followed, and it is recorded rather than
delivered. When the resumer cannot launch the turn because another turn is already running in the
conversation, the handler re-arms the lease one attempt further on; when it comes due, the newer
turn has usually moved the conversation past this one, and the attempt cap ends it otherwise. So
every exit leaves the turn either with a lease or with a terminal row. A turn left open by any path
that misses this stops counting as running in the history window once it has been quiet for
`OPEN_TURN_MAX_QUIET` (`processing/history_window.py`), so it cannot pin the window verbatim.

The handler then hands the relaunch to the resumer registered for the turn's interface. The handler
returns as soon as the turn has been launched. It does not run the turn itself, so a long turn is
not bound by the task handler timeout. The relaunched turn arms a fresh lease with the next attempt
number, so a crash during the resumed turn is recovered in the same way.

For web turns, the relaunch goes through the same producer as a new turn, with the same `turn_id`.

- It registers in the hub and publishes `turn_started`. A client's follow stream therefore picks it
  up, and Stop and steering work as they do on any turn.
- `handle_chat_interaction_stream(resume=True)` reuses the persisted user row and inserts no
  trigger. It rebuilds context from history, which already contains the turn's partial rows.
- The turn-context block is placed straight after the turn's last user message, which is where the
  original run had it. It is not appended after the trailing tool results.
- `_repair_unmatched_tool_calls` handles a call whose result never landed, as it already does for
  every turn.

## Deliberate simplifications

- **Turns are resumed only when nothing has happened since.** A turn that loses the race to a new
  prompt stays as it is today: a prompt with partial rows, which the next turn reads through the
  repair placeholder. Merging two interleaved turns is not worth building for a window the size of a
  restart.
- **A turn that is waiting on a confirmation is not resumed.** The durable confirmation outlives the
  process, and its approval path already executes the stored call and posts the result in the
  conversation. Re-prompting from a resumed turn would ask the same question twice. The turn is
  closed with a marker saying the request is still open, so it does not read as pending forever;
  nothing carries on from the approved result, and the user asks again if they need a follow-up.
- **Overlap with a hung process is not fenced.** During a rolling update, an old pod whose event
  loop stalls for longer than `LEASE_SECONDS` could have a live turn resumed beside it. The registry
  guards the same-process case. Fencing across processes would need lease ownership tokens on every
  write, and for a single-replica deployment that is out of proportion to the risk.
- **A resumed turn stays on the tier its rows ran on.** The tier stamped on the turn's last
  assistant row is reused, frozen, so an Auto-routed turn is not re-routed partway through. Only a
  turn that made no model call yet goes back through the envelope it was admitted with.
- **A resumed turn rebuilds what its rows record, not the loop's in-memory state.** The prompt,
  attachments, tool rounds, steering, tier and iteration count are recovered from history. Tools
  activated on demand and attachments queued for the reply are not. History shows the model that it
  activated or attached them, so it can repeat that call. Rebuilding them would mean threading more
  turn-local state through the loop for a turn that has already been interrupted.

## Work plan

1. **Web streaming turns** (implemented). This covers:

   - the lease, heartbeat and hand-off,
   - suspend-at-boundary,
   - the resume handler and the web resumer,
   - SIGTERM reaching `shutdown_event` before uvicorn drains.

   Verified by unit tests for the registry and the handler's decisions, a functional test that
   relaunches a turn interrupted partway through a tool round and checks the reply continues from
   the persisted rows, and a test that a suspended turn's lease is handed off while a completed
   turn's is deleted.

2. **Telegram turns** (implemented). `handle_chat_interaction` used to save a turn's assistant and
   tool rows only once the loop returned, so a Telegram turn that was killed left nothing to resume
   from. It now saves them as they are produced, as the streaming path does, for every caller.

   A Telegram message turn arms a lease before it runs and holds the chat's turn slot while it does.
   A suspension stops it before it replies. Its resumer reruns the turn in the same slot, so new
   messages still steer it, and delivers through the same path as any Telegram reply. A turn that
   comes due with a terminal reply but no delivered message id has its reply sent instead of being
   run again: the registry hands every finished turn to its resumer, and only resumers that push
   replies out act on it.

   Slash-command turns arm no lease and are not resumed. A resumed reply to an earlier message
   continues in that thread, but is rebuilt from recent history rather than the full thread.

   Verified by Telegram functional tests for each of these:

   - the tool-calling row is durable while the tool runs;
   - a running turn holds a lease, which it releases once it replies;
   - a suspended turn keeps its rows and its lease;
   - a resumed turn replies in the chat;
   - an undelivered reply is sent without rerunning the turn.

3. **Task-worker handlers.** A cancelled handler leaves its row `processing` until the 15-minute
   stale reclaim. On graceful shutdown, return those rows to `pending` straight away. Their own
   checkpoints (delivery checkpoint, `delegation_runs.status`) already govern what a re-run does.
   Verified by a worker test asserting the row is claimable immediately after shutdown.

4. **Delegation wake turns** (implemented). The turn that tells the user about a finished delegation
   -- the delegating profile woken with the result -- used to run inline in whichever handler
   noticed the run had finished: a poll, a reconcile, or the hourly cleanup, inside that handler's
   300-second budget. A wake turn that was still calling tools when the budget ran out was
   cancelled, which records nothing, and the hourly cleanup reran it from the top an hour later. Its
   earlier tool effects were durable, so the rerun could repeat them.

   - **Delivery is a task of its own.** Every path that finishes a run queues a
     `delegation_delivery` task instead of delivering. There is one per delegation, under a
     deterministic id: a pending or running delivery owns the work, and a finished one is replaced
     when the run is queued again. Its budget is ten minutes, under the 15-minute stale-claim
     cutoff, so a live delivery is never reclaimed into a second, concurrent wake turn.
   - **A retry resumes the wake turn.** The wake turn's id is deterministic, so a delivery that
     finds the turn already has its data row continues it with `resume=True` rather than starting
     over. Resume accepts the wake's system trigger: it finds the trigger row (or saves it, if the
     turn was cut off before writing it) and keeps it in place, ahead of the turn's own tool calls.
   - **The wake turn holds a lease.** A process that goes away mid-turn leaves the delivery task
     claimed. When the lease comes due, its resumer hands the claim back to the queue (counting the
     lost attempt against the task's retries), and the next delivery resumes the turn.

   Verified by functional tests: a delivery cut off mid-turn is resumed without rerunning its tool,
   a turn cut off before its trigger resumes from its data row, a turn suspended by a shutdown is
   resumed through its lease on another worker, and a poll retry queues the delivery its cut-off
   attempt never queued.

   Deliberate simplification: a wake lease that comes due after the conversation has moved on is not
   resumed through the lease, by the same rule as any other turn. Its delivery task is still
   claimed, so the stale-claim reclaim picks it up 15 minutes later and resumes it then.

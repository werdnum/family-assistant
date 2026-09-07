# Task queue: due-time ordering and priority lanes

## Status

Proposed. Motivated by the message-history re-embedding incident fixed in
[#1192](https://github.com/werdnum/family-assistant/pull/1192), whose workaround this design
replaces with a queue-level fix.

## Problem

The task queue orders eligible work as
`scheduled_at ASC NULLS FIRST, retry_count ASC, created_at ASC` (`TasksRepository.dequeue`, both
backends). A task enqueued with no `scheduled_at` therefore sorts ahead of every task that has one,
no matter how long the scheduled task has been due. Three consequences, in increasing order of how
often they bite:

1. **Any burst of immediate work starves all scheduled work.** Reminders, automations, delegation
   polls and the nightly cleanups all carry a `scheduled_at`. A reminder due at 07:00 waits behind
   every immediate task enqueued at 07:01, 07:02 and onward, for as long as the burst lasts.
2. **A retry is demoted behind fresh work.** `reschedule_for_retry` sets `scheduled_at` to the
   backoff time, so a retried user-facing task (a reminder whose delivery hit a transient error)
   falls into the scheduled group and waits behind every immediate task that arrived since.
3. **Bulk background work monopolises the pool.** Message-history indexing enqueues one immediate
   task per persisted row, and its backfill walk enqueues its own continuation immediately after
   each batch. With NULLS FIRST, a walk over a large corpus is a self-perpetuating chain that holds
   a worker until it finishes and pushes every scheduled task to the back of the queue. This is the
   mechanism that made #1192's re-embedding storm delay everything else.

#1192 works around the third consequence by scheduling per-row indexing tasks two minutes out with a
jitter. That moves them into the scheduled group, where they compete with reminders on due time
rather than jumping ahead of them, and it also serves as a debounce so a turn is indexed once it has
finished. The debounce is a legitimate purpose. Using the delay for queue fairness is not: it is a
property of one enqueue site, invisible to the queue, and the next bulk producer will forget it. The
queue itself has to know what is urgent.

There is a second problem that ordering alone cannot fix. The pool has two interchangeable workers
(`task_worker_count`, see `docs/design/multi-task-worker-pool.md`). Once both are inside a
five-minute indexing batch, a reminder that becomes due waits for one of them to finish regardless
of how it is ordered. Priority decides what runs *next*; it does not free a worker.

## Approach

Two changes, each independently valuable, plus a follow-up that retires the workaround.

### Order by when a task became due

A task with no `scheduled_at` is not "before everything"; it is due at the moment it was created.
The dequeue order becomes `COALESCE(scheduled_at, created_at) ASC, id ASC`, and the same expression
replaces the NULLS-FIRST ordering in the UI listing. Eligibility is unchanged
(`scheduled_at IS NULL OR scheduled_at <= now`).

`retry_count` leaves the ordering. Its intent, fresh work before retried work, is now carried by the
due time: a retry's backoff *is* a later due time, and once it is due it has waited its turn like
anything else. Keeping it would reintroduce the second consequence above in a milder form.

`scheduled_at` stays nullable rather than being filled with the enqueue time. The worker judges
eligibility against an injected clock, and several tests drive it with a fake one; a NULL that is
always eligible keeps those tests honest, whereas an enqueue-side timestamp from the real clock
would be silently ineligible under a fake one. The COALESCE gives the ordering benefit without
touching eligibility.

### Priority as a property of the task

Every task carries a `priority`, one of two named levels:

- **interactive**: somebody is waiting, or expects it at a particular time. Reminders and future
  callbacks, confirmation-gated tool executions, delegated runs and their polls, automation and
  event-listener scripts, email-intake actions, and user-initiated document work (an upload, a
  reindex, "index this email").
- **background**: nobody is waiting; the work catches up when the house is quiet. Message-history
  indexing (per turn and backfill), note indexing, and every cleanup and reaper task.

The dequeue order is `priority DESC, COALESCE(scheduled_at, created_at) ASC, id ASC`. A due
interactive task is always taken before any background task, however long the background task has
waited.

Priority is chosen at the enqueue site, not looked up from the task type, and the parameter is
**required** rather than defaulted. Two reasons. One handler can serve both lanes: an
`embed_and_store_batch` produced by a user's upload is interactive, and one produced by a future
bulk re-index is not, so the type cannot decide. And a required parameter is the enforcement
chokepoint: the type checker refuses a call site that has not made the choice, so a new producer
cannot land in the wrong lane by omission. A task's children inherit its priority; the indexing
pipeline carries it from the handler that ran it to the processor that dispatches embedding batches.

The module-level `storage.tasks.enqueue_task` still has seven callers alongside the repository's
`enqueue`, and `storage.tasks.dequeue_task` has none. Both go: one enqueue path is what makes the
required parameter a chokepoint rather than a convention.

### Reserved interactive capacity

The pool gains a second kind of worker. A **general** worker dequeues any priority, highest first,
as today. A **reserved** worker dequeues interactive tasks only. `TaskWorker` already restricts its
dequeue by the task types it handles; a minimum priority is the same kind of filter, one more
predicate in the claim query. Reserved workers are built by the same builder with the same handler
set, so the health monitor's in-place restart works unchanged.

The default becomes two general workers and one reserved worker. Interactive work can use all three;
background work is confined to the two it has today, so its throughput does not change. The
in-process deadlock fix that motivated the pool (a delegated run parked on a confirmation, resolved
by a sibling running the confirmation task) needs at least two workers that can run interactive
tasks, which any configuration with a general count of at least one and a reserved count of at least
one satisfies.

The reserved count is configuration alongside `task_worker_count`, which is not yet documented in
`docs/operations/CONFIGURATION_REFERENCE.md` and should be when its sibling is added.

### The #1192 delay keeps its debounce role only

With the lanes in place, the two-minute delay on per-turn indexing does exactly one job: it lets a
turn finish before the first of its tasks embeds it. Its jitter no longer needs to spread tasks
across workers to keep them off the interactive path, and the docstrings that justify it on those
grounds should say only what is still true. The backfill continuation and the per-turn tasks are
both enqueued as background work, so the walk proceeds only while nothing interactive is due.

## Alternatives considered

- **Due-time ordering alone.** Fixes the NULLS-FIRST inversion and the retry demotion, but a backlog
  of ten thousand background tasks created before 07:00 still runs before a reminder due at 07:00.
  Fair is not the same as responsive. Kept as the first milestone because it is a self-contained
  improvement.
- **Priority from a per-type registry.** Keeps enqueue sites untouched, but the same handler serves
  both lanes (see above), and the registry lives in the worker while enqueue lives in storage, so
  the lookup would either be a second copy of the type list or a runtime failure for an unknown
  type. A required parameter puts the decision where the reviewer sees it.
- **Strictly partitioned pools** (interactive-only and background-only workers, no general ones).
  Simpler to reason about but not work-conserving: idle background workers cannot absorb an
  interactive burst. The general-plus-reserved shape gives interactive work every worker and still
  guarantees it one.
- **Separate queue tables per lane.** Two tables, two dequeue paths, two sets of admin views, for a
  distinction that one column expresses.
- **Making `scheduled_at` NOT NULL.** The purer model, rejected for the fake-clock coupling
  described above. Worth revisiting if enqueue ever takes a clock.

## Deliberate simplifications

- **Background work can starve under sustained interactive load.** General workers take interactive
  tasks first, so a deployment that is never quiet never indexes. Interactive load in this
  application is bursty and small, and the daily cleanups tolerate a delay measured in hours. No
  aging, no boost.
- **No preemption.** A background task that holds a general worker keeps it until it finishes or
  times out. Interactive latency is bounded by the reserved worker, not by interrupting anything.
- **Two levels, not a scale.** The column is an integer so a third level would be a code change
  rather than a migration, but nothing today needs one. A "system" level above interactive, or a
  "bulk" level below background, is machinery for a scenario that has not happened.
- **No per-type concurrency limits.** Two background workers can both be inside the backfill walk.
  The fingerprint check from #1192 makes that a bounded duplicate rather than duplicated spend.
- **The reserved worker idles most of the time.** That is the point: it is capacity held back for
  the moment both general workers are busy. One idle coroutine on a database poll is cheap.

## Work plan

1. **Due-time ordering.** Replace the NULLS-FIRST ordering in both dequeue paths and the UI listing
   with the COALESCE expression, drop `retry_count` from the order, fold
   `storage.tasks.enqueue_task` into the repository and delete `storage.tasks.dequeue_task`.
   Verified by tests on both backends: a scheduled task that came due before an immediate task was
   created is dequeued first; a retried task whose backoff has elapsed is not pushed behind
   immediate tasks created after it.
2. **Priority column.** Add the column with a migration that classifies existing rows by task type
   so in-flight reminders are not demoted, make the enqueue parameter required and classify every
   call site, thread the priority through the indexing pipeline to the embedding dispatch, and put
   `priority` first in the dequeue order. Verified on both backends: with a backlog of due
   background tasks, a newly enqueued interactive task is the next one dequeued; an upload's
   embedding batches carry the upload's priority.
3. **Reserved workers.** Add the minimum-priority filter to `TaskWorker` and the claim query, the
   configuration for the reserved count, the pool construction, and the operations documentation for
   both counts. Verified with the pool test fixtures: with every general worker parked on a
   background task, an interactive task starts within the wake latency; a reserved worker never
   claims a background task; the deadlock test still passes with the default configuration.
4. **Retire the workaround's fairness role.** Reword the #1192 delay and jitter to their debounce
   purpose, and update `docs/design/multi-task-worker-pool.md`, which describes the workers as
   interchangeable.

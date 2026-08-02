# Commit-as-you-go database access

**Status**: Proposed. **Motivating incident**:
[#1076](https://github.com/werdnum/family-assistant/issues/1076) (tool-created attachments invisible
to same-turn delegation), which is the latest instance of a year-long pattern of bugs caused by the
turn-spanning transaction.

## Problem

`DatabaseContext.__aenter__` calls `engine.begin()`: one connection, one transaction, committed only
at `__aexit__` (`src/family_assistant/storage/context.py:227-256`). Every entry point wraps its
whole unit of work — a multi-minute conversational turn, an HTTP request, a worker job — in one of
these. Nothing written during a turn is visible to any other connection until the turn ends.

That model is wrong for an agentic application, and the codebase demonstrates it empirically:

- **21 `create_isolated_context()` workaround sites** (14 in `task_worker.py`, 6 in
  `tools/services.py`, 1 in `processing/service.py`) exist solely to get commit-now semantics:
  releasing row locks before long delegated turns, making delegation state visible to waiting
  callers, persisting history durably, validating against a committed view. The escape hatch is used
  more often than the front door for writes that matter.
- **Dialect-forked semantics**: `supports_isolated_writes` is `False` on SQLite because the
  `StaticPool` shares one connection and transaction across all contexts (`storage/base.py:49-82`).
  The default test backend literally cannot express production's visibility behaviour — #1076 needed
  `@pytest.mark.postgres` to be observable at all.
- **False atomicity**: the turn transaction promises all-or-nothing, but the turn's real side
  effects (files on disk, Telegram sends, external API calls, spawned k8s jobs) are not
  transactional. Meanwhile the writes we *want* durable (history, attachments, errors) have been
  individually carved out to survive rollback — revealing that the intended semantics were
  commit-as-you-go all along.
- **Operational hazards**: minutes-long transactions hold a pooled connection per turn, hold row
  locks across LLM calls (documented contention at `task_worker.py:1409-1414`), pin xmin, and defeat
  statement retry (a 25P02-aborted transaction cannot be retried,
  `storage/context.py:171-176, 334-337`).

### A live correctness bug, independent of visibility semantics

Parallel tool calls in a turn share the single turn connection: `processing/tool_execution.py`
threads one `db_context` into every tool, and `llm_loop.py:798-801` dispatches them with
`asyncio.create_task` + `as_completed`. SQLAlchemy's `AsyncConnection` is not safe for concurrent
use, so two tools that both touch the DB in the same round are racing one connection today (on
asyncpg this surfaces as `another operation is in progress`; interleaved statements on a shared
transaction are the quieter failure mode). This motivates the migration even without agreeing about
visibility: under the new model every operation acquires its own pooled connection, and parallel
tool rounds become genuinely safe.

## Target architecture

Replace `DatabaseContext` with two types whose names force every call site to be visited and whose
*types* encode the transactional contract:

### `Database` — the default handle

A plain, stateless object (engine + retry policy + lazily-cached repositories). **Not a context
manager.** Passed freely across turns, tasks, and tool calls.

```python
db = Database(engine)
await db.notes.add_or_update(title, content)      # own short transaction, committed on return
tasks = await db.tasks.get_pending_tasks()        # own short transaction
```

Each operation on the handle acquires a connection from the pool, begins a transaction, executes,
commits, and releases. A repository method that issues multiple statements is atomic at *method*
granularity (see "Repository internals" below) — never per-statement.

### `DatabaseTransaction` — the explicit atomic block

```python
async with db.transaction() as txn:
    run_id = await txn.delegation_runs.create_run(...)
    await txn.tasks.enqueue(DELEGATED_PROFILE_RUN_TASK_TYPE, ...)
# commit; on exception: rollback of both writes
```

- Bound to one connection; exposes the same repository namespace and query helpers as `Database`.
- `Database` and `DatabaseTransaction` are **deliberately not subtypes of each other**. A function
  that must run inside the caller's transaction takes `DatabaseTransaction`; everything else takes
  `Database`. `ToolExecutionContext.db_context` is typed `Database`, so holding a transaction across
  a tool call becomes a *type error*, not a code-review catch.
- `txn.on_commit(cb)` defers to this transaction's commit. On the bare `Database` handle,
  `on_commit`-style callbacks are unnecessary — the write is durable when the method returns, so
  callers run the follow-up as ordinary sequential code.
- No nested `transaction()` on a `DatabaseTransaction` (fail fast with `RuntimeError`; the two
  existing savepoint users — `a2a_tasks.py:156`, `email_indexer.py:233` — keep using
  `conn.begin_nested()` on the transaction's connection, which now has a guaranteed enclosing
  transaction).
- **Handle operations refuse to run under an ambient transaction.** The split stops a
  `DatabaseTransaction` being passed where a handle belongs, but not the reverse capture: code
  inside `async with db.transaction():` reaching the `db` handle (directly, or via a helper frames
  down) would open a *second* transaction scope — a hard deadlock on SQLite's engine lock, and on
  PostgreSQL a silent write on another connection that escapes the enclosing rollback. So opening a
  transaction records (ContextVar token, owning task); a handle operation performed by the task that
  currently holds an open transaction raises immediately, **in production, on both backends** —
  turning a backend-dependent hang into an error at the offending call. Detached tasks are exempt by
  task identity (e.g. the DB log handler's `create_task` writes, which merely queue on the SQLite
  lock and proceed).

### Repository internals

Repositories already funnel through one executor seam (`storage/repositories/base.py:14-25`). They
are rewritten against a small protocol implemented by both types (`execute`, `fetch_one`,
`fetch_all`, `atomic()`, dialect info):

- `atomic()` is a **closure runner**, not a context manager: `await self._db.atomic(body)` where
  `body` is `async (txn) -> T`. On `Database` it opens a transaction, runs the closure, commits —
  and on a replayable failure rolls back and re-invokes the closure (see retry semantics). On
  `DatabaseTransaction` it joins: the closure runs once against the ambient transaction, and retry
  is owned by whoever opened it. Multi-statement repository methods (`tasks.dequeue`,
  `email.store_incoming`, `notes.add_or_update`, all of `schedule_automations`, …) put their
  statements in the closure and are therefore atomic — and replayable — in both usage modes. The
  closure-runner shape (rather than `async with`) exists precisely so the whole body *can* be
  re-entered on retry; closures must confine their side effects to the transaction.
- The audit found **no repository method that needs cross-method ambient state** —
  `delegation_runs`, `confirmation_requests`, `error_logs`, `push_subscriptions`, `taint_audit`,
  `vector`, and the conditional-UPDATE claims in `worker_tasks` are already
  single-statement/conditional and commit-as-you-go-ready.

### Retry semantics (a correctness win, not just a cleanup)

- Single-statement handle operations and `atomic()` closures retry the *whole*
  acquire-begin-execute-commit unit (the closure is re-invoked after rollback). This finally makes
  PostgreSQL serialization failures (`40001`) and deadlocks (`40P01`) — currently listed in
  `context.py:23-25` as "for reference, not currently used" — genuinely retryable.
- Inside a `DatabaseTransaction` (the explicit `async with db.transaction()` block), statement
  failure raises immediately: an `async with` body cannot be re-entered, and retrying inside an
  aborted transaction is futile. A caller-side sequence that wants automatic replay uses the closure
  form (`await db.atomic(body)`) instead of the block form; the retry guarantee is explicitly scoped
  to units that can actually be replayed.
- **Retry × `on_commit`**: callbacks are registered on the transaction object and discarded with it
  on rollback, so a replayed closure re-registers them and they fire exactly once, after the final
  successful commit. No attempt that rolled back ever fires its callbacks (relevant now that
  `40001`/`40P01` widen the retry set — `repositories/tasks.py:124-135`'s worker wake-up would
  tolerate a double-fire, but nothing should have to).

### Dialect strategy

- **PostgreSQL**: production currently uses `NullPool` (`storage/base.py:59-60`), which was
  tolerable at one-connection-per-turn but means one TCP connect per operation under
  commit-as-you-go. Switch to the default async `QueuePool` with modest bounds.
  `pool_reset_on_return="rollback"` stays.
- **Engine topology**: the per-worker engines minted by `Assistant.create_worker_engine`
  (`assistant.py:1865-1897`) are deleted in the cutover and all workers share the application
  engine. That topology exists, per its own docstring, because "a worker parked inside a transaction
  … does not hold the single shared connection and block its siblings" — a hazard this design
  removes (nothing parks holding a transaction any more). It cannot stay: a per-engine SQLite lock
  cannot serialize across engines, and per-engine PostgreSQL pools would multiply the 5/10 bounds
  per worker instead of bounding the application. One database, one engine.
- **SQLite**: keep `StaticPool` (single connection), but serialize *transaction scopes* with a
  per-engine `asyncio.Lock` held from begin to commit. All transactions are short after this
  migration, so serialization is cheap, and it eliminates today's undefined behaviour where
  concurrent contexts silently share one transaction — the direct cause of
  `pool_reset_on_return=None` hazard documented at `base.py:73-81`. SQLite and PostgreSQL visibility
  semantics **converge**: a committed write is immediately visible everywhere on both backends, so
  the `supports_isolated_writes` fork and its untestable-on-SQLite bug class disappear.

## Deliberate behaviour changes

These are the semantic changes; each is intended and called out for review:

1. **A failed turn no longer discards its DB writes.** This is already production behaviour for
   history (`_USE_ISOLATED_HISTORY_WRITES = True`, `processing/service.py:153`) and for everything
   task_worker writes through isolated contexts; the change extends it uniformly and makes SQLite
   match. Errors still surface as errors — they just don't unwind durable facts (a fetched
   attachment *was* fetched; a received user message *was* received).
2. **Rollback-dependent sites get explicit transactions.** The audit enumerated where rollback is
   load-bearing; each becomes a `db.transaction()` block (see worklist below). The one *deletion*:
   `tools_api.py:117-118`'s manual `conn.rollback()` on validation errors — validation moves before
   writes (fail fast) instead.
3. **`on_commit` timing.** Worker wake-ups (`repositories/tasks.py:124-135`) and stream-hub nudges
   (`chat_api.py:2088-2121`, `task_worker.py:2479-2531`) currently defer to end-of-turn commit.
   Under commit-as-you-go they fire as soon as the enqueue/write is durable — strictly earlier, and
   the "don't wake before the row is visible" invariant those comments protect is preserved by
   construction.
4. **OAuth callback**: today a failed token exchange rolls back the flow-nonce claim; after
   migration the nonce is consumed and the user restarts the flow. Rare failure, reasonable
   behaviour (behaviour-altitude principle) — not worth holding a transaction across a network call.
5. **Mid-turn crash leaves a user message without an assistant reply** in history. Already true in
   production (isolated history writes); now also true on SQLite. More generally, a turn's partial
   state being durable becomes the *design*: an assistant tool call persisted without its result is
   the normal shape of any turn that dies mid-tool, so the orphan repair introduced by
   [#1077](https://github.com/werdnum/family-assistant/pull/1077)
   (`ProcessingService._repair_unmatched_tool_calls`) is **load-bearing** under this design, not
   defensive belt-and-braces — it must not be simplified away.
6. **Read consistency.** The audit and worklist above are entirely about write atomicity; reads
   change too, differently per backend. On PostgreSQL: no change — READ COMMITTED means two reads in
   one turn already saw other sessions' commits between them. On SQLite: a real change — the shared
   turn transaction currently gives a turn a consistent snapshot for its whole duration, and that
   goes away. This is the right trade (consistency only one backend provides, and that no code can
   rely on, is not a feature), and it bounds the latent-races note below: those races are unchanged
   on PostgreSQL and slightly widened on SQLite.

## Explicit-transaction worklist

From the atomicity audit, the sequences that must be wrapped in `db.transaction()` (everything not
listed runs on the handle):

| Site                                                                                                           | Why                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TasksRepository.dequeue` PG path (`repositories/tasks.py:186-233`)                                            | `SELECT … FOR UPDATE SKIP LOCKED` + `UPDATE` — the lock *is* the claim                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `_process_task` terminal update + recurrence (`task_worker.py:3023-3037`; failure twin 3096-3128)              | done/failed + next-instance enqueue must be atomic or recurring tasks stop/double-fire                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Advance-outbox flush (`task_worker.py:2723-2752`)                                                              | enqueue + payload-clear is the outbox contract                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `ConfirmationService._approve` (`confirmation_service.py:182-231`)                                             | approve + enqueue execution ("atomically" is in the docstring)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `delegate_to_service_tool` (`tools/services.py:925-973`)                                                       | create_run + enqueue; `IntegrityError` handler depends on block rollback                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Delegation notification + response delivery (`task_worker.py:2034-2097, 2259-2345`)                            | explicit block spanning add_message → interface send → mark_notified; deliberately spans **one** interface send (bounded), preserving the documented rollback-on-send-failure that keeps `notified_at` NULL for retry. **Precondition**: the send path must not touch the database from the sending task — today `EmailChatInterface._resolve_target` (`email_intake/outbound.py:210-217`) and the Telegram attachment-content fetch (`telegram/interface.py:266-285`) open their own contexts mid-send, which the ambient-transaction guard would reject (and Telegram's per-attachment `except` would turn into silently dropped attachments). The cutover hoists these into a pre-resolve step: targets resolved and attachment payloads fetched *before* the block opens, with interfaces receiving prepared data |
| Delegation source-profile wake (`task_worker.py:2130-2205`) — **must be split, not wrapped**                   | today's isolated context spans a full source-profile LLM turn (`handle_chat_interaction` at 2158-2188), which would violate the no-transaction-across-LLM invariant. Restructure: commit the wakeup-data message on the handle first, run the turn untransacted, then a short explicit block for response delivery + `mark_notified` (same shape as the row above)                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Email intake webhook (`webhooks.py:467-484`), enclosing `email.store_incoming` (`repositories/email.py:44-94`) | the block must extend to the *caller*: store (itself insert + index-task enqueue + backfill update) **plus** the conditional action-task enqueue. If only the store commits and the enqueue fails, the delivery retry hits the duplicate Message-ID, gets `None` back, and the action is skipped forever                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `revoke_api_token` (`storage/api_tokens.py:239-301`)                                                           | parent revocation + child refresh-token cascade must be atomic, or a failure between them leaves a revoked token whose refresh token can still mint a replacement                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `handle_reindex_document` (`task_worker.py:4734-4752`)                                                         | embedding delete → document read → replacement-task enqueue; a failure after the delete commits strips the document's existing search data with no replacement dispatched                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `spawn_worker_tool` DB pair (`tools/worker.py:431-463`)                                                        | worker task row + completion event listener                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `EventProcessor.process_event` per-listener sequence (`events/processor.py:155-196`)                           | rate-limit update + action enqueue + one-time-listener disable must be atomic per listener, or a failed disable re-fires a one-time action and a failed enqueue burns quota; the recent-event record can be its own write                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Indexing doc+embedding pairs (`message_history_indexer.py:110-129`, `indexing/tasks.py:195-205`)               | doc committed without embeddings = permanently unsearchable (upsert makes re-runs no-ops); embedding generation (network) happens *before* the transaction opens                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Notes re-indexing (`notes_indexer.py:88-95` + `dispatch_processors.py:133-150`) — **boundary restructure**     | the embedding delete at :95 and the replacement-task enqueue live on opposite sides of the pipeline run (which may call LLMs). Upsert the document in its own transaction, run the pipeline untransacted to *prepare* the dispatch payload, then one short block for delete-old-embeddings + enqueue-replacement, so the deletion never commits without a durable replacement dispatch                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `a2a_tasks.create_task_if_absent` fallback + `email_indexer` attachment registration                           | `begin_nested()` requires an enclosing transaction                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

A general rule follows from the delivery precondition above: **converting a site to an explicit
block includes auditing its call tree** — everything reached from inside the block must take the
`DatabaseTransaction` or touch no database at all, with handle-using helpers hoisted to before the
block. The production ambient-transaction guard is the enforcement: a missed path fails immediately
at the offending call with a clear stack, on both backends, rather than deadlocking on one and
silently escaping rollback on the other — and the dual-backend suite exercises the delivery paths,
so misses surface in CI, not production.

Latent races the audit found that are *not* new (last-writer-wins RMW in `manually_retry`,
`check_and_update_rate_limit`, `notes append`, `modify_pending_callback_tool`'s missing status
re-assert, etc.) are recorded as follow-up issues, not blockers — today's turn-long transactions
don't protect them either (they span *between* transactions).

## Verification: automation does the "did this break anything" work

The migration touches ~82 context-entry sites plus 50 injected endpoint parameters. Manual review
cannot carry that; the following machinery can.

### 1. The type checker generates the worklist and enforces the contract

Deleting `DatabaseContext` and introducing differently-named, differently-shaped types means
**every** call site, annotation, and `async with` fails `basedpyright` until visited (config covers
`src` *and* `tests`, `pyproject.toml:559-575`). Classification is forced at each site: does this
function need `Database` or `DatabaseTransaction`? The distinction then *stays* enforced forever —
e.g. `ToolExecutionContext.db_context: Database` makes "transaction held across a tool call"
unrepresentable.

Blind spots (`typeCheckingMode = "basic"`, `Any`-typed paths, string-keyed dispatch) are covered by
layers 2–4.

### 2. ast-grep conformance rules

Because the cutover deletes the legacy names outright (see migration plan), no migration ratchet is
needed: once `DatabaseContext` / `get_db_context` / `create_isolated_context` no longer exist, the
old pattern is unrepresentable and the type checker alone prevents reintroduction. Two permanent
rules guard the parts the type checker cannot see:

- ban `engine.begin()` / raw transaction management outside `storage/` (lands with the cutover)
- ban raw `create_async_engine` outside `storage/base.py` — this one lands in **M0**, covering
  `tests/` too, because bypassing fixtures are a moving target: the known offenders are
  `tests/conftest.py:324` (PG engine, wrong pool class), `tests/functional/web/conftest.py:548`
  (session engine, no factory at all), `tests/functional/storage/test_message_history.py:48-58` and
  `tests/unit/storage/test_message_history_live_updates.py:19-28` (local in-memory fixtures). The
  rule, not the inventory, is what guarantees every DB test actually exercises the factory's pool
  configuration and instrumentation

### 3. Runtime invariants asserted by the test suite

Instrumentation lives in `create_engine_with_sqlite_optimizations` (opt-in), enabled by the shared
`db_engine` fixture (`tests/conftest.py:224-392`) — the chokepoint every DB test funnels through:

- **No transaction spans an LLM call or tool dispatch.** `DatabaseTransaction.__aenter__` sets a
  `ContextVar` token (inherited by child tasks — exactly the propagation we want to catch);
  `RuleBasedMockLLMClient._record_call` (`tests/mocks/mock_llm.py:119`) and a test wrapper at
  `ToolExecutor.execute` (`processing/tool_execution.py:778`) assert it is unset. This turns the
  core architectural rule into a failing test rather than a review comment, forever.
- **Transaction duration watchdog**: begin/commit event listeners fail a test if any transaction
  lives longer than a generous bound (catches accidentally reintroduced long holds that the
  ContextVar check can't see, e.g. via raw connections).
- **Connection-leak teardown check**: pool checkout counter must be zero at test end.
- **Cross-connection visibility property test** (`@pytest.mark.postgres`): for each repository write
  method, call it on a `Database` handle, then read from a second engine — the row must be visible.
  This is the direct regression test for the #1076 bug class, parametrized across the whole
  repository surface.
- **Parallel tool-call test**: N concurrent tools writing via `context.db_context` on PostgreSQL —
  currently unsafe-by-construction, safe after migration, and locked in by test.

### 4. Dual-backend CI is the backstop — after fixing what it tests

CI already runs the full non-Playwright suite against **both** SQLite and PostgreSQL
(`test-backend-sqlite` / `test-backend-postgres`,
`.github/workflows/ci-with-devcontainer.yml:353, 454`) plus both Playwright lanes — near-100%
dual-backend coverage is the single most valuable existing asset. The gap to close first (Milestone
0): several test engines bypass the production factory (see the fixture list under the ast-grep
rules above). Route every test engine through `create_engine_with_sqlite_optimizations`, enforced by
the M0 ban on raw `create_async_engine`, so the pool/serialization changes in this design are
actually what CI exercises.

## Migration plan

Two PRs, each green on full dual-backend CI. The whole semantic change lands **all at once**: no
interim tactical fix, no window with two internal database APIs, complying literally with the
no-backwards-compatibility rule (`AGENTS.md`: "when migrating from X to Y, delete X immediately").

- **M0 — groundwork (no semantic change).** Unify test engine creation through the production
  factory, enforced by the raw-`create_async_engine` ban; add engine instrumentation + the
  leak/duration checks (observational until the cutover).
- **M1 — the cutover, one PR.** Introduce `Database` / `DatabaseTransaction` / executor protocol;
  rewrite the repositories (moving multi-statement bodies into `atomic()` closures); convert every
  caller per the worklist; and in the same change delete the legacy API and its scar tissue:
  `DatabaseContext`, `get_db_context`, `create_isolated_context`, `supports_isolated_writes`,
  `_USE_ISOLATED_HISTORY_WRITES` and the `save_with_isolated_context` plumbing, the a2a pre-commit
  workaround (`a2a_api.py:310-347`), the deferred-confirmation FK workaround
  (`deferred_tool_confirmation.py:150-158`), `tools_api.py`'s manual rollback, the per-worker engine
  topology (`Assistant.create_worker_engine` / `worker_engines`, `assistant.py:1865-1897` — see
  dialect strategy), and SQLite's `pool_reset_on_return=None` (`storage/base.py:83`), which exists
  solely to protect the cross-context clobbering the engine lock makes impossible. The conversion
  includes hoisting interface-internal database reads out of delivery paths (the pre-resolve step in
  the worklist). Flip the runtime invariants from observational to enforcing; land the
  cross-connection visibility property test and the parallel tool-call test; update `AGENTS.md`'s
  Database Access Pattern section and the error message that told the model to "try again once they
  are committed".

For reviewability, the cutover PR is structured as one commit per subsystem in dependency order —
storage core → task worker → web `get_db` dependency → turn pipeline (`turn_producer`, `chat_api`,
`llm_loop`, `ToolExecutionContext`) → telegram → a2a → asterisk → excisions — with the PR head
green; intermediate commits need not be. **#1076 is structurally fixed by the cutover**
(tool-registered attachments commit at registration and are visible to delegation validation), which
is also the point where #1077's `_repair_unmatched_tool_calls` formally changes role from race
mitigation to the designed-for recovery path (behaviour change 5).

## Alternatives considered

- **Interim tactical fix for #1076** (register tool attachments via an isolated context ahead of the
  migration): deliberately not landed — the cutover fixes the bug structurally, and a stopgap that
  the same change immediately deletes adds a confusing intermediate state for little gain. The break
  persists only until M1 merges.
- **Incremental migration** (per-entry-point PRs with a shrink-only ast-grep ratchet modeled on
  `.lint-budget.toml`, budgets seeded from measured counts): viable and fully designed, but it
  leaves two internal database APIs coexisting across the window — against the delete-immediately
  rule — and spreads one semantic change across many merges. Rejected by maintainer decision in
  favour of the single cutover; with the legacy names deleted in the same PR that converts the
  callers, the ratchet has nothing left to count.
- **Keep `DatabaseContext`'s name/shape, change semantics silently**: zero compile errors → no
  worklist, and the most dangerous kind of change (behaviour shifts under unchanged syntax).
  Rejected.
- **Session-per-turn with SQLAlchemy ORM `Session`**: this codebase is Core-based with a working
  repository pattern; introducing the ORM is an unrelated (larger) migration.

## Decisions

- **Cadence: groundwork PR, then one cutover PR** — new API, all caller conversions, and legacy
  deletion land together; no interim tactical fix (maintainer decision).
- PostgreSQL pool bounds: **5/10** (`pool_size`/`max_overflow`) at this deployment's scale.
- Naming: **`Database` / `DatabaseTransaction`** — the `*Context` suffix was part of what made the
  current type easy to misuse.
- Conversion order within the cutover: **web dependency before the turn pipeline** (smaller blast
  radius first).
- Production runtime checks: the **handle-under-ambient-transaction guard raises in production on
  both backends** (it is the difference between an exception and a wedged turn); the LLM/tool
  boundary assertion stays test-only, since its seams are the test fakes.

No open questions remain.

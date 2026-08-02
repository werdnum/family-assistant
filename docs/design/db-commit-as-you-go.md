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
- **Unsafe concurrency**: parallel tool calls in a turn share the single turn connection
  (`processing/llm_loop.py:773-823`) — concurrent use of one `AsyncConnection` from multiple asyncio
  tasks.

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

### Repository internals

Repositories already funnel through one executor seam (`storage/repositories/base.py:14-25`). They
are rewritten against a small protocol implemented by both types (`execute`, `fetch_one`,
`fetch_all`, `atomic()`, dialect info):

- `Database.atomic()` opens a real transaction; `DatabaseTransaction.atomic()` is a join
  (nullcontext). Multi-statement repository methods (`tasks.dequeue`, `email.store_incoming`,
  `notes.add_or_update`, all of `schedule_automations`, …) wrap their bodies in
  `async with self._db.atomic() as tx:` and are therefore atomic in both usage modes.
- The audit found **no repository method that needs cross-method ambient state** —
  `delegation_runs`, `confirmation_requests`, `error_logs`, `push_subscriptions`, `taint_audit`,
  `vector`, and the conditional-UPDATE claims in `worker_tasks` are already
  single-statement/conditional and commit-as-you-go-ready.

### Retry semantics (a correctness win, not just a cleanup)

- Handle-level operations retry the *whole* acquire-begin-execute-commit unit. This finally makes
  PostgreSQL serialization failures (`40001`) and deadlocks (`40P01`) — currently listed in
  `context.py:23-25` as "for reference, not currently used" — genuinely retryable.
- Inside a `DatabaseTransaction`, statement failure raises immediately (retrying inside an aborted
  transaction is futile; the caller owns retry of the whole block if it wants it).

### Dialect strategy

- **PostgreSQL**: production currently uses `NullPool` (`storage/base.py:59-60`), which was
  tolerable at one-connection-per-turn but means one TCP connect per operation under
  commit-as-you-go. Switch to the default async `QueuePool` with modest bounds.
  `pool_reset_on_return="rollback"` stays.
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
   production (isolated history writes); now also true on SQLite.

## Explicit-transaction worklist

From the atomicity audit, the sequences that must be wrapped in `db.transaction()` (everything not
listed runs on the handle):

| Site                                                                                                                       | Why                                                                                                                                                              |
| -------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TasksRepository.dequeue` PG path (`repositories/tasks.py:186-233`)                                                        | `SELECT … FOR UPDATE SKIP LOCKED` + `UPDATE` — the lock *is* the claim                                                                                           |
| `_process_task` terminal update + recurrence (`task_worker.py:3023-3037`; failure twin 3096-3128)                          | done/failed + next-instance enqueue must be atomic or recurring tasks stop/double-fire                                                                           |
| Advance-outbox flush (`task_worker.py:2723-2752`)                                                                          | enqueue + payload-clear is the outbox contract                                                                                                                   |
| `ConfirmationService._approve` (`confirmation_service.py:182-231`)                                                         | approve + enqueue execution ("atomically" is in the docstring)                                                                                                   |
| `delegate_to_service_tool` (`tools/services.py:925-973`)                                                                   | create_run + enqueue; `IntegrityError` handler depends on block rollback                                                                                         |
| Delegation notification family (`task_worker.py:2034-2097, 2130-2200, 2259-2345`)                                          | documented rollback-on-send-failure keeps `notified_at` NULL for retry                                                                                           |
| `email.store_incoming` (`repositories/email.py:44-94`)                                                                     | insert + index-task enqueue + backfill update                                                                                                                    |
| `spawn_worker_tool` DB pair (`tools/worker.py:431-463`)                                                                    | worker task row + completion event listener                                                                                                                      |
| Indexing doc+embedding pairs (`message_history_indexer.py:110-129`, `notes_indexer.py:89-95`, `indexing/tasks.py:195-205`) | doc committed without embeddings = permanently unsearchable (upsert makes re-runs no-ops); embedding generation (network) happens *before* the transaction opens |
| `a2a_tasks.create_task_if_absent` fallback + `email_indexer` attachment registration                                       | `begin_nested()` requires an enclosing transaction                                                                                                               |

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

### 2. ast-grep migration ratchet (shrink-only counter)

Reuse the suppression-budget pattern (`scripts/check_suppression_budget.py`, `.lint-budget.toml`):
its ratchet logic is generic; only its counter is ruff-specific. Add a sibling counter driven by
`ast-grep scan --json` over migration rules:

- `migration-legacy-db-context`: any reference to `DatabaseContext` / `get_db_context`
- `migration-isolated-context`: calls to `create_isolated_context` / `supports_isolated_writes`

Budgets start at today's counts, auto-lower as sites are converted (rewritten file must reach the
commit — same mechanic as the existing script), and **fail CI on any increase**. This makes
incremental migration enforceable: the old pattern can only shrink, and the milestones below can
land as separate reviewable PRs without the old world leaking back. When a counter hits zero, the
symbol is deleted and the rule flips to a permanent `severity: error` conformance rule.

Two more permanent rules land with the end state:

- ban `engine.begin()` / raw transaction management outside `storage/`
- ban raw `create_async_engine` outside `storage/base.py` — today two test fixtures bypass the
  factory (`tests/conftest.py:324`, `tests/functional/web/conftest.py:548`) and therefore don't test
  production pool semantics at all

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
dual-backend coverage is the single most valuable existing asset. Two gaps to close first (Milestone
0): the PG test engine bypasses the production factory (wrong pool class), and the Playwright
session engine bypasses it entirely. Route all test engines through
`create_engine_with_sqlite_optimizations` so the pool/serialization changes in this design are
actually what CI exercises.

## Migration plan

Milestones are separately land-able PRs, each green on full dual-backend CI. The old and new
implementations coexist *only* between M1 and M3, with the ratchet guaranteeing monotone progress —
this is a bounded migration with an enforced endpoint, not a compatibility layer.

- **M0 — groundwork (no semantic change).** Unify test engine creation through the production
  factory; add engine instrumentation + leak/duration checks (observational); land the ast-grep
  ratchet tooling with budgets at current counts; add the migration rules.
- **M1 — new core + storage layer.** Introduce `Database` / `DatabaseTransaction` / executor
  protocol; rewrite repositories (adding `atomic()` to multi-statement methods); convert the task
  worker (already structured as four transaction phases at `task_worker.py:3351-3465`, it is the
  natural first consumer) and delete its 14 isolated-context sites. Enable the visibility property
  test.
- **M2 — entry points, one per PR.** Web `get_db` dependency (endpoints receive `Database`; explicit
  blocks in webhooks/oauth/a2a per worklist) → turn pipeline (`turn_producer`, `chat_api`,
  `llm_loop`, `ToolExecutionContext`) → telegram handler → a2a → asterisk. As each flips, its lane
  of the LLM/tool-boundary invariant goes from observational to enforcing. **#1076 is structurally
  fixed** when the turn pipeline flips: tool-registered attachments are committed at registration
  and visible to delegation validation.
- **M3 — excision.** Delete `DatabaseContext`, `get_db_context`, `create_isolated_context`,
  `supports_isolated_writes`, `_USE_ISOLATED_HISTORY_WRITES` and the `save_with_isolated_context`
  plumbing, the a2a pre-commit workaround (`a2a_api.py:310-347`), the deferred-confirmation FK
  workaround (`deferred_tool_confirmation.py:150-158`), and `tools_api.py`'s manual rollback. Flip
  migration rules to bans; update `AGENTS.md`'s Database Access Pattern section and the error
  message that told the model to "try again once they are committed".

## Alternatives considered

- **Tactical fix for #1076 only** (register tool attachments via isolated context, mirroring
  `_save_history_message`): unbreaks `media_analyst`, but is workaround #22; every future
  (turn-context write → other-connection read) pair is another incident. Superseded by M2, though it
  can land independently if the migration stalls.
- **Big-bang rewrite in one PR**: ~130 sites + 50 endpoints; unreviewable, and a single regression
  blocks everything. The ratchet exists precisely to make incremental safe.
- **Keep `DatabaseContext`'s name/shape, change semantics silently**: zero compile errors → no
  worklist, and the most dangerous kind of change (behaviour shifts under unchanged syntax).
  Rejected.
- **Session-per-turn with SQLAlchemy ORM `Session`**: this codebase is Core-based with a working
  repository pattern; introducing the ORM is an unrelated (larger) migration.

## Open questions

1. PostgreSQL pool bounds (`pool_size` / `max_overflow`) — propose 5/10 for this deployment's scale.
2. Naming: `Database` / `DatabaseTransaction` vs `Db` / `DbTransaction` vs keeping a `*Context`
   suffix.
3. Should the LLM/tool-boundary ContextVar assertion also run in production (as a logged warning)
   rather than tests only?
4. Whether the M2 entry-point PRs flip the web dependency before or after the turn pipeline
   (proposed: web first — smaller blast radius, exercises the endpoint surface early).

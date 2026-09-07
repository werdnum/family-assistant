# Message-history indexing: idempotency

## Problem

A billing investigation covering 11 August – 6 September attributed **$241.81 (80%) of
post-27-August Gemini spend to `gemini-embedding-001`**, against 1.61 billion billed units and
124,831 successful embedding calls over two days — while only 147 conversational LLM calls happened
in the same window. Embedding spend went from ~$0.44/day to ~$21.98/day, roughly 50×, while the
deployment rate only doubled.

The application was re-embedding message history it had already embedded. Three mechanisms combined:

1. **Startup rewound the backfill.** `system_message_history_backfill` is a system task, and
   `TasksRepository.enqueue` upserts system tasks: it overwrites the payload and revives the row out
   of a terminal state. The startup payload is `{"limit": N}` — no cursor — so every process start
   reset the walk to the beginning of history and re-ran it over a corpus that only grows. At 4.55
   deployments/day this is the dominant term.

2. **Selection did not know what was already indexed.** `get_indexable_message_groups` orders by
   `internal_id` and returns everything in range. The write side upserts (documents are unique on
   `source_id`, embeddings on `(document_id, chunk_index, embedding_type)`), so re-running produced
   no duplicate rows — but the provider call that produced the vector had already been paid for.
   Storage-level upsert deduplicates the *result*; it does not deduplicate the *spend*.

3. **Every persisted row queued a fresh index of the whole turn.** `add_message` enqueues one
   `index_message_history_batch` per row, keyed on `turn_id`. A 43-row turn therefore embedded that
   turn 43 times, each time over a slightly longer prefix of the same conversation. In a 40-minute
   sample of one pod, 251 documents were embedded more than once and one was embedded 43 times.

## Approach

Make the embedding call itself the thing that is conditional, and stop the walk from restarting.

### Identity is content, not position

Before embedding a turn, compare what would be written against what is stored:
`source_id + content_hash + embedding_model`. The columns already exist — `documents.source_id` is
unique, `document_embeddings` carries `content_hash` and `embedding_model` — so this is a lookup,
not a schema change. A group whose stored fingerprint matches is skipped without a provider call.

This is the enforcement chokepoint: it sits in the one place every message-history indexing path
funnels through, so a redundant enqueue from *any* source — a restarted backfill, a duplicate
per-turn task, a retry, a future caller — costs a database read instead of an embedding. Nothing
upstream has to be careful.

Including `embedding_model` in the identity is what makes a model migration work, and it has to
reach the backfill as well as the fingerprint — see below. Search matches `embedding_model` exactly,
so turns embedded under a retired model answer nothing.

The identity is only as good as what that column records, which is the model's *name*. A
configuration that moves the embedding space while keeping the name — changing
`EMBEDDING_DIMENSIONS`, say — is invisible to it, and to everything else keyed on that column: the
document, email and note indexers share the gap. Recording the space rather than the name is the
real fix, and it is a change to a stored value with a corpus-wide re-index behind it, so it belongs
in its own change. Until then `force` (below) is the supported way to rebuild.

### The backfill is seeded once, not on every start

`enqueue` gains `only_if_absent`, which turns the system-task upsert into an insert that does
nothing when the row already exists. Startup uses it for the backfill, so a redeploy no longer
rewinds the cursor or revives a finished walk.

The cursor then needs no separate home. A batch enqueues its own continuation carrying
`after_internal_id`, as a normal queued task; if the process dies mid-walk that row is still pending
and the next process picks it up. Retries re-run the same batch rather than the whole history, and
with content identity in place a re-run of an already-indexed batch is free.

What decides whether the walk runs again is the corpus, not a count of how many times it has run. If
any stored turn was embedded by a model other than the configured one, that turn answers no query,
and a *finished* seed is cleared so the walk runs again; a pending or running one is never touched,
so a restart mid-migration resumes rather than rewinding. Reading the state is what makes a rollback
work — returning to a model the corpus has since been re-embedded away from needs the same repair as
moving to a new one, and a seed keyed on the model name would call that already done.

For the changes the model name cannot show — the indexed text, or an embedding space that moved
under a name that stayed put — the walk takes `force`, which skips the fingerprint and re-embeds
what it visits. That is the supported way to rebuild the corpus.

Neither the seed nor its continuations are enqueued *unscheduled*. Dequeue orders by `scheduled_at`
with nulls first, so an unscheduled task outranks every scheduled one however long that one has been
due — and because a batch enqueues its continuation from inside its predecessor's handler, the chain
keeps such a row in front of the queue for the whole walk. In one incident that held every scheduled
task for 96 minutes with no backlog to see, since the chain regenerates exactly one row at exactly
the moment it matters.

They are scheduled for *now*, not for later. Carrying a timestamp is what takes them out of the
nulls-first group; delaying them would multiply, because the walk is a chain — thirty seconds across
a corpus that re-embeds in four thousand batches is a day and a half. Due-now tasks run at full
speed and take their turn by due time, which is what lets a poll that has been due for forty minutes
go first.

This is a stopgap confined to the enqueue sites here. Ordering the whole queue by due time and
giving it priority lanes is [task-queue-priority-lanes.md](task-queue-priority-lanes.md), which
retires the fairness role of these timestamps and of the per-row delay, leaving the latter its
debounce purpose.

### Per-turn indexing waits for the turn to finish

The per-row enqueue stays — it is the thing that guarantees a turn gets indexed — but the task is
scheduled a short delay after the row lands, plus a jitter. By the time the first of a turn's tasks
runs, the turn is usually complete; it embeds once, and its siblings find a matching fingerprint and
skip. The mechanism is the identity check again, not a new debounce lifecycle — which is also its
limit: the fingerprint read is not a claim on the turn, and the jitter shrinks the window in which
two workers both find it empty rather than closing it.

## Deliberate simplifications

- **A turn that stays open longer than the delay is embedded more than once.** Each of those
  embeddings covers genuinely new content, so they are re-indexes rather than duplicates, and the
  count is bounded by the turn's duration rather than by its row count. Not worth a debounce state
  machine.
- **Two workers can still embed the same turn once each.** The fingerprint read is not a claim, so a
  turn's sibling tasks that come due close enough together both find nothing stored and both pay.
  The delay and its jitter make that uncommon, and the waste is bounded by the worker count rather
  than by the turn's row count — a duplicate, not the per-row multiplication this change removes.
  Claiming a turn before embedding it would need a lock or a lease, which is more lifecycle than a
  bounded duplicate is worth.
- **A backfill chain that exhausts its retries stays broken.** The walk stops and the gap persists
  until someone re-seeds. The only signal is the failed task row: a task that gives up logs at
  warning rather than error, so it does not reach `error_logs`, and the `embedded`-to-`skipped`
  ratio cannot show it either — a stopped walk emits no samples, which reads exactly like an idle
  deployment. What is bounded is the damage: the per-turn tasks keep indexing new conversations, so
  a dead chain costs coverage of old history, not of anything current.
- **A configuration change that keeps the model's name is not detected.** The fingerprint and the
  corpus check both compare `embedding_model`, which holds a name, not an embedding space. Changing
  `EMBEDDING_DIMENSIONS` alone therefore leaves stored vectors looking current, and pgvector raises
  a dimension error on query until the corpus is rebuilt with `force`. The failure is loud rather
  than silent, and the gap predates this change and is shared by every indexer; closing it means
  recording the space in the stored column, which is a migration of its own.
- **Embedding requests are still one text per call.** Batching reduces request overhead but not
  billed units, and the per-provider batch limits are a live failure risk; it is a throughput change
  to make on its own evidence, not part of this fix.

## Verification

- Indexing the same turn twice issues one embedding call; changing the turn's content issues a
  second.
- Changing the embedding model re-embeds content whose text did not change, and clears a finished
  backfill so the corpus is re-indexed under it — including on a rollback to an earlier model.
- A second startup seed leaves an existing backfill task's payload and status untouched, and one
  issued mid-migration does not rewind the walk's cursor.
- Every background indexing task carries a `scheduled_at`, so none of them can outrank scheduled
  work.
- `force` re-embeds a turn the fingerprint considers current.
- A generator whose result disagrees with its own `model_name` fails the batch loudly, rather than
  re-embedding the corpus on every pass because no fingerprint can match.
- `family_assistant_indexing_documents{source_type, outcome}` counts `embedded` against
  `skipped_unchanged`; a sustained ratio near 1:0 on a corpus that is not growing means selection
  has come loose again. It does not detect a stopped walk — see above.

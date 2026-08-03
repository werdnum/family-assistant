# Reaping unreferenced attachments

## Problem

The web client uploads an attachment first (`POST /api/attachments/upload`) and only afterwards
sends the message that references it. The upload commits an `attachment_metadata` row and writes the
file immediately, so every send that never produces a persisted message leaves both behind: the user
closes the tab after attaching, the kickoff fails, the network drops, or the conversation already
has a running turn and the send is refused with 409.

Nothing collected those rows. `AttachmentRegistry.cleanup_orphaned_attachments` builds its
"referenced" set from *every* row in `attachment_metadata`, so it can only delete files that have no
row at all — an abandoned upload has a row, and keeps it forever. It also had no caller.

## Approach

A reaper that works on the state that actually decides whether an attachment is live: a metadata row
nothing references, older than a grace period. That covers every route to the orphan — the turn
guard's 409, an abandoned compose, a failed kickoff — rather than the one route that prompted it.

### What counts as referenced

Candidates are restricted to `source_type = "user"`, which is exactly the class of rows the upload
endpoint and the interface handlers create. Tool-, script- and email-sourced rows are reachable from
delegation runs, scripts, and `received_emails.attachment_info`, and are never candidates, so the
reaper does not need to understand those reference sets.

A candidate row is referenced when any of the following holds:

- `attachment_metadata.message_id` is set;
- some `message_history.attachments` entry names its `attachment_id` — this is the reference the
  send path actually writes, since nothing back-fills `message_id` for user attachments;
- some `notes.attachment_ids` entry names it — the notes tool can attach an uploaded file to a note
  that outlives the message that carried it.

Both JSON checks are dialect-specific (`jsonb_array_elements` on PostgreSQL, `json_each` on SQLite),
following the pattern already used for note visibility labels.

### Paging, and why the limit is not applied first

Nothing back-fills `message_id`, so every upload a message references stays in the candidate columns
forever — the candidate set is "every user attachment ever", not "the orphans". Two things follow,
and the reaper has to satisfy both at once:

- The batch limit has to bound rows *collected*, not rows *examined*. Limiting candidates first
  would let the oldest sent uploads fill every pass, and the orphans behind them would never be
  reached.
- The reference check has to be a scan per page, not a correlated subquery per candidate. The JSON
  columns are unindexed, so a `NOT EXISTS` per candidate row makes a pass that collects nothing cost
  one message-history scan per historical attachment.

So candidates are walked oldest-first in keyset pages of `REAP_PAGE_SIZE`, each page's references
resolved with one scan of the message JSON and one of the note JSON, and paging continues until the
limit is filled or the candidates run out. A keyset cursor on `(created_at, attachment_id)` rather
than an OFFSET keeps a late page as cheap as the first, and stops a deletion from an earlier page
shifting a later one past the reader.

### Grace period

A row is only a candidate once it is older than the grace period (24 hours by default), so an
in-progress compose is never collected out from under the user, and neither is an upload whose send
is still sitting in the worker queue.

### Ordering and cost

The reaper deletes rows first and unlinks files afterwards, matching `delete_attachment`: a file
without a row is collectable, a row without a file is a broken attachment. The reference scan runs
only when a candidate exists, which is the uncommon case, so the usual nightly pass is a single
indexed lookup that returns nothing.

Each pass is bounded by a batch limit on the rows collected — orphans, not candidates — so a backlog
drains over successive runs rather than in one long transaction.

A missing attachment registry fails the task rather than returning quietly: a pass that collects
nothing every day while files accumulate should look like the broken worker configuration it is.

### `cleanup_orphaned_attachments`

It is kept, with the narrower job it actually does: deleting files that have no metadata row at all
— leftovers from a store that committed the file but not the row, and the files of rows the reaper
has just deleted if an unlink failed. It now runs as the second phase of the same task, and only
considers files older than the grace period, so an upload writing its file while the sweep runs is
not collected before its row commits.

## Delivery

`attachment_cleanup` is a system task registered alongside the other cleanups and scheduled daily at
3am local, with a 24-hour grace period.

Scheduling it surfaced a bug in the shared machinery: a system task keeps one row across occurrences
(both the recurrence and the startup setup re-enqueue under the same `system_...` id), and the
upsert updated the schedule and payload but not the status. A row marked `done` by its first run
therefore stayed `done`, and dequeue — which selects pending, or stale processing, rows — never
picked it up again. Every recurring system task ran exactly once.

The upsert now hands a finished row back to the queue: a row in a terminal status (`done`, `failed`)
is reset to `pending` with its retry count and locks cleared, while a row still `pending` or
`processing` keeps those columns, so a startup upsert cannot resurrect an occurrence a worker is
running or reset one that is mid-retry.
